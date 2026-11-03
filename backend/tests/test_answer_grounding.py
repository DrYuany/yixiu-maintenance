"""数字溯源：答案里的数字追不到工具结果就不能算「已核实」。

这些用例不看模型、不联网——只验证比较逻辑本身。真要端到端看效果，
是 conversation_agent 把工具返回的原样内容喂进来，由这里判哪些数字有出处。
"""
from __future__ import annotations

from app.core.answer_grounding import caveat_line, data_numbers, grounding_report

# 一份假的工具返回：船租 25000 元/天，待修 13 项，总成本 128.46 万元。
TOOL_PAYLOAD = '{"status":"ok","data":{"总成本":128.46,"船租":25000,"待修":13}}'


def test_numbers_from_tools_are_grounded() -> None:
    answer = "本版总成本 128.46 万元，船租 25000 元/天，还有 13 项待修。"
    report = grounding_report(answer, [TOOL_PAYLOAD])

    # 「13 项待修」里的 13 是光秃秃的小整数，刻意不算数据（见模块注释）。
    assert report["检查数字"] == 2
    assert report["未溯源"] == []
    assert caveat_line(report) == ""


def test_unsourced_numbers_are_flagged() -> None:
    answer = "本版总成本 128.46 万元，另外船租大概 48000 元/天。"
    report = grounding_report(answer, [TOOL_PAYLOAD])

    assert "48000 元" in report["未溯源"], "工具里没有 48000，必须标出来"
    assert "128.46 万元" not in report["未溯源"], "工具里有的不能误标"
    assert "没有找到出处" in caveat_line(report)


def test_numbers_the_user_supplied_are_not_fabrications() -> None:
    """用户自己报的数不算模型编的：「船租压到 8 万」里的 8 万有出处。"""
    report = grounding_report("已经把船租改成 8 万。", [TOOL_PAYLOAD, "把船租压到 8 万"])

    assert report["未溯源"] == []


def test_dates_and_ids_are_not_treated_as_data() -> None:
    """日期、编号、版本号里也有数字，但不该进「待核实」清单。"""
    answer = "2026-11-02 的方案01里，任务001 排在 11月8日 08:30 开工。"
    assert data_numbers(answer) == []


def test_unit_conversion_is_not_a_fabrication() -> None:
    """工具给 25000 元，人话写 2.5 万元——换单位不算编数字。"""
    report = grounding_report("船租 2.5 万元/天。", [TOOL_PAYLOAD])

    assert report["未溯源"] == []


def test_rounding_tolerance_is_accepted() -> None:
    """工具给 128.46，人话写 128.5——四舍五入不算没出处。"""
    report = grounding_report("总成本 128.5 万元。", [TOOL_PAYLOAD])

    assert report["未溯源"] == []


def test_bare_small_integers_are_not_data() -> None:
    """序号、条款号、「五项成本」这类光秃秃的小整数不进清单，
    否则每条回答都会被标一遍，用户很快就学会无视这个提示。"""
    answer = "分三步：1. 先读方案 2. 再对比 3. 最后确认。"
    assert data_numbers(answer) == []


def test_concept_answer_without_numbers_is_untouched() -> None:
    report = grounding_report("分层目标是字典序：先最优再次级，不走加权求和。", [TOOL_PAYLOAD])

    assert report["有未溯源"] is False
    assert caveat_line(report) == ""


def test_no_sources_means_everything_is_unsourced() -> None:
    """一个工具都没调（sources 为空），答案里的数字就都没有出处。"""
    report = grounding_report("总成本 128.46 万元。", [])

    assert report["未溯源"] == ["128.46 万元"]


def test_caveat_truncates_long_lists() -> None:
    report = {
        "未溯源": ["1 万元", "2 万元", "3 万元", "4 万元", "5 万元", "6 万元"],
    }
    line = caveat_line(report, limit=5)

    assert "6 万元" not in line, "超过上限的只报数量，不再逐个列"
    assert "等 6 处" in line
