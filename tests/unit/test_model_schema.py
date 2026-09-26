import pytest

from src.db.model_schema import (
    NOT_APPLICABLE,
    UNPUBLISHED,
    is_missing,
    missing_label,
    missing_reason,
    normalize_model,
    render_missing,
)


def test_missing_recognises_every_source_spelling():
    for value in (None, "", "  ", "None", "NONE", "暂无", "暂无报价", "待查"):
        assert is_missing(value), value
    for value in ("油耗", "未知", 0, "0"):
        assert not is_missing(value), value


def test_normalize_drops_sentinels_so_missing_means_absent_property():
    normalized = normalize_model(
        {
            "车名": "测试车 2024款",
            "官方指导价": "暂无报价",
            "上市时间": "None",
            "最高车速(km/h)": "None",
            "energy_raw": None,
        }
    )

    assert normalized["车名"] == "测试车 2024款"
    assert "官方指导价" not in normalized
    assert "price_wan" not in normalized
    assert "上市时间" not in normalized
    assert "launch_ym" not in normalized
    assert "最高车速(km/h)" not in normalized
    assert "energy_raw" not in normalized


def test_normalize_keeps_raw_text_and_adds_comparable_numbers():
    normalized = normalize_model({"车名": "x", "官方指导价": "76.98万"})

    # 原文保留供展示，数值另存供比较：两者语义不同，不能互相替代
    assert normalized["官方指导价"] == "76.98万"
    assert normalized["price_wan"] == pytest.approx(76.98)


def test_power_splits_kilowatts_from_horsepower():
    normalized = normalize_model({"车名": "x", "最大功率(kW)": "294(400Ps)"})

    assert normalized["power_kw"] == 294
    assert normalized["power_ps"] == 400


def test_power_without_horsepower_keeps_only_kilowatts():
    normalized = normalize_model({"车名": "x", "最大功率(kW)": "155"})

    assert normalized["power_kw"] == 155
    assert "power_ps" not in normalized


def test_launch_time_becomes_sortable_integer():
    assert normalize_model({"车名": "x", "上市时间": "2017.09"})["launch_ym"] == 201709
    assert normalize_model({"车名": "x", "上市时间": "2023"})["launch_ym"] == 202300


def test_dimensions_split_into_millimetres():
    normalized = normalize_model({"车名": "x", "长x宽x高(mm)": "4194x1832x1367"})

    assert normalized["length_mm"] == 4194
    assert normalized["width_mm"] == 1832
    assert normalized["height_mm"] == 1367


def test_warranty_splits_years_from_kilometres():
    normalized = normalize_model({"车名": "x", "整车保修期限": "3年或10万公里"})

    assert normalized["warranty_years"] == 3
    assert normalized["warranty_km"] == 100000
    assert "warranty_km_unlimited" not in normalized


def test_warranty_marks_unlimited_instead_of_faking_a_number():
    unlimited_km = normalize_model({"车名": "x", "整车保修期限": "3年不限公里"})
    assert unlimited_km["warranty_years"] == 3
    assert unlimited_km["warranty_km_unlimited"] is True
    assert "warranty_km" not in unlimited_km

    unlimited_all = normalize_model({"车名": "x", "整车保修期限": "不限年限/不限里程"})
    assert unlimited_all["warranty_years_unlimited"] is True
    assert unlimited_all["warranty_km_unlimited"] is True


def test_charge_hours_are_keyed_by_fast_or_slow():
    normalized = normalize_model({"车名": "x", "充电时间(小时)": "快充0.5小时\n慢充8小时"})
    assert normalized["fast_charge_hours"] == pytest.approx(0.5)
    assert normalized["slow_charge_hours"] == pytest.approx(8.0)

    slow_only = normalize_model({"车名": "x", "充电时间(小时)": "慢充3小时"})
    assert slow_only["slow_charge_hours"] == pytest.approx(3.0)
    assert "fast_charge_hours" not in slow_only


def test_fast_charge_soc_handles_range_and_single_value_spellings():
    ranged = normalize_model({"车名": "x", "快充电量(%)": "30%-80%"})
    assert ranged["fast_charge_soc_low"] == 30
    assert ranged["fast_charge_soc_high"] == 80

    single = normalize_model({"车名": "x", "快充电量(%)": "80"})
    assert single["fast_charge_soc_high"] == 80
    assert "fast_charge_soc_low" not in single


def test_energy_keeps_both_canonical_class_and_raw_detail():
    normalized = normalize_model(
        {"车名": "x", "能源类型": "48V轻混系统"},
        energy_canonical="其他",
    )

    # 规范值用于与 ENERGY_TYPE_IS 关系边保持一致，原始细分值保住 15 类的信息
    assert normalized["能源类型"] == "其他"
    assert normalized["energy_raw"] == "48V轻混系统"


def test_source_id_is_preserved_for_relationship_binding():
    normalized = normalize_model({"车名": "x", "_source_id": "model-7"})
    assert normalized["_source_id"] == "model-7"


def test_same_null_reads_as_unavailable_or_unpublished_by_column():
    # 同一列「纯电续航」，汽油车的空是"不适用"（这车根本没有该属性）……
    assert missing_reason("纯电续航", {"品牌": "某牌", "能源类型": "汽油"}) == NOT_APPLICABLE
    # ……换成纯电车就是"未公布"（属性存在，厂商没给）
    assert missing_reason("纯电续航", {"能源类型": "纯电动"}) == UNPUBLISHED
    # 插混两类属性都存在，空只可能是未公布
    assert missing_reason("纯电续航", {"能源类型": "插电式混合动力"}) == UNPUBLISHED
    # 与能源无关的列永远不会是"不适用"
    assert missing_reason("保养成本", {"能源类型": "汽油"}) == UNPUBLISHED


def test_missing_reason_is_none_when_the_value_exists():
    assert missing_reason("指导价", {"能源类型": "汽油", "指导价": "12.30万"}) is None


def test_missing_reason_without_energy_context_stays_unpublished():
    assert missing_reason("纯电续航", {"品牌": "某牌"}) == UNPUBLISHED
    assert missing_reason("纯电续航", {"能源类型": None}) == UNPUBLISHED


def test_missing_labels_are_limited_to_the_three_documented_words():
    assert missing_label("指导价", UNPUBLISHED) == "暂无报价"
    assert missing_label("价格区间", UNPUBLISHED) == "暂无报价"
    # 价格以外的列一律"未公布"：不再有"暂无数据""未收录"这类第四、第五种说法
    assert missing_label("保养成本", UNPUBLISHED) == "未公布"
    assert missing_label("纯电续航", UNPUBLISHED) == "未公布"
    assert missing_label("没配过文案的列", UNPUBLISHED) == "未公布"
    # 不适用不看字段文案：它表达的是"这车没有这个属性"
    assert missing_label("保修年数", NOT_APPLICABLE) == "不适用"
    assert missing_label("指导价", NOT_APPLICABLE) == "不适用"


def test_render_missing_rewrites_cells_and_summarises_each_column():
    records = [
        {"品牌": "某牌", "能源类型": "汽油", "指导价": None, "纯电续航": None},
        {"品牌": "某牌", "能源类型": "汽油", "指导价": "12万", "纯电续航": None},
        {"品牌": "某牌", "能源类型": "纯电动", "指导价": None, "纯电续航": 500},
    ]

    rendered, summary = render_missing(records)

    assert rendered[0]["指导价"] == "暂无报价"
    assert rendered[0]["纯电续航"] == "不适用"
    assert rendered[1]["指导价"] == "12万"
    assert rendered[2]["纯电续航"] == 500
    # 统计的键就是数据里看到的那段文案，两者是同一个值
    assert summary["指导价"] == {"暂无报价": 2}
    assert summary["纯电续航"] == {"不适用": 2}


def test_render_missing_does_not_mutate_the_input_records():
    record = {"品牌": "某牌", "能源类型": "汽油", "指导价": None}
    render_missing([record])

    assert record["指导价"] is None


def test_render_missing_reports_nothing_when_no_column_is_empty():
    rendered, summary = render_missing([{"品牌": "某牌", "指导价": "12万"}])

    assert rendered == [{"品牌": "某牌", "指导价": "12万"}]
    assert summary == {}
