"""submit_change：把预览变成真求解。

这个工具的唯一风险不是"用错"，是"用得太随便"——一次真求解要跑三分钟、
占算力、生成新方案并改掉当前选中版本。所以设计的重心全在**门槛**上：

  门槛不是参数对不对，是**有没有预览过**。

没有待确认的变更就拒绝，模型不能凭一句"直接算"凭空发起求解。
下面第一组测试守这个门槛，第二组守"派活失败要说出来"。
"""

from __future__ import annotations

from pathlib import Path

from app.core.agent_tools import AgentToolbox
from app.core.solve_jobs import SolveJobRepository
from app.db.database import Database
from app.db.models import ModelArtifactRepository, VersionRepository


def _toolbox(
    temp_db_path: Path,
    *,
    with_jobs: bool = True,
    submit=None,
) -> AgentToolbox:
    db = Database(temp_db_path)
    return AgentToolbox(
        conversation_id="conv_submit",
        versions=VersionRepository(db),
        model_artifacts=ModelArtifactRepository(db),
        jobs=SolveJobRepository(db) if with_jobs else None,
        on_submit_change=submit,
    )


def _pending() -> dict:
    return {
        "kind": "dsl",
        "dsl": {"constraints": [], "objectives": [], "data": []},
        "baseVersionId": None,
        "sourcePrompt": "船租涨到8万，直接算",
    }


def _ok_submitter(calls: list) -> object:
    def submit(conversation_id: str, pending: dict, reason: str) -> dict:
        calls.append({"conversationId": conversation_id, "pending": pending, "reason": reason})
        return {"ok": True, "jobId": "job_fake123", "message": "真实求解已启动"}

    return submit


def test_without_pending_change_it_refuses_to_start_a_solve(temp_db_path: Path) -> None:
    """核心安全线：没预览过就不许派活。

    允许模型凭空发起求解，等于绕开"先看预览再确认"。真排程一次三分钟、
    占算力、还会覆盖当前选中版本——这种动作不能是模型想起来就做的。
    """
    calls: list = []
    toolbox = _toolbox(temp_db_path, submit=_ok_submitter(calls))

    result = toolbox.execute("submit_change", {"reason": "直接算吧"})

    assert result["status"] == "clarify"
    assert calls == [], "被拒绝了却还是把活派出去了"


def test_with_pending_change_it_dispatches_and_returns_job_id(temp_db_path: Path) -> None:
    calls: list = []
    toolbox = _toolbox(temp_db_path, submit=_ok_submitter(calls))
    toolbox.pending_change = _pending()

    result = toolbox.execute("submit_change", {"reason": "直接算吧"})

    assert result["status"] == "submitted"
    assert result["data"]["jobId"] == "job_fake123"
    assert calls and calls[0]["conversationId"] == "conv_submit"
    # 授权依据要原样带到下游，事后能回答"这次是谁让派的"
    assert calls[0]["reason"] == "直接算吧"


def test_without_solver_service_it_says_so_and_keeps_the_preview(temp_db_path: Path) -> None:
    """派不出去时不能假装派了，也不能把预览作废——用户还能在界面上确认。"""
    toolbox = _toolbox(temp_db_path, with_jobs=False, submit=_ok_submitter([]))
    toolbox.pending_change = _pending()

    result = toolbox.execute("submit_change", {})

    assert result["status"] == "unavailable"
    assert "仍然有效" in result["data"]["message"]


def test_dispatch_failure_is_reported_not_swallowed(temp_db_path: Path) -> None:
    """派活失败必须让模型知道，否则它会照着"已启动"去答复用户。"""

    def boom(_conversation_id, _pending, _reason):
        raise RuntimeError("求解进程没起来")

    toolbox = _toolbox(temp_db_path, submit=boom)
    toolbox.pending_change = _pending()

    result = toolbox.execute("submit_change", {})

    assert result["status"] == "error"
    assert "求解进程没起来" in result["data"]["message"]
    assert any("没有启动" in item for item in result["warnings"])


def test_expired_pending_change_is_an_error_not_a_crash(temp_db_path: Path) -> None:
    def expired(_conversation_id, _pending, _reason):
        return {"ok": False, "message": "待确认的变更已经失效：'request'"}

    toolbox = _toolbox(temp_db_path, submit=expired)
    toolbox.pending_change = _pending()

    result = toolbox.execute("submit_change", {})

    assert result["status"] == "error"
    assert "已经失效" in result["data"]["message"]


def test_spec_tells_the_model_when_it_may_submit() -> None:
    """工具说明是安全边界的一部分：它必须写清"什么时候可以调"。

    光靠代码拦（没预览就拒绝）不够——模型会反复试、反复被拒，白烧轮次。
    说明里写明两种可调情形，它才不会在用户犹豫的时候动手。
    """
    toolbox = object.__new__(AgentToolbox)
    spec = next(item for item in toolbox.tool_specs() if item["name"] == "submit_change")
    text = spec["description"]

    assert "必须先调过 solve" in text
    assert "直接算" in text and "算吧" in text
    assert "不许调" in text


# ── 接线：模型派活要走用户点确认那条路 ────────────────────────────────────────
# 这一层不测模型（没 key 跑不了），只测"接到哪去"。
# 值得单独守的原因是：如果哪天有人图省事在 toolbox 里另写一套派活逻辑，
# 就会出现"点按钮"和"模型派"两条路，迟早给出两个答案——上一轮 DSL 双路径
# 就是这么坏的。

def test_pending_change_dispatches_through_the_same_path_as_user_confirm(monkeypatch) -> None:
    import threading
    from types import SimpleNamespace

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime.memories = SimpleNamespace(get_or_create=lambda _cid: SimpleNamespace())
    seen: dict = {}

    def fake_start_dsl_solve(*, conversation_id, dsl, content, memory, base_version_id=None, utterance=None):
        seen.update({"conversationId": conversation_id, "dsl": dsl,
                     "content": content, "baseVersionId": base_version_id,
                     "utterance": utterance})
        return SimpleNamespace(solveJob=SimpleNamespace(id="job_real_looking"), message="x")

    runtime._start_dsl_solve = fake_start_dsl_solve

    outcome = AgentRuntime._submit_pending_change(
        runtime, "conv_submit",
        {"kind": "dsl", "dsl": {"constraints": []}, "baseVersionId": "ver_1",
         "sourcePrompt": "船租涨到8万，直接算", "utterance": "船租涨到8万，直接算"},
        "直接算吧",
    )

    assert outcome["ok"] is True
    assert outcome["jobId"] == "job_real_looking"
    assert seen["conversationId"] == "conv_submit"
    assert seen["baseVersionId"] == "ver_1"
    assert seen["content"] == "船租涨到8万，直接算"
    # 原话跟着变更一起交给求解侧：那里会拿它跟 DSL 再核一次。
    # 只传模型转写过的 sourcePrompt，等于把核查材料换成被核查人自己写的。
    assert seen["utterance"] == "船租涨到8万，直接算"
    # 外层靠这个字段决定"用求解响应顶掉模型的话"，忘了设就会重复播报
    assert runtime._tls.submitted_response is not None


def test_malformed_pending_change_fails_cleanly() -> None:
    import threading
    from types import SimpleNamespace

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime.memories = SimpleNamespace(get_or_create=lambda _cid: SimpleNamespace())

    outcome = AgentRuntime._submit_pending_change(
        runtime, "conv_submit", {"kind": "plan", "request": {"不认识的字段": 1}}, "算吧"
    )

    assert outcome["ok"] is False
    assert outcome["message"]


def test_new_turn_clears_the_previous_submission() -> None:
    """上一轮派出去的响应必须清掉。

    不清的话，下一轮模型只是闲聊，也会被那个旧响应顶掉——
    用户问"今天天气怎么样"，界面回"求解已启动"。
    """
    import threading

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime._tls.submitted_response = "上一轮的旧响应"

    AgentRuntime._begin_trace(runtime, "conv_submit", "今天天气怎么样")

    assert runtime._tls.submitted_response is None


# ── 接线：模型派活要走用户点确认那条路 ────────────────────────────────────────
# 这一层不测模型（没 key 跑不了），只测"接到哪去"。
# 值得单独守的原因是：如果哪天有人图省事在 toolbox 里另写一套派活逻辑，
# 就会出现"点按钮"和"模型派"两条路，迟早给出两个答案——上一轮 DSL 双路径
# 就是这么坏的。

def test_pending_change_dispatches_through_the_same_path_as_user_confirm(monkeypatch) -> None:
    import threading
    from types import SimpleNamespace

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime.memories = SimpleNamespace(get_or_create=lambda _cid: SimpleNamespace())
    seen: dict = {}

    def fake_start_dsl_solve(*, conversation_id, dsl, content, memory, base_version_id=None, utterance=None):
        seen.update({"conversationId": conversation_id, "dsl": dsl,
                     "content": content, "baseVersionId": base_version_id,
                     "utterance": utterance})
        return SimpleNamespace(solveJob=SimpleNamespace(id="job_real_looking"), message="x")

    runtime._start_dsl_solve = fake_start_dsl_solve

    outcome = AgentRuntime._submit_pending_change(
        runtime, "conv_submit",
        {"kind": "dsl", "dsl": {"constraints": []}, "baseVersionId": "ver_1",
         "sourcePrompt": "船租涨到8万，直接算", "utterance": "船租涨到8万，直接算"},
        "直接算吧",
    )

    assert outcome["ok"] is True
    assert outcome["jobId"] == "job_real_looking"
    assert seen["conversationId"] == "conv_submit"
    assert seen["baseVersionId"] == "ver_1"
    assert seen["content"] == "船租涨到8万，直接算"
    # 原话跟着变更一起交给求解侧：那里会拿它跟 DSL 再核一次。
    # 只传模型转写过的 sourcePrompt，等于把核查材料换成被核查人自己写的。
    assert seen["utterance"] == "船租涨到8万，直接算"
    # 外层靠这个字段决定"用求解响应顶掉模型的话"，忘了设就会重复播报
    assert runtime._tls.submitted_response is not None


def test_malformed_pending_change_fails_cleanly() -> None:
    import threading
    from types import SimpleNamespace

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime.memories = SimpleNamespace(get_or_create=lambda _cid: SimpleNamespace())

    outcome = AgentRuntime._submit_pending_change(
        runtime, "conv_submit", {"kind": "plan", "request": {"不认识的字段": 1}}, "算吧"
    )

    assert outcome["ok"] is False
    assert outcome["message"]


def test_new_turn_clears_the_previous_submission() -> None:
    """上一轮派出去的响应必须清掉。

    不清的话，下一轮模型只是闲聊，也会被那个旧响应顶掉——
    用户问"今天天气怎么样"，界面回"求解已启动"。
    """
    import threading

    from app.core.agent_runtime import AgentRuntime

    runtime = object.__new__(AgentRuntime)
    runtime._tls = threading.local()
    runtime._tls.submitted_response = "上一轮的旧响应"

    AgentRuntime._begin_trace(runtime, "conv_submit", "今天天气怎么样")

    assert runtime._tls.submitted_response is None
