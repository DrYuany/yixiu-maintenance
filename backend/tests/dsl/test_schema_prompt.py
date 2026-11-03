"""schema_prompt：注册表 → LLM 槽位说明书。"""
from __future__ import annotations

from dsl_core.registry import parameters as param_reg
from dsl_core.registry import schema_prompt as sp


def test_catalog_lists_every_changeable_param() -> None:
    catalog = sp.param_catalog()
    changeable = [r["参数名称"] for r in param_reg._rows() if param_reg.is_changeable(r["参数名称"])]
    assert changeable, "目录不能为空"
    for name in changeable:
        # 能写进 parameters 的才列在参数目录里；换节写的另有一块。
        if param_reg.route_of(name) == "parameters":
            assert name in catalog


def test_catalog_excludes_fixed_params() -> None:
    catalog = sp.param_catalog()
    fixed = [r["参数名称"] for r in param_reg._rows() if not param_reg.is_changeable(r["参数名称"])]
    assert fixed, "注册表应存在不可改参数，否则本测试无意义"
    for name in fixed:
        assert name not in catalog


def test_retrieve_finds_daily_hours_slot_from_colloquial_phrase() -> None:
    names = sp.retrieve_params("让人员猛干，每天能干12小时")
    assert "每日有效维修上限" in names


def test_retrieve_finds_ship_rent_slot() -> None:
    names = sp.retrieve_params("船租翻倍了，重新排一下")
    assert "船舶日租金" in names


def test_build_prompt_has_contract_and_blocks() -> None:
    prompt = sp.build_system_prompt("每天最多干12小时")
    assert "## 输出格式" in prompt
    assert "## 约束原语" in prompt
    assert "## 输入数据字段" in prompt
    assert "## 可引用实体" in prompt
    # 检索到的槽位应展开详解
    assert "每日有效维修上限" in prompt


def test_prompt_size_is_bounded() -> None:
    """分层注入的意义：控制体积。全量详述约 6.3k，分层后应明显小于它。"""
    prompt = sp.build_system_prompt("每天最多干12小时，并且001和002不要同一天")
    assert len(prompt) < 6000, f"prompt 过大：{len(prompt)}"


def test_snapshot_block_lists_entities_without_numbers() -> None:
    class _Snap:
        tasks = [
            {"任务编号": "任务001", "风机编号": "风机001"},
            {"任务编号": "任务002", "风机编号": "风机002"},
        ]
        parts = {"部件01": {"部件名称": "叶片"}, "部件02": {"部件名称": "齿轮箱"}}
        observation_windows = [{"编号": "窗口01"}, {"编号": "窗口02"}]

    block = sp.snapshot_block(_Snap())
    assert "任务001~任务002" in block
    assert "叶片/齿轮箱" in block
    assert "风机001~风机002" in block
    # 不应出现成本/风速这类具体数值
    assert "元" not in block


def test_context_block_includes_plan_and_pending() -> None:
    block = sp.context_block({
        "plan_label": "方案02",
        "schedule": [{"task": "任务001", "start": "11-05"}],
        "pending": "任务003 最早 11-07 开工",
    })
    assert "方案02" in block
    assert "任务001" in block
    assert "未确认变更" in block


def test_system_prompt_marks_unimplemented_primitives() -> None:
    block = sp.primitive_block()
    # no_overlap / capacity 登记了但未实现，必须显式标注，避免 LLM 使用后被校验器打回
    assert "no_overlap" in block
    assert "未实现" in block
