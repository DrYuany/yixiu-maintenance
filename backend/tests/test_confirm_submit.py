# -*- coding: utf-8 -*-
"""「确认」这一步的回归：预览出得来、确认起不来，是本产品最贵的一类空洞。

线上实测（会话 conv_d80043ff）：用户要求「经济优先和分层各排一版对比」→
出批量预览 → 回「确认」→ 提交函数只认 kind=yx，批量被判「待确认的变更已经
失效」→ 模型只能把同一句话再翻译一遍再出预览，空转 7 轮、2 分 40 秒、0 个方案。

以前只有「预览」这一侧有测试（test_agent_tools_solve_exits），确认这一侧一条
都没有，所以这个洞一直绿着。这里从工具层一路钉到求解真的被派出去。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_runtime import AgentRuntime

SCENARIO_PATCH = {"slot": "strategy", "value": "成本最低优先"}
BASE_PATCH = {"slot": "strategy", "value": "高风险优先抢修"}


def _runtime(filters=None):
    """最小 runtime：只需要记忆、待确认状态和提交后的响应槽。

    记忆存储照真实语义来：apply_patch 返回**新对象**、不改传入的那个。
    以前这里图省事直接改对象，把「清完待确认又被写回去」这类问题盖住了。
    """
    store = {"memory": SimpleNamespace(activeFilters=dict(filters or {}), activeView="P01")}

    def get_or_create(_cid):
        return store["memory"]

    def apply_patch(memory, patch):
        # 用 key 判断而不是 or：清空后的 {} 也是合法的新值，不能被当成「没传」
        next_filters = (dict(patch["activeFilters"]) if "activeFilters" in patch
                        else dict(memory.activeFilters))
        store["memory"] = SimpleNamespace(activeFilters=next_filters, activeView="P01")
        return store["memory"]

    rt = AgentRuntime.__new__(AgentRuntime)
    rt.memories = SimpleNamespace(get_or_create=get_or_create, apply_patch=apply_patch)
    rt._tls = SimpleNamespace(submitted_response=None)
    return rt, store


def test_batch_pending_change_is_submittable():
    """批量预览同样是「等用户点头」的形态，确认必须放行。"""
    pending = {"kind": "batch", "scenarios": [{"标签": "经济优先", "patches": []}]}
    rt, store = _runtime({"pendingChange": pending})
    started = []

    def fake_start(conversation_id, pending_change, mem, sink):
        started.append(pending_change)
        return SimpleNamespace(
            assistantMessage=SimpleNamespace(content="已并发启动 1 个求解任务。"))

    rt._start_planning_solve = fake_start
    out = rt._submit_pending_change("c1", pending, "算吧")

    assert out["ok"] is True, out
    assert started and started[0]["kind"] == "batch", "批量场景没被交给求解分派"
    assert "失效" not in out["message"]
    assert "pendingChange" not in store["memory"].activeFilters, "提交后待确认状态要清掉"


def test_yx_pending_change_still_submittable():
    """单场景那条老路不能被这次放行改坏。"""
    pending = {"kind": "yx", "sourcePrompt": "船租涨两成"}
    rt, _ = _runtime({"pendingChange": pending})
    started = []

    def fake_start(conversation_id, pending_change, mem, sink):
        started.append(pending_change)
        return SimpleNamespace(assistantMessage=SimpleNamespace(content="求解已启动。"))

    rt._start_planning_solve = fake_start
    out = rt._submit_pending_change("c1", pending, "算吧")

    assert out["ok"] is True
    assert started and started[0]["kind"] == "yx"


def test_clarify_pending_change_is_refused_with_its_own_words():
    """澄清中的变更不是「失效」，是「还没答完」——两句话不能混着说。"""
    pending = {"kind": "clarify", "groups": [], "chosen": [], "nextIndex": 0}
    rt, store = _runtime({"pendingChange": pending})
    out = rt._submit_pending_change("c1", pending, "算吧")

    assert out["ok"] is False
    assert "还没定下来" in out["message"]
    assert "失效" not in out["message"], "说失效会让用户重说一遍需求"
    assert store["memory"].activeFilters.get("pendingChange") is pending, "拒绝时不该清掉待选项"


def test_unknown_kind_is_refused():
    rt, _ = _runtime()
    out = rt._submit_pending_change("c1", {"kind": "whatever"}, "算吧")
    assert out["ok"] is False and "失效" in out["message"]


def test_batch_route_reaches_batch_solver():
    """分派层：kind=batch 必须落到并发批量求解，不该掉进单场景。"""
    rt, _ = _runtime()
    called = {}
    rt._start_batch_solve = lambda cid, pending, memory, sink: called.update(pending)
    rt._start_planning_solve(
        "c1", {"kind": "batch", "scenarios": []}, SimpleNamespace(), None)
    assert called.get("kind") == "batch"


def test_batch_scenario_starts_from_current_requirements(monkeypatch):
    """批量场景要先铺当前已生效的要求，再叠本方案那一个维度。

    不铺底的话，「船租涨两成、最多修 8 个」这类前几轮确认过的要求会在批量
    求解里凭空消失，算出来的方案看着有、其实少了约束。
    """
    applied = []

    class _Session:
        def _ensure_data(self):
            return None

        def apply(self, patches):
            applied.append([p.to_json() for p in patches])
            return SimpleNamespace(错误=[])

        def run(self, seconds=0):
            return {"求解": {"解状态": "OPTIMAL", "有解": True}}

    class _Engine:
        def __init__(self):
            self.session = _Session()

        def build_version(self, cid, label, result, version_id, **kwargs):
            return SimpleNamespace(id=version_id, windows=[], label=label)

    monkeypatch.setattr("app.core.agent_runtime.YxEngine", _Engine)
    rt, _ = _runtime()
    rt.versions = SimpleNamespace(add=lambda version: None)
    rt.candidates = None

    version = rt._run_batch_scenario_sync(
        "c1", {"标签": "成本优先", "patches": [SCENARIO_PATCH]}, [BASE_PATCH])

    assert version is not None
    assert len(applied) == 2, "应当先铺底、再叠本场景"
    assert applied[0][0]["槽位"] == "strategy" and applied[0][0]["值"] == "高风险优先抢修"
    assert applied[1][0]["值"] == "成本最低优先"


def test_fresh_batch_engine_has_base_snapshot():
    """批量引擎没经过 say()，必须自己装上基础快照。

    不装的话 build_version 读 session.base_data 拿到 None 直接抛错，
    每个场景都失败、0 个方案——就算确认这道门放开了也出不来东西。
    """
    engine = AgentRuntime._fresh_session_engine()
    assert engine.session.base_data is not None


def test_batch_job_failure_is_marked_not_left_running(monkeypatch):
    """单个场景算炸了要落成 FAILED 并推进批量，不能挂成永远 RUNNING。

    挂着不动的话前端会一直轮询、批量永不汇合，用户看到的就是「一直在循环」。
    """
    class _Session:
        def _ensure_data(self):
            return None

        def apply(self, patches):
            return SimpleNamespace(错误=[])

        def run(self, seconds=0):
            raise RuntimeError("solver boom")

    class _Engine:
        def __init__(self):
            self.session = _Session()

    monkeypatch.setattr("app.core.agent_runtime.YxEngine", _Engine)
    rt, _ = _runtime()
    failed, advanced = [], []
    rt.jobs = SimpleNamespace(mark_running=lambda *args: None,
                              mark_failed=lambda jid, msg: failed.append((jid, msg)))
    rt._batch_job_done = lambda cid, jid, version: advanced.append(jid)

    rt._execute_batch_job("job1", "c1", "方案A", [], [])

    assert failed and "boom" in failed[0][1]
    assert advanced == ["job1"], "失败的场景也要推进批量，否则永远算不完"


def test_batch_solve_does_not_resurrect_cleared_pending():
    """清掉待确认之后写 pendingBatch，不能拿旧记忆把 pendingChange 带回来。

    线上实测的残余循环：两个方案都算完了，再点一次「确认」又能触发一轮，
    因为提交时把「已清掉的待确认」连同旧快照一起写回了记忆。
    """
    stale = {"kind": "batch", "scenarios": [{"标签": "经济优先", "patches": []}]}
    rt, store = _runtime({})  # 存储里已经是清的，旧的 stale 只在调用方手里
    rt.jobs = None
    rt.job_pool = None
    rt._yx_engine = lambda _cid: SimpleNamespace(session=SimpleNamespace(patches=lambda: []))
    rt._run_batch_scenario_sync = lambda *args, **kwargs: None
    rt._emit_batch_compare = lambda *args: None
    rt._finalize_response = lambda *args, **kwargs: SimpleNamespace()

    rt._start_batch_solve("c1", {"scenarios": [{"标签": "经济优先", "patches": []}]},
                          SimpleNamespace(activeFilters=dict(pendingChange=stale), activeView="P01"),
                          None)

    filters = store["memory"].activeFilters
    assert "pendingChange" not in filters, "已清掉的待确认被旧快照写回复活了"
    assert filters.get("pendingBatch", {}).get("total") == 1


def test_toolbox_submit_change_actually_starts_solve_for_batch():
    """端到端：工具返回 submitted，且求解分派真的被调用。

    这条正是原来缺的那半边——以前的测试只到 solve 出预览为止，
    工具能不能把确认送出去、送出去之后有没有真的起任务，全都没人守。
    """
    pending = {"kind": "batch", "preview": "将按这些方案分别排：\n- 经济优先",
               "scenarios": [{"标签": "经济优先", "patches": []}]}
    rt, _ = _runtime({"pendingChange": pending})
    rt.versions = SimpleNamespace(list_by_conversation=lambda _cid: [])
    rt.model_artifacts = None
    rt.jobs = SimpleNamespace()
    rt._yx_engine = lambda _cid: SimpleNamespace(session=SimpleNamespace())
    rt._record_tool_call = lambda record: None
    started = []

    def fake_start(conversation_id, pending_change, mem, sink):
        started.append(pending_change)
        return SimpleNamespace(assistantMessage=SimpleNamespace(
            content="已并发启动 1 个求解任务，全部算完会自动出方案卡和对比。"))

    rt._start_planning_solve = fake_start
    toolbox = rt._build_toolbox("c1", None, None)

    result = toolbox.execute("submit_change", {"reason": "算吧"})

    assert result["status"] == "submitted", result
    assert started, "确认之后求解没有真的被派出去"
    assert "失效" not in result["data"]["message"]
    assert result["data"]["verbatim"] is True
