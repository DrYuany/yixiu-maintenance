"""预览是契约：预览里说的每一句，都要能在真实求解产物里核出来。

这是 `PASSTHROUGH_TOOLS` 改动的验收条件。预览改成原样输出之后，用户看到的
就是系统给的这份文字；如果预览写了"排除任务009"、产物里却排进去了，用户
没有任何办法发现——以前至少还有模型转述可以对照（虽然它也会编）。

所以这里不做单元测试那种"文本里有没有某个词"，而是**从预览里把断言抽出来，
真求解一遍，再拿产物去对**。对不上就是"说一套做一套"，测试直接红。

跑法：``py -m pytest tests/dsl/test_preview_contract.py -v``（真求解，约 1 分钟）
"""
from __future__ import annotations

import re

import pytest

from app.core.change_planning import format_dsl_preview
from dsl_core.runner import run

pytestmark = pytest.mark.slow


def _preview_lines(dsl: dict) -> str:
    return format_dsl_preview(dsl)


def test_preview_exclusions_really_disappear_from_the_schedule() -> None:
    """预览说排除的，产物里一个都不能有。"""
    dsl = {
        "constraints": [
            {"type": "exclude", "task": f"任务{i:03d}"} for i in (9, 10, 11)
        ]
    }
    preview = _preview_lines(dsl)
    claimed = set(re.findall(r"排除：(任务\d+)", preview))
    assert claimed == {"任务009", "任务010", "任务011"}, preview

    report = run(dsl, seconds=60)

    scheduled = {row["任务编号"] for row in (report["排程"] or [])}
    assert scheduled, "预览说能改，求解却什么都没排出来"
    leaked = claimed & scheduled
    assert not leaked, f"预览说排除了 {sorted(claimed)}，产物里却排了 {sorted(leaked)}"


def test_preview_cardinality_bound_holds_in_the_schedule() -> None:
    """预览写“完成数 >= 9”，产物里就得真有至少 9 个任务。"""
    dsl = {"constraints": [{"type": "cardinality", "filter": {}, "op": ">=", "value": 9}]}
    preview = _preview_lines(dsl)
    match = re.search(r"完成数 >= (\d+)", preview)
    assert match, preview
    bound = int(match.group(1))

    report = run(dsl, seconds=60)

    scheduled = report["排程"] or []
    assert len(scheduled) >= bound, f"预览承诺完成 {bound} 个，实际只排了 {len(scheduled)} 个"


def test_preview_line_assignment_really_lands_on_that_line() -> None:
    """预览写“只上 2 号线”，产物里这条任务的作业线就必须是 2。"""
    dsl = {"constraints": [{"type": "fix", "task": "任务001", "line": 2}]}
    preview = _preview_lines(dsl)
    match = re.search(r"(任务\d+) 只上 (\d+) 号线", preview)
    assert match, preview
    task, line = match.group(1), int(match.group(2))

    report = run(dsl, seconds=60)

    rows = [row for row in (report["排程"] or []) if row["任务编号"] == task]
    assert rows, f"预览说 {task} 只上 {line} 号线，产物里却没有这条任务"
    assert {row["作业线"] for row in rows} == {line}, "预览说的作业线没落到产物里"
