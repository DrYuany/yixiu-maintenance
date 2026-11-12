"""「只修这几个」的排除语义：端到端结果断言。

大一修 v0.17 线上实测：只写「1~8 之和 = 8」，16 个任务照样排了 12 个——
「只做」是排除语义，必须把范围外压到 0，光有计数等式不够。

小一修的 `scope_only` 编译方式是**补集压零**（`closure.py:294-303`：
`m.Add(sum(补集) == 0)`），不是 `Σx = N`，所以那个陷阱在这里不成立。
本文件不是修 bug，而是把这件事**锁死成结果级断言**：
以后谁把排除语义改成计数、或把判分器改成只看个数，测试立刻红。

一个方法论要点：用例必须选得能区分两种实现。点名前 5 个任务时，
`count_max=5` 碰巧也排前 5 个（默认按代价排序同解），对照实验会假通过；
所以这里点名**后 5 个**任务，计数上限必然去挑代价更低的前 5 个。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "maintenance-milp-modeler" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["MWA_SOLVER_WORKERS"] = "1"

import product_planner  # noqa: E402
from product_data import load_project  # noqa: E402
from result_contract import check  # noqa: E402
from yxcore.patch import parse_patch  # noqa: E402
from yxcore.pipeline import run as yx_run  # noqa: E402

AS_OF = "2026-10-23T18:00:00+08:00"  # 台账首批 11 条，求解快
SECONDS = 10


def P(**kw):
    patch, error = parse_patch(kw)
    assert patch is not None, error
    return patch


@pytest.fixture(scope="module", autouse=True)
def single_worker():
    """和结果基线同一条件：多线程超时截断会取到不同中间解。"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(product_planner, "WORKERS", 1)
        yield


@pytest.fixture(scope="module")
def task_ids():
    data = load_project(ROOT, as_of=AS_OF)
    return [t["任务编号"] for t in data.tasks]


@pytest.fixture(scope="module")
def named_tasks(task_ids):
    """故意点名后 5 个，让「计数上限」与「补集压零」必定给出不同结果。"""
    return task_ids[-5:]


def _solve(patches):
    with tempfile.TemporaryDirectory() as out:
        return yx_run(patches, as_of=AS_OF, seconds=SECONDS, output=Path(out))


def test_scope_only_excludes_every_task_outside_the_list(named_tasks):
    """点名一串任务编号 → 结果里一个组外任务都不能有。"""
    patch = P(slot="scope_only", utterance="只修这五个任务",
              index={"范围": {"kind": "任务清单", "value": named_tasks}})
    result = _solve([patch])
    placed = {row["任务编号"] for row in result["排程"]}

    assert placed, "圈了 5 个任务，至少要排出方案"
    assert placed - set(named_tasks) == set(), "结果里混进了没点名的任务"
    assert check(result, {"只修任务": named_tasks}) == [], "结果契约应判定满足排除要求"


def test_count_max_is_not_an_exclusion(named_tasks):
    """对照：只写「最多修 5 个」不会排除组外任务，结果契约必须指出来。

    如果有人把「只修这几个」错编译成计数上限，这条就会暴露：
    求解器会去挑代价更低的前 5 个，而点名的是后 5 个。
    """
    patch = P(slot="count_max", op="=", value=5, index={"组": {"kind": "全部"}})
    result = _solve([patch])
    placed = {row["任务编号"] for row in result["排程"]}

    assert len(placed) <= 5, "count_max 本身应该生效（数量上限）"
    problems = check(result, {"只修任务": named_tasks})
    assert any("组外任务" in p for p in problems), (
        "计数上限碰巧也排出了点名的任务，对照用例失去区分力；"
        "应换一组默认不会被优先挑中的任务编号"
    )


def test_exclusion_and_required_are_orthogonal():
    """「只许这些」和「这些都要修」是两件事，别捆在一条断言里。

    scope_only 只压补集为 0，范围内任务仍可走 y=1 本轮不修；
    所以只修任务不检查「点名的没排上」，要检查就显式加「必修任务」。
    """
    from result_contract import check as _check

    all_ids = [f"任务{i:03d}" for i in range(1, 9)]
    rows = [{"任务编号": tid, "风机编号": "风机X", "部件名称": "齿轮箱",
             "严重等级": "高", "窗口编号": "窗口01", "作业线": 1,
             "开始": 0, "完成": 60} for tid in all_ids[:3]]
    result = {
        "排程": rows,
        "推荐": {"未安排": [{"任务编号": t} for t in all_ids[3:]],
                 "待修总数": len(all_ids), "总成本": 0},
        "求解": {}, "数据": {},
    }
    named = all_ids[:5]

    # 只排了 3 个，但没混进组外：排除要求通过。
    assert _check(result, {"只修任务": named}) == []
    # 点名的 5 个里有 2 个没排：这是「必修任务」该管的，且确实会报。
    required = _check(result, {"必修任务": named})
    assert any("必修任务没进方案" in p for p in required)
    # 两个一起用，才等价于「只做这 5 个且都要做」。
    assert any("必修任务没进方案" in p for p in _check(result, {"只修任务": named, "必修任务": named}))
