"""复合需求的不变量：合起来才矛盾的那些，必须在进求解器之前就被算出来。

这一批全是「单条都合法、合起来做不到」的用例。它们此前要么被放过（到求解器才炸），
要么被静默地按错的口径执行（「高危备件费」管了全部备件费）——后者比拒绝更危险。
"""
from __future__ import annotations

import pytest

from yxcore import PROJECT_ROOT, solver_modules
from yxcore.apply import compile_patches
from yxcore.patch import normalize_patch, parse_patch
from yxcore.pipeline import YxError, run
from yxcore.reconcile import Ctx, reconcile
from yxcore.validate import validate


def P(**kw):
    patch, error = parse_patch(kw)
    assert patch is not None, error
    return normalize_patch(patch)


def G(kind, value=None):
    return {"kind": kind, "value": value}


@pytest.fixture(scope="module")
def snapshot():
    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    return data, options


# ── 预算的「组」不是装饰 ────────────────────────────────────────────────

def test_budget_group_is_honored_in_compile_and_reconcile(snapshot):
    data, options = snapshot
    patch = P(slot="budget_max", value=300000, utterance="高危备件费不超30万",
              index={"口径": G("budget_口径", "备件"), "组": G("严重度", "高")})
    errors, _ = validate([patch], data, options)
    assert not errors
    result = run([patch], seconds=15)
    text = result["对账"]
    assert "高严重度的任务的备件" in text          # 点名了是哪一格
    assert "整轮的备件" not in text


def test_budget_cash_by_group_is_refused(snapshot):
    """内核只有一个现金口袋，只能整轮。硬记成整轮上限就是「生效了但生效错了」。"""
    data, options = snapshot
    patch = P(slot="budget_max", value=800000, utterance="高危现金不超80万",
              index={"口径": G("budget_口径", "现金"), "组": G("严重度", "高")})
    assert not validate([patch], data, options)[0]
    with pytest.raises(YxError) as exc:
        run([patch], seconds=10)
    assert "只能按整轮设" in str(exc.value)


def test_budget_rental_days_by_group_is_refused(snapshot):
    data, options = snapshot
    patch = P(slot="budget_max", value=5, utterance="高危租船最多5天",
              index={"口径": G("budget_口径", "租船天"), "组": G("严重度", "高")})
    with pytest.raises(YxError) as exc:
        run([patch], seconds=10)
    assert "船·天" in str(exc.value)


# ── 交叉静态预检 ────────────────────────────────────────────────────────

def test_scope_narrows_static_count(snapshot):
    """只修叶片（2 个任务）却要求至少 3 个——不用求解器就该拦。"""
    data, options = snapshot
    patches = [P(slot="scope_only", utterance="只修叶片", index={"范围": G("部件", "叶片")}),
               P(slot="count_min", value=3, utterance="至少修3个", index={"组": G("全部")})]
    errors, _ = validate(patches, data, options)
    assert errors and "最多只有 2 个" in errors[0]


def test_two_scopes_with_empty_intersection(snapshot):
    data, options = snapshot
    patches = [P(slot="scope_only", utterance="只修叶片", index={"范围": G("部件", "叶片")}),
               P(slot="scope_only", utterance="只修齿轮箱", index={"范围": G("部件", "齿轮箱")})]
    errors, _ = validate(patches, data, options)
    assert errors and "没有任何任务剩下" in errors[0]


def test_min_over_max_same_cell(snapshot):
    data, options = snapshot
    patches = [P(slot="count_min", value=4, utterance="高危全修", index={"组": G("严重度", "高")}),
               P(slot="count_max", value=2, utterance="高危最多2个", index={"组": G("严重度", "高")})]
    errors, _ = validate(patches, data, options)
    assert errors and "自相矛盾" in errors[0]
    assert "高严重度的任务" in errors[0]


def test_unresolvable_group_does_not_report_zero(snapshot):
    """组没读懂时说「这一格 0 个任务」会把人引去改一个没问题的数。"""
    data, options = snapshot
    patch = P(slot="count_min", value=1, utterance="叶片备件至少1个",
              index={"组": G("部件", "叶片备件")})
    errors, _ = validate([patch], data, options)
    assert any("不在部件库里" in e for e in errors)
    assert not any("最多只有 0 个" in e for e in errors)


# ── 归一化在入口，不在翻译层独占 ────────────────────────────────────────

def test_english_kind_aliases(snapshot):
    data, options = snapshot
    patch = P(slot="count_min", value=4, utterance="高危全修",
              index={"组": G("severity", "高")})
    assert patch.index["组"]["kind"] == "严重度"
    assert not validate([patch], data, options)[0]


def test_run_normalizes_at_entry():
    """同一句话从翻译层进、从脚本手搓进，必须落在同一格。"""
    raw = parse_patch({"slot": "count_min", "value": 1, "utterance": "高危至少1个",
                       "index": {"组": {"kind": "severity", "value": "高"}}})[0]
    result = run([raw], seconds=10)
    assert result["排程"]
    assert "高严重度的任务" in result["对账"]


# ── 部分排程必须说出来 ──────────────────────────────────────────────────

def test_implicit_partial_is_announced():
    """只修一个任务 → 只排 1 个，且把「其余几个没修」按当轮真实任务数说出来。

    告警里的数字从产物推，别写死 12：告警台账按天累计，10-23 时点是 10 个任务、
    11-03 时点是 13 个，写死就会变成一条随日期漂移的假失败。
    """
    patch = P(slot="scope_only", utterance="只修任务001", index={"范围": G("任务", "任务001")})
    result = run([patch], seconds=10)
    total = result["推荐"]["待修总数"]
    assert len(result["排程"]) == 1
    assert result["部分排程"] is True
    assert any(f"其余 {total - 1} 个这轮不修" in w for w in result["警告"])


def test_scope_reconcile_names_the_scope(snapshot):
    data, options = snapshot
    patch = P(slot="scope_only", utterance="只修叶片", index={"范围": G("部件", "叶片")})
    row = {"任务编号": "任务001", "风机编号": "风机001", "部件编号": "部件01",
           "部件名称": "叶片", "严重等级": "高", "窗口编号": "窗口03",
           "开始": 33600, "完成": 35360, "作业线": 1}
    audit = reconcile([patch], Ctx(selected=[row], rec={}, data=data,
                                   base_data=data, info={}, request=None))
    assert "只修「叶片的任务」" in audit["明细"][0]["说明"]


# ── 闭包报错不许崩成 traceback ──────────────────────────────────────────

def test_closure_error_becomes_user_message(snapshot):
    """注入口的错是内核拼成 ValueError 抛的；不接住就是一串 traceback。"""
    data, options = snapshot
    patch = P(slot="budget_max", value=5, utterance="高危租船最多5天",
              index={"口径": G("budget_口径", "租船天"), "组": G("严重度", "高")})
    _request, errors = compile_patches([patch], data, data)
    assert not errors  # 编译层放行，错在注入口闭包里
    with pytest.raises(YxError) as exc:
        run([patch], seconds=10)
    assert exc.value.code == "编译"


def test_探针预算用完要如实说没试完(tmp_path, monkeypatch):
    """诊断探针有总预算：超了就得说「还有几条没试」，不能装作都查过了。"""
    import json

    import yxcore.diagnose as D
    from yxcore import PROJECT_ROOT, solver_modules
    from yxcore.patch import parse_patch

    data = solver_modules()["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: solver_modules()["candidates"](data, t) for t in data.tasks}
    # 按本文件位置定位：从仓库根跑 pytest 时 cwd 不是 yxcore/，
    # 写死 "eval/cases_e2e.json" 会 FileNotFoundError（既有的第 6 条基线失败）。
    from pathlib import Path
    case_file = Path(__file__).resolve().parents[1] / "eval" / "cases_e2e.json"
    with case_file.open(encoding="utf-8") as handle:
        case = [c for c in json.load(handle) if "隔半天" in c["名"]][0]
    patches = [parse_patch(p)[0] for p in case["补丁"]]

    monkeypatch.setattr(D, "PROBE_TOTAL_SECONDS", 0)
    text = D.explain_infeasible(patches, data, options, {"解状态": "INFEASIBLE"}, None, seconds=5)
    assert "没试" in text or "没跑成" in text
    assert "不能当成「没问题」" in text


def test_探针预算用完要如实说没试完(tmp_path, monkeypatch):
    """诊断探针有总预算：超了就得说「还有几条没试」，不能装作都查过了。"""
    import json

    import yxcore.diagnose as D
    from yxcore import PROJECT_ROOT, solver_modules
    from yxcore.patch import parse_patch

    data = solver_modules()["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: solver_modules()["candidates"](data, t) for t in data.tasks}
    # 按本文件位置定位：从仓库根跑 pytest 时 cwd 不是 yxcore/，
    # 写死 "eval/cases_e2e.json" 会 FileNotFoundError（既有的第 6 条基线失败）。
    from pathlib import Path
    case_file = Path(__file__).resolve().parents[1] / "eval" / "cases_e2e.json"
    with case_file.open(encoding="utf-8") as handle:
        case = [c for c in json.load(handle) if "隔半天" in c["名"]][0]
    patches = [parse_patch(p)[0] for p in case["补丁"]]

    monkeypatch.setattr(D, "PROBE_TOTAL_SECONDS", 0)
    text = D.explain_infeasible(patches, data, options, {"解状态": "INFEASIBLE"}, None, seconds=5)
    assert "没试" in text or "没跑成" in text
    assert "不能当成「没问题」" in text


def test_手搓裸索引补丁不许崩成traceback():
    """两条入口（run 和会话）都得把形状问题说成一句话，不是 AttributeError。"""
    from yxcore.patch import Patch
    from yxcore.pipeline import YxError, run
    from yxcore.session import Session

    bad = Patch(slot="count_max", index={"组": "全部"}, op="=", value=8, strength="hard")
    try:
        run([bad], seconds=2)
    except YxError as exc:
        assert exc.code == "校验" and "要说清是哪一类" in "".join(exc.errors)
    else:
        raise AssertionError("裸索引居然跑通了")
    turn = Session().apply([bad])
    assert turn.错误 and "要说清是哪一类" in turn.错误[0]
