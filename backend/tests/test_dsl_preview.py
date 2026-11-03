"""变更预览必须无损：DSL 里有几个字段，预览里就得看见几个。

为什么这条现在才重要：预览已经改成**原样输出**（solve/submit_change 进
PASSTHROUGH_TOOLS + AUTHORITATIVE_TOOLS），模型不许转述、不许加工。
以前模型会顺手把"严重任务完成数 >= 3"补全成一句人话，预览漏字段还可能被
补救；现在预览就是用户看到的全部，**预览的每一处省略都是用户批准的盲区**。

真出过的三处偏差（都是预览少说、实际多做）：
  - cardinality 漏 filter：写"完成数 >= 3"，实际是"严重任务完成数 >= 3"
  - precedence 漏 gap：用户不知道还卡了间隔
  - co_occur 漏 mode：六种模式（同天/异天/同窗口/异窗口/同线/异线）在预览里
    长得一模一样，连没实现的 diff_window 也显示成已实现的样子
"""
from __future__ import annotations

import pytest

from app.core.change_planning import format_dsl_preview


def _body(dsl: dict) -> str:
    return (
        format_dsl_preview(dsl)
        .split("变更预览：")[1]
        .split("这次会生成")[0]
        .strip()
    )


def test_cardinality_preview_keeps_filter() -> None:
    """“严重任务至少完成 3 个”不能缩成“完成数 >= 3”。"""
    body = _body(
        {"constraints": [{"type": "cardinality", "filter": {"严重度": "严重"}, "op": ">=", "value": 3}]}
    )

    assert "严重度=严重" in body, "filter 丢了，用户会以为这是全部任务的完成数"
    assert ">= 3" in body


def test_precedence_preview_keeps_gap() -> None:
    """先后顺序带间隔时，间隔是实际生效的约束，必须写出来。"""
    body = _body(
        {"constraints": [{"type": "precedence", "before": "任务001", "after": "任务002", "gap": 2880}]}
    )

    assert "任务001" in body and "任务002" in body
    assert "2880" in body, "gap 丢了：用户批准了一个自己没看见的间隔约束"


def test_co_occur_preview_distinguishes_all_six_modes() -> None:
    """六种模式必须两两可区分——以前全都渲染成“互斥（不同天/同时）”。"""
    modes = ["same_day", "diff_day", "same_window", "diff_window", "same_line", "diff_line"]
    rendered = {
        mode: _body({"constraints": [{"type": "co_occur", "mode": mode, "tasks": ["任务001", "任务002"]}]})
        for mode in modes
    }

    assert len(set(rendered.values())) == len(modes), "有两种模式在预览里同形，用户分不出来"


def test_unimplemented_primitive_says_so_before_confirmation() -> None:
    """登记了但没实现编译的原语，预览当场说明会报错，不能等点确认才炸。

    用 no_overlap：它是唯一还留在"登记但未实现"里的原语（按当前资源维度
    做出来是恒真约束，见 primitives.UNIMPLEMENTED_REASONS）。
    """
    body = _body(
        {"constraints": [{"type": "no_overlap", "resource": "line", "tasks": ["任务001", "任务002"]}]}
    )

    assert "未实现" in body and "不会生效" in body
    assert "不改变解" in body, "要说清不做它的原因，而不是只说'未实现'"


def test_unimplemented_co_occur_mode_says_so() -> None:
    body = _body(
        {"constraints": [{"type": "co_occur", "mode": "diff_window", "tasks": ["任务001", "任务002"]}]}
    )

    assert "未实现" in body


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "must", "task": "任务001"},
        {"type": "must", "filter": {"严重度": "严重"}},
        {"type": "exclude", "task": "任务009"},
        {"type": "at_most_one", "tasks": ["任务001", "任务002"]},
        {"type": "at_least_one", "tasks": ["任务001", "任务002"]},
        {"type": "precedence", "before": "任务001", "after": "任务002", "gap": 1440},
        {"type": "co_occur", "mode": "same_day", "tasks": ["任务001", "任务002"]},
        {"type": "no_overlap", "resource": "船", "tasks": ["任务001", "任务002"]},
        {"type": "capacity", "resource": "船", "time": "2026-11-03", "limit": 2},
        {"type": "fix", "task": "任务001", "line": 2},
        {"type": "cardinality", "filter": {"严重度": "高"}, "op": ">=", "value": 4},
        {"type": "deadline", "task": "任务001", "time": "2026-11-08T08:00:00+08:00"},
        {"type": "earliest", "task": "任务001", "time": "2026-11-03T08:00:00+08:00"},
        {"type": "forbidden_dates", "dates": ["2026-11-05", "2026-11-06"]},
        {"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 800000},
        {"type": "linear", "terms": [[1, "任务001"], [2, "任务002"]], "op": "<=", "value": 5},
    ],
)
def test_no_field_is_dropped_by_the_renderer(spec: dict) -> None:
    """总闸：15 种约束的每个字段值，都得在预览文本里露面。

    这条不检查语义好看不好看，只检查“有没有说”。以后新增原语忘了改渲染器，
    预览会退化成“原始字段：{...}”补丁——难看，但不会再静默丢约束。
    """
    body = _body({"constraints": [spec]})

    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from leaves(item)
        else:
            yield value

    for key, value in spec.items():
        if key == "type":
            continue
        for leaf in leaves(value):
            assert str(leaf) in body, f"{spec['type']} 的 {key}={leaf} 没出现在预览里：{body}"


def test_preview_has_no_raw_json_patch_for_known_primitives() -> None:
    """已实现的 11 种原语不该出现“原始字段”补丁——出现就说明渲染器又漏了。"""
    specs = [
        {"type": "must", "task": "任务001"},
        {"type": "exclude", "task": "任务009"},
        {"type": "precedence", "before": "任务001", "after": "任务002", "gap": 60},
        {"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]},
        {"type": "fix", "task": "任务001", "line": 2},
        {"type": "cardinality", "filter": {"严重度": "高"}, "op": ">=", "value": 4},
        {"type": "deadline", "task": "任务001", "time": "2026-11-08"},
        {"type": "earliest", "task": "任务001", "time": "2026-11-03"},
        {"type": "forbidden_dates", "dates": ["2026-11-05"]},
        {"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 800000},
        {"type": "linear", "terms": [[1, "任务001"]], "op": "<=", "value": 5},
    ]

    for spec in specs:
        body = _body({"constraints": [spec]})
        assert "原始字段" not in body, f"{spec['type']} 渲染不完整：{body}"
