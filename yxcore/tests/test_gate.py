"""第 2 期验收：对账闭合、诊断带数字。

对账的三种结果不许混淆（reconcile.py 的文档串）：
  通过（有证据）/ 违例（有反证）/ 已生效（旋钮到位但证明不了最优）。
「未核对」保留给真查不出来的情况，且照样拦下。
"""
from __future__ import annotations

import pytest

from yxcore import PROJECT_ROOT, solver_modules
from yxcore.patch import parse_patch
from yxcore.pipeline import YxError, run
from yxcore.reconcile import Ctx, audit_coverage, reconcile


def P(**kw):
    patch, error = parse_patch(kw)
    assert patch is not None, error
    return patch


@pytest.fixture(scope="module")
def snapshot():
    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    return data, options


def test_every_registered_reconciler_is_implemented():
    """注册即承诺的守门员：登记表里的对账函数名必须都有实现。"""
    assert audit_coverage() == []


def test_reconcile_passes_with_evidence(snapshot):
    data, options = snapshot
    window = data.windows[0]["编号"]
    patch = P(slot="bucket_min", op="=", value=1, utterance="窗口内必须修叶片",
              index={"桶": {"kind": "窗口", "value": window},
                     "组": {"kind": "部件", "value": "叶片"}})
    row = {"任务编号": "任务001", "风机编号": "风机001", "部件编号": "部件01",
           "部件名称": "叶片", "严重等级": "高", "窗口编号": window,
           "开始": 33600, "完成": 35360, "作业线": 1}
    audit = reconcile([patch], Ctx(selected=[row], rec={}, data=data,
                                   base_data=data, info={}, request=None))
    assert not audit["违例"] and not audit["未核对"]
    assert audit["通过数"] == 1
    assert "排了 1 个" in audit["明细"][0]["说明"]


def test_reconcile_flags_violation_with_numbers(snapshot):
    """空产物 + 至少修 1 个 → 必须是违例，不是未核对。"""
    data, options = snapshot
    patch = P(slot="count_min", op="=", value=1, utterance="叶片至少修一个",
              index={"组": {"kind": "部件", "value": "叶片"}})
    audit = reconcile([patch], Ctx(selected=[], rec={}, data=data,
                                   base_data=data, info={}, request=None))
    assert audit["违例"] and "只排了 0 个" in audit["违例"][0]


def test_objective_is_applied_not_passed():
    """目标类只能到「已生效」，不许标成「通过」——单份产物证明不了最优性。"""
    from yxcore.apply import compile_patches
    from yxcore import solver_modules

    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
    patch = P(slot="strategy", op="=", value="高风险优先抢修", strength="soft", utterance="高危优先")
    request, _ = compile_patches([patch], data, data)
    audit = reconcile([patch], Ctx(selected=[], rec={}, data=data, base_data=data,
                                   info={"策略": request.strategy}, request=request))
    item = audit["明细"][0]
    assert item["结果"] == "已生效"
    assert "证明不了最优" in item["说明"]


def test_infeasible_gives_reason_with_numbers():
    """真正被证明不可行时，诊断要指认到补丁，不许只说「可能冲突」。

    用例换成「全部任务 24 号前干完」：期限收紧过的任务全部进必修名单，
    窗口还没开就到期，求解器能证明 INFEASIBLE。预算上限那类约束在新模型里
    已经不是不可行，改由下面那条用例判「优雅少修」。
    """
    patches = [P(slot="deadline_delta", op="=", value="2026-10-24T00:00:00+08:00",
                 utterance="全部24号前干完", index={"组": {"kind": "全部"}})]
    with pytest.raises(YxError) as exc:
        run(patches, seconds=15)
    message = str(exc.value)
    assert "排不出" in message or "不可行" in message
    assert "全部24号前干完" in message          # 指认到具体哪一条
    assert "未能定位" not in message.split("\n")[0]


def test_budget_shortage_is_graceful_partial_not_infeasible():
    """资源预算不够不再让整模型不可行，少修要出解、要告警、要能逐个查 P。"""
    patches = [P(slot="budget_max", op="=", value=10, utterance="租船最多10艘天",
                 index={"口径": {"kind": "budget_口径", "value": "租船天"}})]
    result = run(patches, seconds=15)
    recommended = result["推荐"] or {}
    total = recommended["待修总数"]
    done = len(result["排程"])
    assert 0 < done < total, "预算是硬约束，修几个是自由变量，不该退化成全修或零修"
    assert result["部分排程"] is True
    assert any(f"其余 {total - done} 个这轮不修" in w for w in result["警告"])
    penalty = (result.get("模型产物") or {}).get("unrepairedPenalty") or {}
    assert penalty and penalty["horizonDays"] == 14
    assert len(penalty["records"]) == total
    assert all(r["不修惩罚P（元）"] > 0 for r in penalty["records"])


def test_end_to_end_reconciled_run(snapshot):
    data, options = snapshot
    window = data.windows[0]["编号"]
    patches = [
        P(slot="bucket_min", op="=", value=1, utterance="窗口内必须修叶片",
          index={"桶": {"kind": "窗口", "value": window},
                 "组": {"kind": "部件", "value": "叶片"}}),
        P(slot="船租", op="*", value=1.2, utterance="船租涨两成"),
    ]
    result = run(patches, seconds=15)
    assert result["排程"]
    assert "[通过]" in result["对账"]
    assert result["对账"].count("[违例]") == 0
    assert result["对账"].count("[未核对]") == 0
