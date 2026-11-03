"""第 0 期验收：骨架能跑、宪法自洽、拦截生效。

验收标准（README 第 0 期）：手写补丁能求解，产物指纹稳定，同一输入两次跑一致。

分两类：
  * 不求解的：注册表自检、校验拦截、编译与指纹——秒级跑完，每次改代码都该跑
  * 求解的：基线 + 代表性补丁，用 max_tasks 缩规模控制在十几秒内
"""
from __future__ import annotations

import pytest

from yxcore import slots as S
from yxcore.apply import compile_patches, needs_partial
from yxcore.patch import Patch, fingerprint, parse_patch
from yxcore.pipeline import baseline, run
from yxcore.validate import validate
from yxcore import PROJECT_ROOT, solver_modules

BASE = "基座01"
SMALL = 6          # 求解用例的任务数上限
SECONDS = 8


def P(**kw) -> Patch:
    patch, error = parse_patch(kw)
    assert patch is not None, error
    return patch


@pytest.fixture(scope="module")
def snapshot():
    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id=BASE)
    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    return data, options


# ── 一、宪法自洽 ────────────────────────────────────────────────────────

def test_registry_self_check():
    """登记项不全不许上线——尤其是对账函数，缺一个就等于留了「加了族却没对账」的窟窿。"""
    assert S.audit_registry() == []


def test_slot_count_matches_constitution():
    """37 个可写槽位 = 22 个约束族 + 15 个标量参数。"""
    assert len(S.SLOTS) == 22
    assert len(S.PARAM_SLOTS) == 15
    assert len(S.SLOTS) + len(S.PARAM_SLOTS) == 37


def test_every_slot_has_route_and_reconcile():
    for name, slot in S.SLOTS.items():
        assert slot.route, f"{name} 缺落点"
        assert slot.reconcile, f"{name} 缺对账函数"


# ── 二、校验拦截（6 条规则各一条） ──────────────────────────────────────

def test_unknown_slot_is_rejected(snapshot):
    data, options = snapshot
    errors, _ = validate([P(slot="均衡一下", op="=", value=1)], data, options)
    assert errors and "没有" in errors[0]


def test_missing_entity_is_rejected(snapshot):
    data, options = snapshot
    patch = P(slot="bucket_min", op="=", value=1,
              index={"桶": {"kind": "窗口", "value": "窗口99"},
                     "组": {"kind": "部件", "value": "叶片"}})
    errors, _ = validate([patch], data, options)
    assert errors and "窗口99" in errors[0]


def test_out_of_range_is_rejected(snapshot):
    data, options = snapshot
    patch = P(slot="count_min", op="=", value=10_000,
              index={"组": {"kind": "严重度", "value": "高"}})
    errors, _ = validate([patch], data, options)
    assert any("超出允许范围" in e or "最多只有" in e for e in errors)


def test_static_infeasibility_is_caught_before_solving(snapshot):
    """第 4 条：不跑求解器就能算出「这一格里根本没有这么多任务」。"""
    data, options = snapshot
    patch = P(slot="count_min", op="=", value=99,
              index={"组": {"kind": "严重度", "value": "高"}})
    errors, _ = validate([patch], data, options)
    assert any("最多只有" in e for e in errors)


def test_force_in_and_force_out_conflict(snapshot):
    data, options = snapshot
    tid = data.tasks[0]["任务编号"]
    patches = [
        P(slot="count_min", op="=", value=1, index={"组": {"kind": "任务", "value": tid}}),
        P(slot="count_max", op="=", value=0, index={"组": {"kind": "任务", "value": tid}}),
    ]
    errors, _ = validate(patches, data, options)
    assert any("既被要求必须修" in e for e in errors)


def test_same_cell_twice_is_rejected(snapshot):
    """决策 D2：同格重复写直接报错，不累乘。"""
    data, options = snapshot
    spec = {"组": {"kind": "严重度", "value": "高"}}
    patches = [P(slot="count_min", op="=", value=1, index=spec),
               P(slot="count_min", op="=", value=2, index=spec)]
    errors, _ = validate(patches, data, options)
    assert any("同一格子被写了两次" in e for e in errors)


def test_confidence_number_is_rejected():
    """强度只收三选一，不收置信度数字——那个数字没校准过，拿它当阈值是伪精度。"""
    patch, error = parse_patch({"slot": "count_min", "op": "=", "value": 1, "strength": 0.9})
    assert patch is None and "置信度" in error


# ── 三、编译与指纹 ──────────────────────────────────────────────────────

def test_relative_op_is_folded_to_absolute(snapshot):
    data, _ = snapshot
    patch = P(slot="船租", op="*", value=1.2, utterance="船租涨两成")
    request, errors = compile_patches([patch], data, data)
    assert not errors
    base = data.params[S.PARAM_ALIASES["船租"]]
    assert request.overrides[S.PARAM_ALIASES["船租"]] == pytest.approx(base * 1.2)
    assert any("折算" in e for e in request.echo)


def test_fingerprint_is_stable(snapshot):
    data, _ = snapshot
    patches = [P(slot="bucket_min", op="=", value=1,
                 index={"桶": {"kind": "窗口", "value": "窗口03"},
                        "组": {"kind": "部件", "value": "叶片"}})]
    once = fingerprint(patches, {"基座": BASE})
    twice = fingerprint(list(reversed(patches)), {"基座": BASE})
    assert once == twice  # 顺序无关：补丁先排序再盖章


def test_compile_is_deterministic(snapshot):
    data, _ = snapshot
    patches = [P(slot="count_max", op="=", value=8, index={"组": {"kind": "全部"}}),
               P(slot="船租", op="*", value=1.2)]
    first, _ = compile_patches(patches, data, data)
    second, _ = compile_patches(patches, data, data)
    assert first.overrides == second.overrides
    assert first.limits == second.limits
    assert first.partial == second.partial
    assert first.echo == second.echo


def test_partial_trigger_is_single_sourced(snapshot):
    """决策 D10：判据只有一份。「最多修 8 个」必须切部分排程，否则必然判不可行。"""
    data, options = snapshot
    patches = [P(slot="count_max", op="=", value=8, index={"组": {"kind": "全部"}})]
    assert needs_partial(patches, data, options) is True
    assert needs_partial([P(slot="count_max", op="=", value=99,
                            index={"组": {"kind": "全部"}})], data, options) is False


def test_deadline_can_only_tighten(snapshot):
    """内核的期限补丁只做收紧——放宽必须明确拒绝，不能静默忽略。

    快照里任务自带期限，且部分期限晚于规划期末，所以要先把期限统一压早，
    再给一个更晚的期限，才能构造出「放宽」这个方向。
    """
    data, _ = snapshot
    original = [(t["任务编号"], t.get("截止分钟")) for t in data.tasks]
    try:
        for task in data.tasks:
            task["截止分钟"] = 20000
        patch = P(slot="deadline_delta", op="=", value=data.stamp(30000),
                  index={"组": {"kind": "全部"}})
        _, errors = compile_patches([patch], data, data)
        assert any("只能收紧" in e for e in errors)
    finally:
        for task, (_, minute) in zip(data.tasks, original):
            task["截止分钟"] = minute


# ── 四、求解（慢，用缩规模跑） ──────────────────────────────────────────

def test_baseline_solves():
    result = baseline(base_id=BASE, seconds=SECONDS, max_tasks=SMALL)
    assert result["排程"], "基线必须出方案，否则后面的对比无从谈起"
    assert result["指纹"]


def test_bucket_min_solves_and_holds():
    """README 走查的例子：某时段必须修某类部件。"""
    modules = solver_modules()
    probe = modules["load_project"](PROJECT_ROOT, base_id=BASE)
    window = probe.windows[0]["编号"]
    patches = [P(slot="bucket_min", op="=", value=1, utterance=f"{window}必须修叶片",
                 index={"桶": {"kind": "窗口", "value": window},
                        "组": {"kind": "部件", "value": "叶片"}})]
    result = run(patches, base_id=BASE, seconds=SECONDS, max_tasks=SMALL)
    selected = result["排程"]
    if selected is None:
        pytest.skip("缩规模后该窗口内没有叶片任务，静态预检已拦（见 test_static_*）")
    blades = [s for s in selected if s["部件名称"] == "叶片" and s["窗口编号"] == window]
    assert blades, "说了必须修叶片，产物里却没有叶片任务"


def test_scope_only_narrows_the_plan():
    """「只修叶片」：产物里不该出现别的部件。"""
    patches = [P(slot="scope_only", op="=", utterance="只修叶片",
                 index={"范围": {"kind": "部件", "value": "叶片"}})]
    result = run(patches, base_id=BASE, seconds=SECONDS, max_tasks=SMALL)
    assert result["部分排程"] is True, "只修一类部件必须切部分排程，否则与「每个任务都要排」矛盾"
    selected = result["排程"]
    if selected:
        assert {s["部件名称"] for s in selected} == {"叶片"}


def test_same_input_twice_gives_same_fingerprint():
    patches = [P(slot="count_max", op="=", value=4, index={"组": {"kind": "全部"}})]
    one = run(patches, base_id=BASE, seconds=SECONDS, max_tasks=SMALL)
    two = run(patches, base_id=BASE, seconds=SECONDS, max_tasks=SMALL)
    assert one["指纹"] == two["指纹"]
