"""绝对优先 → count_min 硬约束：语义识别、形状复核、澄清推荐、模型里的证据。

整层不调大模型：翻译出口用 canned JSON 顶掉 ``_chat``，其余全部本地判定。
最后两条直接查 CP-SAT 模型 / 求解器，证明硬约束不是「回显里说说」。

背景：默认目标模式切到加权之后只有一个目标等式，``strategy`` 里的偏好允许被成本
换掉。用户说「必须全修」时只有写进可行域才算数，权重顶替就是「生效了但生效错了」。
"""
from __future__ import annotations

import json

import pytest

from yxcore import PROJECT_ROOT, solver_modules
from yxcore import absolute as A
from yxcore.apply import compile_patches
from yxcore.closure import build_closure
from yxcore.patch import duplicate_cells, normalize_patch, parse_patch
from yxcore.pipeline import YxError, run
from yxcore.session import Session

SPEC = {"kind": "严重度", "value": "高"}


def P(**kw):
    patch, error = parse_patch(kw)
    assert patch is not None, error
    return normalize_patch(patch)


def _count_min(patches):
    return next((p for p in patches if p.slot == "count_min"), None)


@pytest.fixture(scope="module")
def snapshot():
    modules = solver_modules()
    data = modules["load_project"](PROJECT_ROOT, base_id="基座01")
    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    return data, options


# ── 1. 语义按小句判，别把「尽量」做成「必须」 ──────────────────────────

def test_硬软语义按小句分开(snapshot):
    data, options = snapshot
    absolute = ["高危必须全修", "高危全都要修", "高危一个都不能少",
                "成本压到最低，但高风险的必须全修", "这轮砍到最多5个，但高危必须全在",
                "高危优先，同时高危一个都不能少"]
    soft = ["高危优先", "尽量把高危都修了", "整体按高风险抢修的思路排"]
    for utterance in absolute:
        plans = A._plans(utterance, data, options)
        assert plans, f"{utterance} 应当识别出绝对优先"
        assert plans[0]["组"] == SPEC, utterance
    for utterance in soft:
        assert not A._plans(utterance, data, options), f"{utterance} 是相对偏好，不该升级成硬约束"


def test_明写数字与带时段的话不按全修口径改(snapshot):
    data, options = snapshot
    for utterance in ["高危四个必须全修", "高危至少修3个", "11月第二周必须修叶片",
                      "这周风机003 的活全干完"]:
        assert not A._plans(utterance, data, options), utterance


def test_光说必须但没说全的不替用户把范围拧大(snapshot):
    """「齿轮箱必须修」可能只指其中一件；程序不许擅自补成「所有齿轮箱任务全修」。"""
    data, options = snapshot
    for utterance in ["齿轮箱必须修", "风机005 的活这轮必须干"]:
        assert not A._plans(utterance, data, options), utterance
        out = A.enforce(utterance, [P(slot="strategy", value="成本最低优先",
                                      utterance=utterance)], data, options)
        assert out.patches[0].slot == "strategy" and out.notes == []


def test_组规模来自快照(snapshot):
    data, options = snapshot
    n = A.group_size(SPEC, data, options)
    assert n > 0
    assert n == sum(1 for t in data.tasks
                    if t["严重等级"] == "高" and options.get(t["任务编号"]))
    patch = A.hard_patch(SPEC, n, "高危必须全修")
    assert patch.slot == "count_min" and patch.value == n and patch.strength == "hard"


# ── 2. 翻译出口的形状复核 ──────────────────────────────────────────────

def test_只落加权偏好时要补硬约束(snapshot):
    data, options = snapshot
    utterance = "高危必须全修"
    only_preference = [P(slot="strategy", value="高风险优先抢修", utterance=utterance)]
    out = A.enforce(utterance, only_preference, data, options)
    assert out.clarify is None
    assert len(out.patches) == 2
    cm = _count_min(out.patches)
    assert cm is not None and cm.value == A.group_size(SPEC, data, options)
    assert cm.index["组"] == SPEC and cm.strength == "hard"
    assert any("硬约束" in note for note in out.notes)
    assert not duplicate_cells(out.patches)


def test_全修被写成至少1个要按快照改正(snapshot):
    """错误分类学 E4.3：拿 cardinality>=1 顶替「全部」。"""
    data, options = snapshot
    utterance = "成本压到最低，但高风险的必须全修"
    patches = [P(slot="strategy", value="成本最低优先", utterance=utterance),
               P(slot="count_min", value=1, index={"组": SPEC}, utterance=utterance)]
    out = A.enforce(utterance, patches, data, options)
    n = A.group_size(SPEC, data, options)
    assert _count_min(out.patches).value == n
    assert out.patches[0].slot == "strategy"  # 相对偏好照留，不被硬约束吃掉
    assert any("至少修 1 个" in note and str(n) in note for note in out.notes)


def test_强度被标软要改回硬(snapshot):
    data, options = snapshot
    utterance = "高危必须全修"
    n = A.group_size(SPEC, data, options)
    patches = [P(slot="count_min", value=n, index={"组": SPEC},
                 strength="soft", utterance=utterance)]
    out = A.enforce(utterance, patches, data, options)
    assert _count_min(out.patches).strength == "hard"
    assert any("硬性要求" in note for note in out.notes)


def test_已经正确落硬约束时不重复也不啰嗦(snapshot):
    data, options = snapshot
    utterance = "高危必须全修"
    n = A.group_size(SPEC, data, options)
    patches = [P(slot="count_min", value=n, index={"组": SPEC}, utterance=utterance)]
    out = A.enforce(utterance, patches, data, options)
    assert out.patches == patches and out.notes == [] and out.clarify is None


# ── 3. 澄清 / 候选意图推荐 ─────────────────────────────────────────────

def test_含糊语义出澄清且硬约束排第一并标推荐(snapshot):
    data, options = snapshot
    utterance = "高危最好全修"
    out = A.enforce(utterance, [P(slot="strategy", value="高风险优先抢修",
                                  utterance=utterance)], data, options)
    assert out.clarify and len(out.clarify) == 1
    group = out.clarify[0]
    cands = group["候选"]
    assert len(cands) == 2
    hard, soft = cands
    assert hard["性质"] == "硬约束" and hard["推荐"] is True
    assert soft["性质"] == "相对偏好" and not soft["推荐"]
    assert _count_min(hard["patches"]).value == A.group_size(SPEC, data, options)
    assert any(p.slot == "strategy" for p in soft["patches"])
    assert "硬约束" in group["问"] and "加权" in group["问"]
    # 两条候选都得能直接落地，不能撞同格重复
    for cand in cands:
        assert not duplicate_cells(cand["patches"])
        assert cand["patches"], "候选必须带真补丁，不能只是意图名"


def test_模型报拿不准要问人不猜(snapshot):
    data, options = snapshot
    utterance = "高危必须全修"
    n = A.group_size(SPEC, data, options)
    out = A.enforce(utterance, [P(slot="count_min", value=n, index={"组": SPEC},
                                  strength="unsure", utterance=utterance)], data, options)
    assert out.clarify and out.clarify[0]["候选"][0]["性质"] == "硬约束"


def test_大模型自己澄清时补进硬候选(snapshot):
    data, options = snapshot
    groups = [{"问": "按什么顺序", "候选": [
        {"意图": "按截止最早", "patches": []},
        {"意图": "按严重度最高", "patches": []},
    ]}]
    out = A.enrich_clarification("高危必须全修", groups, data, options)
    cands = out[0]["候选"]
    assert cands[0]["性质"] == "硬约束" and cands[0]["推荐"] is True
    assert _count_min(cands[0]["patches"]) is not None
    assert len(cands) == 3  # 原来两条都留着


def test_澄清候选经会话落地后标签不丢(snapshot):
    data, options = snapshot
    utterance = "高危最好全修"
    out = A.enforce(utterance, [P(slot="strategy", value="高风险优先抢修",
                                  utterance=utterance)], data, options)
    chosen = out.clarify[0]["候选"][0]["patches"]
    session = Session(data)
    turn = session.apply(chosen, utterance=utterance)
    assert not turn.错误
    assert _count_min(session.patches()) is not None
    turn.澄清 = out.clarify
    trace = session.trace()[0]["澄清"][0]["候选"][0]
    assert trace["性质"] == "硬约束" and trace["推荐"] is True


# ── 4. 翻译层接线（canned JSON，不调大模型） ────────────────────────────

def _stub(monkeypatch, payload):
    import yxcore.parse as parse_mod

    monkeypatch.setattr(parse_mod, "_chat",
                        lambda *_a, **_k: json.dumps(payload, ensure_ascii=False))


def test_翻译出口把只有偏好的绝对优先补成硬约束(snapshot, monkeypatch):
    from yxcore.parse import translate_full

    data, options = snapshot
    _stub(monkeypatch, {"patches": [{"slot": "strategy", "value": "高风险优先抢修"}]})
    patches, reason, clarify, scenarios, notes = translate_full("高危必须全修", data, options)
    assert reason is None and clarify is None and scenarios is None
    assert _count_min(patches).value == A.group_size(SPEC, data, options)
    assert notes


def test_模型判做不到的绝对优先不再被拒(snapshot, monkeypatch):
    from yxcore.parse import translate_full

    data, options = snapshot
    _stub(monkeypatch, {"patches": [], "拒绝理由": "表达不了「全部」"})
    patches, reason, clarify, _scenarios, notes = translate_full("高危必须全修", data, options)
    assert reason is None and _count_min(patches) is not None
    assert any("做不到" in note for note in notes)


def test_相对偏好翻译不受影响(snapshot, monkeypatch):
    from yxcore.parse import translate_full

    data, options = snapshot
    _stub(monkeypatch, {"patches": [{"slot": "strategy", "value": "高风险优先抢修"}]})
    patches, reason, clarify, _scenarios, notes = translate_full("高危优先", data, options)
    assert [p.slot for p in patches] == ["strategy"]
    assert reason is None and clarify is None and notes == []


def test_含糊语义从翻译层走澄清(snapshot, monkeypatch):
    from yxcore.parse import translate_full

    data, options = snapshot
    _stub(monkeypatch, {"patches": [{"slot": "strategy", "value": "高风险优先抢修"}]})
    patches, _reason, clarify, _scenarios, _notes = translate_full("高危最好全修", data, options)
    assert patches == []  # 拿不准就不落补丁
    assert clarify[0]["候选"][0]["性质"] == "硬约束"


# ── 5. 回显 / 对账：硬软必须说清楚 ─────────────────────────────────────

def test_回显把硬约束和加权偏好分开说(snapshot):
    data, _options = snapshot
    n = A.group_size(SPEC, data, None)
    patches = [A.hard_patch(SPEC, n, "高危必须全修"),
               P(slot="strategy", value="成本最低优先", utterance="成本压到最低")]
    request, errors = compile_patches(patches, data, data)
    assert not errors
    assert any("成本换不掉" in line for line in request.echo)
    assert any("不保证" in line for line in request.echo)
    assert request.mode == "加权"


# ── 6. 模型里的证据：约束真的写进 CP-SAT，成本目标拿不走 ────────────────

def test_硬约束真的进了CP_SAT模型(snapshot):
    from product_planner import RunSpec, build_model

    data, options = snapshot
    n = A.group_size(SPEC, data, options)
    patch = A.hard_patch(SPEC, n, "高危必须全修")
    # 一次求解的配置必须先在 RunSpec 定稿；两次只换 DSL 闭包，其余保持同一份。
    spec = RunSpec.resolve(data, partial=True)
    plain, _, _, _ = build_model(data, options, spec)
    with_hard, _, _, _ = build_model(
        data, options, RunSpec.resolve(
            data, partial=True, extra_constraints=build_closure([patch], data)))
    plain_constraints = list(plain.Proto().constraints)
    hard_constraints = list(with_hard.Proto().constraints)
    assert len(hard_constraints) == len(plain_constraints) + 1
    added = hard_constraints[len(plain_constraints):]
    # Σx ≥ n：所有系数都是 1，下界就是这一格的任务数，不是「至少 1 个」
    assert any(list(c.linear.domain) and c.linear.domain[0] == n
               and c.linear.coeffs and set(c.linear.coeffs) == {1}
               for c in added)


def test_与总数上限冲突时求解器判不可行(snapshot):
    """老写法（只有加权偏好）能出解；加了硬约束，成本/数量把它换不掉 → INFEASIBLE。"""
    data, options = snapshot
    n = A.group_size(SPEC, data, options)
    cap = P(slot="count_max", value=n - 1, utterance="这轮最多修4个",
            index={"组": {"kind": "全部"}})
    feasible = run([cap], seconds=10)
    assert feasible["排程"]
    assert sum(1 for s in feasible["排程"] if s["严重等级"] == "高") < n
    hard = A.hard_patch(SPEC, n, "高危必须全修")
    with pytest.raises(YxError) as caught:
        run([cap, hard], seconds=10)
    assert caught.value.code == "不可行"
