"""车辆字段语义契约：导入与查询共用的唯一事实来源。

源数据 ``models.json`` 有三个会让查询语义分叉的特点：

1. **数值带单位/后缀**：``'76.98万'``、``'294(400Ps)'``、``'26812.0元'``、
   ``'2017.09'``、``'4194x1832x1367'``。原样写库后，每个查询都要自己写字符串
   解析，于是 ``toInteger('294(400Ps)')`` 得到 ``null``、
   ``ORDER BY m.上市时间 DESC`` 会把字符串 ``'None'`` 排到数字前面。
2. **缺失有多种写法**：真 ``null``、字符串 ``'None'``、业务用语
   ``'暂无报价'``/``'待查'``，以及"该记录根本没有这个字段"。四种写法要求四种
   判空方式。
3. **字段集合异构**：43 个字段分布在 40912 条记录里，多数只覆盖一部分记录。

本模块把这份契约固定成一处，导入与查询都从这里取：

- **缺失只有一种表示**：属性不存在。所有哨兵值在导入时被剔除，查询只需判空。
- **原文与数值分列**：语义明确的数值额外写成 ASCII 规范化属性
  （``price_wan``、``power_kw``、``launch_ym``…），可直接排序比较；原始文本
  字段照旧保留，供展示、追溯与既有查询复用。
- **复合值拆成独立语义**：``'294(400Ps)'`` 拆成 ``power_kw``+``power_ps``；
  ``'4194x1832x1367'`` 拆成长宽高；``'2017.09'`` 变 ``launch_ym=201709``；
  ``'3年或10万公里'`` 拆成 ``warranty_years``+``warranty_km``。

**查询侧约定**：比较、排序、聚合一律使用规范化属性；原始文本字段只用于展示。
"""

import re

__all__ = [
    "MISSING_SENTINELS",
    "ENERGY_SOURCE_FIELD",
    "SCALAR_FIELDS",
    "POWER_FIELDS",
    "SPECIAL_FIELDS",
    "is_missing",
    "normalize_model",
    "missing_reason",
    "missing_label",
    "render_missing",
    "PRICE_COLUMNS",
    "PRICE_MISSING_LABEL",
    "DEFAULT_MISSING_LABEL",
    "NOT_APPLICABLE_LABEL",
    "COLUMN_ENERGY_SCOPE",
    "UNPUBLISHED",
    "NOT_APPLICABLE",
]


# 缺失哨兵：出现这些值等同于"没有数据"，导入时整条属性不写入。
# 注意 ``'未知'`` 不在此列——它是能源类型里的一个真实类别（28 条），不是缺失。
MISSING_SENTINELS = frozenset(
    {None, "", "None", "NONE", "none", "暂无", "暂无报价", "待查", "-", "—", "N/A"}
)

# 能源类型有两层语义：原始细分写法（15 类，含 ``None``）与规范化类别（5 类，
# 由 ``relationships.csv`` 给出，与 ``ENERGY_TYPE_IS`` 关系边同源）。导入时
# 两者分别写入 ``能源类型`` 与 ``energy_raw``。
ENERGY_SOURCE_FIELD = "能源类型"


# 单值数值字段：源字段 -> 规范化属性名。取第一个数字，因此对 ``'76.98万'``、
# ``'26812.0元'`` 这类"数字+单位"写法天然有效。
SCALAR_FIELDS = {
    # 价格：''万''即为单位，数值本身就是万元，不需要再乘
    "官方指导价": "price_wan",
    # 性能
    "最高车速(km/h)": "top_speed_kmh",
    "官方百公里加速时间(s)": "accel_100_s",
    "官方0—50Km/h加速时间(s)": "accel_50_s",
    # 续航：不同测试标准口径不同，各自独立成字段，不做合并
    "纯电续航里程(km)CLTC": "range_cltc_km",
    "纯电续航里程(km)工信部": "range_miit_km",
    "纯电续航里程(km)NEDC": "range_nedc_km",
    "纯电续航里程(km)WLTC": "range_wltc_km",
    "综合续航里程(km)CLTC": "total_range_cltc_km",
    "综合续航里程(km)工信部": "total_range_miit_km",
    "综合续航里程(km)NEDC": "total_range_nedc_km",
    "综合续航里程(km)WLTC": "total_range_wltc_km",
    # 能耗
    "百公里耗电量(kWh/100km)": "energy_cons_kwh",
    "电能当量燃料消耗量(L/100km)": "energy_equiv_l",
    "NEDC综合油耗(L/100km)": "fuel_nedc_l",
    "WLTC综合油耗(L/100km)": "fuel_wltc_l",
    "最低荷电状态燃料消耗量(L/100km)工信部": "fuel_min_soc_miit_l",
    "最低荷电状态燃料消耗量(L/100km)NEDC": "fuel_min_soc_nedc_l",
    "最低荷电状态燃料消耗量(L/100km)WLTC": "fuel_min_soc_wltc_l",
    "最低荷电状态燃料消耗量(L/100km)CLTC": "fuel_min_soc_cltc_l",
    "官方百公里综合醇耗(L/100km)WLTC": "alcohol_cons_wltc_l",
    # 养护：源值形如 '26812.0元'
    "6万公里保养总成本预估": "maintenance_60k_cost",
    # 扭矩
    "最大扭矩(N·m)": "torque_nm",
    "发动机最大扭矩(N·m)": "engine_torque_nm",
    "电动机最大扭矩(N·m)": "motor_torque_nm",
}

# 功率字段：源值形如 '294(400Ps)'（kW 在前、马力在括号内），少数记录只有裸数字。
# 两个量纲含义不同，必须拆开，否则数值比较会拿 kW 和 Ps 混着比。
POWER_FIELDS = {
    "最大功率(kW)": ("power_kw", "power_ps"),
    "发动机最大功率(kW)": ("engine_power_kw", "engine_power_ps"),
    "电动机最大功率(kW)": ("motor_power_kw", "motor_power_ps"),
}

# 纯文本字段（车名、厂商、级别、变速箱、车身结构…）不需要清单：``normalize_model``
# 原样保留所有源字段，只有上面三张表列出的字段才会额外产出可比较的数值属性。


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)"
)
_LAUNCH_RE = re.compile(r"(\d{4})\s*[.\-/年]?\s*(\d{1,2})?")


def is_missing(value) -> bool:
    """判断源值是否表示"没有数据"（真 null、空串或任何缺失哨兵）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in MISSING_SENTINELS
    return False


def _numbers(value) -> list[float]:
    if is_missing(value):
        return []
    return [float(match.group()) for match in _NUMBER_RE.finditer(str(value))]


def _parse_launch_ym(value):
    """``'2017.09'`` -> ``201709``；只有年份时月份记 0，便于按整数排序。"""
    match = _LAUNCH_RE.fullmatch(str(value).strip())
    if not match:
        return {}
    year = int(match.group(1))
    month = int(match.group(2) or 0)
    if not 1 <= month <= 12:
        month = 0
    return {"launch_ym": year * 100 + month}


def _parse_dimensions(value):
    """``'4194x1832x1367'`` -> 长/宽/高三个毫米整数。"""
    match = _SIZE_RE.search(str(value))
    if not match:
        return {}
    return {
        "length_mm": int(float(match.group(1))),
        "width_mm": int(float(match.group(2))),
        "height_mm": int(float(match.group(3))),
    }


def _parse_warranty(value):
    """``'3年或10万公里'`` -> ``warranty_years=3, warranty_km=100000``。

    "不限公里/不限里程"与"不限年限"分别用布尔标记，避免用 0 冒充不限。
    """
    text = str(value)
    parsed = {}
    if "不限年限" in text:
        parsed["warranty_years_unlimited"] = True
    else:
        year = re.search(r"(\d+)\s*年", text)
        if year:
            parsed["warranty_years"] = int(year.group(1))
    if "不限公里" in text or "不限里程" in text:
        parsed["warranty_km_unlimited"] = True
    else:
        distance = re.search(r"(\d+(?:\.\d+)?)\s*万公里", text)
        if distance:
            parsed["warranty_km"] = int(float(distance.group(1)) * 10000)
    return parsed


def _parse_charge_hours(value):
    """``'快充0.5小时\\n慢充8小时'`` -> 快充/慢充各自的小时数。

    源文本里两种口径可能只出现一种，因此按键分别匹配而不是按位置切分。
    """
    text = str(value)
    parsed = {}
    for label, key in (("快充", "fast_charge_hours"), ("慢充", "slow_charge_hours")):
        match = re.search(rf"{label}\s*(\d+(?:\.\d+)?)", text)
        if match:
            parsed[key] = float(match.group(1))
    return parsed


def _parse_fast_charge_soc(value):
    """``'30-80'``/``'30%-80%'`` -> 区间上下限；单个数字视为上限。"""
    numbers = _numbers(value)
    if not numbers:
        return {}
    if len(numbers) >= 2:
        return {
            "fast_charge_soc_low": int(numbers[0]),
            "fast_charge_soc_high": int(numbers[1]),
        }
    return {"fast_charge_soc_high": int(numbers[0])}


# 需要专门解析的复合字段：源字段 -> 解析函数
SPECIAL_FIELDS = {
    "上市时间": _parse_launch_ym,
    "长x宽x高(mm)": _parse_dimensions,
    "整车保修期限": _parse_warranty,
    "充电时间(小时)": _parse_charge_hours,
    "快充电量(%)": _parse_fast_charge_soc,
}


def normalize_model(record: dict, *, energy_canonical: str | None = None) -> dict:
    """把一条源记录转成写入 Neo4j 的属性集合。

    - 缺失哨兵一律剔除，"缺失"在库里只剩"属性不存在"这一种表示；
    - 原始文本字段原样保留（含数值字段的原文，如 ``'76.98万'``，供展示与追溯）；
    - 追加 ASCII 规范化数值属性，供比较、排序、聚合使用；
    - ``能源类型`` 写成 5 类规范值（与 ``ENERGY_TYPE_IS`` 关系边同源，永远有值），
      原始 15 类细分写法另存为 ``energy_raw``。
    """
    normalized = {
        key: value for key, value in record.items() if not is_missing(value)
    }

    for field, canonical in SCALAR_FIELDS.items():
        numbers = _numbers(record.get(field))
        if numbers:
            normalized[canonical] = numbers[0]

    for field, (power_key, ps_key) in POWER_FIELDS.items():
        numbers = _numbers(record.get(field))
        if numbers:
            normalized[power_key] = numbers[0]
            if len(numbers) > 1:
                normalized[ps_key] = numbers[1]

    for field, parser in SPECIAL_FIELDS.items():
        value = record.get(field)
        if not is_missing(value):
            normalized.update(parser(value))

    raw_energy = record.get(ENERGY_SOURCE_FIELD)
    if not is_missing(raw_energy):
        normalized["energy_raw"] = str(raw_energy).strip()
    if energy_canonical:
        normalized[ENERGY_SOURCE_FIELD] = energy_canonical

    return normalized


# ---------------------------------------------------------------------------
# 缺失语义：存储层只保留 "null = 没有值" 一种表示，但同一个 null 在不同字段上
# 含义不同，回显时必须分开：
#
#   不适用  —— 该属性在这辆车上根本不存在（燃油车没有纯电续航）
#   未公布  —— 属性本应存在，但厂商没给（燃油车没给 NEDC 油耗）
#
# 判定规则来自全量数据分布，而不是记录里预存的标记（2026-09 全量核对）：
#   汽油 26933 台：纯电续航 0 条有值、百公里耗电量 0、充电时间 0；
#                  但 NEDC 油耗有 15906 条 —— 剩下的空属于 "未公布"
#   纯电动 5744 台：NEDC 油耗 0、发动机功率 0 —— 油类属性对该车不存在
#   插电式混合动力 / 增程式：电、油两类都有覆盖，不做 "不适用" 判定
#
# 之所以不落库："不适用" 完全可由 能源类型 + 字段类别 推出，存下来只会多一份
# 可能与能源类型不一致的冗余数据（PPT 上 7077 与 7047 的分歧就是这样来的）。
# ---------------------------------------------------------------------------

# 目前只有电类列会被投影出来（`query_by_energy_type` / `compare_models` 的「纯电续航」），
# 所以范围表只保留这一列。下面这些油类列当前没有任何查询返回，规则留在注释里，
# 将来加列时一并加回（实测依据同上一段的汽油车数据）：
#   油类（汽油 / 其他）：NEDC综合油耗、WLTC综合油耗、发动机最大功率、发动机最大扭矩
#   电类（纯电动 / 插混 / 增程）：百公里耗电量、快充时间、慢充时间
ELECTRIC_ENERGY = frozenset({"纯电动", "插电式混合动力", "增程式"})

# 回显列 -> 该列语义成立的能源范围；能源不在范围内且该列为空，判定为"不适用"
COLUMN_ENERGY_SCOPE = {
    "纯电续航": ELECTRIC_ENERGY,
}

# 缺失文案只有三种，与文档、系统提示词中的三态一一对应：
#   暂无报价 —— 价格类缺失（PRICE_COLUMNS）
#   未公布   —— 本该有值而厂商没给（其余所有列，DEFAULT_MISSING_LABEL）
#   不适用   —— 属性对这类车根本不存在（由能源范围判定，不看列名）
# 不再逐列铺开：16 个结果列里只有「指导价 / 价格区间」固定是「暂无报价」，「纯电续航」
# 可能因能源类型变成「不适用」，其余 13 列一律是「未公布」——按列罗列只会让规则表与
# 真实结果列脱节（原先就存在 3 个永不投影的键，以及「未收录」这种无人可见的第四种词）。
PRICE_COLUMNS = frozenset({"指导价", "价格区间"})
PRICE_MISSING_LABEL = "暂无报价"
DEFAULT_MISSING_LABEL = "未公布"
NOT_APPLICABLE_LABEL = "不适用"

UNPUBLISHED = "未公布"
NOT_APPLICABLE = "不适用"


def missing_reason(column: str, record: dict) -> str | None:
    """某列在这条记录上的缺失类型；``None`` 表示有值。

    "不适用"需要能源类别作为上下文，所以输入是整条记录而不是单个值。
    """
    if not is_missing(record.get(column)):
        return None
    scope = COLUMN_ENERGY_SCOPE.get(column)
    energy = record.get(ENERGY_SOURCE_FIELD)
    if scope and isinstance(energy, str) and energy.strip() and energy.strip() not in scope:
        return NOT_APPLICABLE
    return UNPUBLISHED


def missing_label(column: str, reason: str) -> str:
    """把缺失类型翻成回显文案：只可能得到三种文案之一。"""
    if reason == NOT_APPLICABLE:
        return NOT_APPLICABLE_LABEL
    if column in PRICE_COLUMNS:
        return PRICE_MISSING_LABEL
    return DEFAULT_MISSING_LABEL


def render_missing(records: list[dict]) -> tuple[list[dict], dict]:
    """把结果里的空值换成按字段区分的文案，并统计每列的缺失构成。

    返回 ``(渲染后的记录, 缺失统计)``，输入记录不会被修改。渲染后原本参与比较
    的数值也变成文案，因此**排序必须在此之前完成**；调用方要保证先排序再渲染。

    ``缺失统计`` 的键就是数据里实际看到的那段文案（``暂无报价`` / ``未公布`` /
    ``不适用``），因此统计与回显是同一个值，不存在两套说法。
    """
    rendered: list[dict] = []
    summary: dict[str, dict[str, int]] = {}
    for record in records:
        row = dict(record)
        for column in record:
            reason = missing_reason(column, record)
            if reason is None:
                continue
            label = missing_label(column, reason)
            row[column] = label
            bucket = summary.setdefault(column, {})
            bucket[label] = bucket.get(label, 0) + 1
        rendered.append(row)
    return rendered, summary
