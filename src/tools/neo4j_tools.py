import asyncio
import logging
import re
from collections import defaultdict
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from src.db.model_schema import is_missing, render_missing
from src.db.neo4j_conn import Neo4jConnection, Neo4jConnectionError


logger = logging.getLogger(__name__)

PriceRange = Literal["0-10万", "10-20万", "20-30万", "30-40万", "40-50万", "50万以上"]
EnergyType = Literal["汽油", "纯电动", "插电式混合动力", "增程式", "其他"]
NodeType = Literal["品牌", "车系", "车型", "价格区间", "能源类型"]
QueryLimit = Annotated[int, Field(ge=1, le=500)]


# 排序统一用规范化属性（launch_ym=YYYYMM 整数、range_cltc_km 等），并用
# `ORDER BY 列 IS NULL, 列 DESC` 把缺失稳定压到末位：既不把字符串哨兵排到数字前面，
# 也不依赖 Neo4j 对 null 的排序约定（实测 null 在 DESC 下排最前，会把"无上市时间"的
# 车顶成最新）。缺失在返回给上游之前由 render_missing 按字段回显成文案。


def _present(value):
    return not is_missing(value)


def _price_bucket(record):
    """价格分档：只认查询投影出来的 ``价格区间`` 关系节点名。

    这里不再从规范化价格 ``price_wan`` 现算——没有任何查询把 ``price_wan``
    投影出来，留一段永不生效的兜底只会让后来人误以为还有第二条路径。
    价格有效的车 100% 带 ``IN_PRICE_RANGE`` 关系，这一条路已足够。
    """
    explicit_bucket = record.get("价格区间")
    return explicit_bucket if _present(explicit_bucket) else None


def _launch_key(record):
    """返回整数排序键，缺失记为 0；统一类型避免与字符串比较时报错。"""
    launch = record.get("上市年月")
    if isinstance(launch, (int, float)):
        return int(launch)
    digits = re.sub(r"\D", "", str(record.get("上市时间") or ""))
    return int(digits[:6]) if len(digits) >= 6 else 0


def stratified_sample(records: list[dict], max_count: int = 20) -> list[dict]:
    """Select a deterministic, diverse and recent subset of vehicle records."""
    if max_count <= 0:
        return []
    if len(records) <= max_count:
        return list(records)

    ordered = sorted(records, key=_launch_key, reverse=True)
    selected = []
    selected_ids = set()

    def add(record):
        record_id = id(record)
        if record_id in selected_ids or len(selected) >= max_count:
            return False
        selected.append(record)
        selected_ids.add(record_id)
        return True

    # Keep half the capacity for dimensions other than brand.
    brand_cap = max(1, max_count // 2)
    brand_buckets = defaultdict(list)
    for record in ordered:
        brand = record.get("品牌")
        if _present(brand):
            brand_buckets[brand].append(record)
    for bucket in brand_buckets.values():
        if len(selected) >= brand_cap:
            break
        add(bucket[0])

    covered_energy = {
        record.get("能源类型") for record in selected if _present(record.get("能源类型"))
    }
    for record in ordered:
        energy = record.get("能源类型")
        if _present(energy) and energy not in covered_energy and add(record):
            covered_energy.add(energy)

    covered_prices = {
        bucket for record in selected if (bucket := _price_bucket(record)) is not None
    }
    for record in ordered:
        bucket = _price_bucket(record)
        if bucket is not None and bucket not in covered_prices and add(record):
            covered_prices.add(bucket)

    for record in ordered:
        if len(selected) >= max_count:
            break
        add(record)
    return selected


def sample_stats(records, sampled):
    def unique(field, data):
        return len({record.get(field) for record in data if _present(record.get(field))})

    return {
        "原始条数": len(records),
        "采样条数": len(sampled),
        "品牌覆盖": f"{unique('品牌', sampled)}/{unique('品牌', records)}",
        "能源覆盖": f"{unique('能源类型', sampled)}/{unique('能源类型', records)}",
        "价格区间覆盖": f"{len({_price_bucket(r) for r in sampled if _price_bucket(r)})}/"
        f"{len({_price_bucket(r) for r in records if _price_bucket(r)})}",
    }


def validate_result(data, tool_name):
    if not data:
        return f"[{tool_name}] 未查询到结果，请调整筛选条件后重试。"

    required_fields = ("品牌", "车系", "车型")
    if all(all(not _present(record.get(field)) for field in required_fields) for record in data):
        return f"[{tool_name}] 查询结果缺少车型标识，数据可能异常。"
    return data


async def _run_query_threaded(cypher: str, params: dict) -> tuple[list[dict], str | None]:
    def sync_query():
        connection = Neo4jConnection()
        with connection.get_session() as session:
            return session.run(cypher, params).data()

    try:
        return await asyncio.to_thread(sync_query), None
    except Neo4jConnectionError as exc:
        logger.error("Neo4j connection error: %s", exc)
    except Exception:
        logger.exception("Neo4j query failed")
    return [], "数据库查询暂时不可用，请稍后重试。"


async def _query_with_sample(
    cypher: str,
    params: dict,
    tool_name: str,
    *,
    max_count: int = 20,
    preserve_order: bool = False,
):
    raw, error = await _run_query_threaded(cypher, params)
    if error:
        return error
    validated = validate_result(raw, tool_name)
    if isinstance(validated, str):
        return validated
    # 顺序不可颠倒：采样与统计都要看未渲染的记录（渲染后每格都是非空字符串，
    # 判空、分档、排序键全部失效）。库侧排序已完成，渲染只影响给上游看的文案，
    # 并额外汇报每列的缺失构成，让"哪些行排不进来"有据可查。
    sampled = validated[:max_count] if preserve_order else stratified_sample(validated, max_count)
    rendered, missing = render_missing(sampled)
    result = {"数据": rendered, "采样统计": sample_stats(raw, sampled)}
    if missing:
        result["缺失说明"] = missing
    return result


@tool
async def query_by_brand(brand: str, limit: QueryLimit = 500) -> dict | str:
    """按品牌查询车型。brand 必须是完整品牌名称。"""
    cypher = """
    MATCH (b:品牌)-[:HAS_SERIES]->(s:车系)-[:HAS_MODEL]->(m:车型)
    WHERE b.name = $brand
    OPTIONAL MATCH (m)-[:IN_PRICE_RANGE]->(p:价格区间)
    OPTIONAL MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           coalesce(e.name, m.能源类型) AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间, m.launch_ym AS 上市年月
    ORDER BY 上市年月 IS NULL, 上市年月 DESC, m.车名
    LIMIT $limit
    """
    return await _query_with_sample(cypher, {"brand": brand.strip(), "limit": limit}, "query_by_brand")


@tool
async def query_by_price_range(price_range: PriceRange, limit: QueryLimit = 500) -> dict | str:
    """按价格区间查询车型。"""
    cypher = """
    MATCH (m:车型)-[:IN_PRICE_RANGE]->(p:价格区间)
    WHERE p.name = $price_range
    MATCH (s:车系)-[:HAS_MODEL]->(m)
    MATCH (b:品牌)-[:HAS_SERIES]->(s)
    OPTIONAL MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           coalesce(e.name, m.能源类型) AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间, m.launch_ym AS 上市年月
    ORDER BY 上市年月 IS NULL, 上市年月 DESC, m.车名
    LIMIT $limit
    """
    return await _query_with_sample(
        cypher,
        {"price_range": price_range, "limit": limit},
        "query_by_price_range",
    )


@tool
async def query_by_energy_type(energy_type: EnergyType, limit: QueryLimit = 500) -> dict | str:
    """按能源类型查询车型。"""
    cypher = """
    MATCH (m:车型)-[:ENERGY_TYPE_IS]->(e:能源类型)
    WHERE e.name = $energy_type
    MATCH (s:车系)-[:HAS_MODEL]->(m)
    MATCH (b:品牌)-[:HAS_SERIES]->(s)
    OPTIONAL MATCH (m)-[:IN_PRICE_RANGE]->(p:价格区间)
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           e.name AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间, m.launch_ym AS 上市年月,
           m.range_cltc_km AS 纯电续航
    ORDER BY 纯电续航 IS NULL, 纯电续航 DESC, 上市年月 IS NULL, 上市年月 DESC
    LIMIT $limit
    """
    return await _query_with_sample(
        cypher,
        {"energy_type": energy_type, "limit": limit},
        "query_by_energy_type",
    )


@tool
async def query_by_conditions(
    price_range: PriceRange | None = None,
    energy_type: EnergyType | None = None,
    level: str | None = None,
    brand: str | None = None,
    limit: QueryLimit = 500,
) -> dict | str:
    """按价格、能源、级别和品牌的任意组合查询车型；level 使用包含匹配。"""
    base_filters = []
    params = {"limit": limit}
    if level and level.strip():
        base_filters.append("m.级别 CONTAINS $level")
        params["level"] = level.strip()
    if brand and brand.strip():
        base_filters.append("b.name = $brand")
        params["brand"] = brand.strip()

    clauses = ["MATCH (b:品牌)-[:HAS_SERIES]->(s:车系)-[:HAS_MODEL]->(m:车型)"]
    if base_filters:
        clauses.append("WHERE " + " AND ".join(base_filters))
    if price_range:
        clauses.extend(
            [
                "MATCH (m)-[:IN_PRICE_RANGE]->(p:价格区间)",
                "WHERE p.name = $price_range",
            ]
        )
        params["price_range"] = price_range
    else:
        clauses.append("OPTIONAL MATCH (m)-[:IN_PRICE_RANGE]->(p:价格区间)")
    if energy_type:
        clauses.extend(
            [
                "MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)",
                "WHERE e.name = $energy_type",
            ]
        )
        params["energy_type"] = energy_type
    else:
        clauses.append("OPTIONAL MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)")

    cypher = " ".join(clauses) + """
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           coalesce(e.name, m.能源类型) AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间, m.launch_ym AS 上市年月
    ORDER BY 上市年月 IS NULL, 上市年月 DESC, m.车名
    LIMIT $limit
    """
    return await _query_with_sample(cypher, params, "query_by_conditions")


@tool
async def compare_models(brand: str, limit: QueryLimit = 500) -> dict | str:
    """对比同一品牌下车型的价格、续航、加速、尺寸、保修和保养成本。"""
    cypher = """
    MATCH (b:品牌)-[:HAS_SERIES]->(s:车系)-[:HAS_MODEL]->(m:车型)
    WHERE b.name = $brand
    OPTIONAL MATCH (m)-[:IN_PRICE_RANGE]->(p:价格区间)
    OPTIONAL MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           coalesce(e.name, m.能源类型) AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间, m.launch_ym AS 上市年月,
           m.range_cltc_km AS 纯电续航,
           m.accel_100_s AS 百公里加速,
           m.`长x宽x高(mm)` AS 车身尺寸, m.`车身结构` AS 车身结构,
           m.warranty_years AS 保修年数, m.warranty_km AS 保修公里数,
           m.maintenance_60k_cost AS 保养成本
    ORDER BY 上市年月 IS NULL, 上市年月 DESC, m.车名
    LIMIT $limit
    """
    return await _query_with_sample(cypher, {"brand": brand.strip(), "limit": limit}, "compare_models")


@tool
async def query_by_maintenance_cost(
    price_ranges: list[PriceRange],
    limit: QueryLimit = 500,
) -> dict | str:
    """查询指定价格区间内六万公里保养总成本最低的车型。"""
    if not price_ranges:
        return "[query_by_maintenance_cost] 至少需要一个价格区间。"
    cypher = """
    MATCH (m:车型)-[:IN_PRICE_RANGE]->(p:价格区间)
    WHERE p.name IN $price_ranges
      AND m.maintenance_60k_cost IS NOT NULL
    MATCH (s:车系)-[:HAS_MODEL]->(m)
    MATCH (b:品牌)-[:HAS_SERIES]->(s)
    OPTIONAL MATCH (m)-[:ENERGY_TYPE_IS]->(e:能源类型)
    RETURN DISTINCT b.name AS 品牌, s.name AS 车系, m.车名 AS 车型,
           m.`官方指导价` AS 指导价, p.name AS 价格区间,
           m.maintenance_60k_cost AS 保养成本,
           coalesce(e.name, m.能源类型) AS 能源类型, m.级别 AS 级别,
           m.`上市时间` AS 上市时间
    ORDER BY m.maintenance_60k_cost ASC
    LIMIT $limit
    """
    return await _query_with_sample(
        cypher,
        {"price_ranges": list(dict.fromkeys(price_ranges)), "limit": limit},
        "query_by_maintenance_cost",
        preserve_order=True,
    )


@tool
async def explore_schema(node_type: NodeType, limit: QueryLimit = 15) -> list[dict] | str:
    """返回指定知识图谱节点类型的少量属性样例。"""
    cypher = f"MATCH (n:`{node_type}`) RETURN properties(n) AS properties LIMIT $limit"
    raw, error = await _run_query_threaded(cypher, {"limit": limit})
    if error:
        return error
    if not raw:
        return f"[explore_schema] 节点类型“{node_type}”没有数据。"
    return [record["properties"] for record in raw if record.get("properties") is not None]
