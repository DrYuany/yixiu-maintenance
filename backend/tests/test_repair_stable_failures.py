"""两次全量真录都失败的 6 条稳定产品毛病，逐条钉一个回归测试。

录到的原始证据在 backend/eval_cases/online/traces.jsonl（本地证据，不入库）：
- OD-01 第4轮 / OD-02 第1轮：随便跑一个方案 → 「这句没接：大模型没有给出任何补丁」，无预览
- OD-01 第10轮 / OD-43 第2轮：求解启动话术甩 job_ 内部编号
- OD-36 第1轮：两条拉扯的目标只列「记下了」，不说冲突、不给退路
- OD-41 第1轮：窗口拒绝不给业务上正确的说法，还甩内部术语
"""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from app.core.agent_runtime import AgentRuntime
from app.core.agent_tools import AgentToolbox, _conflict_hint, _RUN_AS_IS_RE
from app.core.conversation_agent import ConversationAgent
from app.evals.online_scorers import evaluate_turn


def _turn(*, refusal=None, added=(), echo_lines=()):
    return SimpleNamespace(
        场景=None, 澄清=None, 错误=[], 拒绝=refusal,
        新增=list(added), 修订=[], 撤销=[],
        echo=lambda: list(echo_lines),
    )


def _toolbox(engine, utterance: str, captured: list[dict]) -> AgentToolbox:
    return AgentToolbox(
        conversation_id="c-eval",
        versions=SimpleNamespace(list_by_conversation=lambda _cid: []),
        model_artifacts=None,
        yx_engine=engine,
        user_message=utterance,
        on_pending_change=captured.append,
    )


def test_run_as_is_request_creates_preview_instead_of_refusal() -> None:
    """「随便跑一个方案」不该把内部异常倒给用户，也不该丢掉预览。"""
    utterance = "你随便跑一个方案吧，然后让我看看模型"
    engine = SimpleNamespace(session=SimpleNamespace(
        say=lambda text, timeout=90: _turn(refusal="大模型没有给出任何补丁"),
        summary=lambda: ["整体排程风格按「成本最低优先」"],
    ))
    captured: list[dict] = []
    result = _toolbox(engine, utterance, captured).execute("solve", {})

    assert _RUN_AS_IS_RE.search(utterance)
    assert result["status"] == "needs_confirmation", result
    assert captured and captured[0]["kind"] == "yx" and captured[0]["asIs"] is True
    text = result["data"]["markdown"]
    assert "按当前已生效的要求排一版" in text
    assert "这句没接" not in text and "大模型没有给出任何补丁" not in text
    assert "确认后开始求解" in text


def test_business_refusal_is_not_swallowed_by_run_as_is_fallback() -> None:
    """窗口改期这类业务拒绝不能借「按现状排一版」偷偷变成可执行预览。"""
    utterance = "窗口03 缩短到 11月19号结束"
    engine = SimpleNamespace(session=SimpleNamespace(
        say=lambda text, timeout=90: _turn(
            refusal="海事窗口的起止时间是已批准的事实，不能改",
            echo_lines=["这句没接：海事窗口的起止时间是已批准的事实，不能改"],
        ),
        summary=lambda: [],
    ))
    captured: list[dict] = []
    result = _toolbox(engine, utterance, captured).execute("solve", {})

    assert result["status"] == "clarify", result
    assert not captured, "业务拒绝不该生成待确认预览"


def test_conflict_hint_in_preview_gives_user_a_way_out() -> None:
    """成本最低 + 必须全修：预览要明说会拉扯，并给「放宽」这条退路。"""
    summary = ["高严重度的任务｜至少修几个 = 5 个", "整体排程风格按「成本最低优先」"]
    hint = _conflict_hint(summary)
    assert hint and "互相拉扯" in hint and "放宽" in hint
    assert _conflict_hint(["整体排程风格按「成本最低优先」"]) is None

    engine = SimpleNamespace(session=SimpleNamespace(
        say=lambda text, timeout=90: _turn(
            added=[object()],
            echo_lines=["记下了：整体排程风格按「成本最低优先」",
                        "记下了：高严重度的任务｜至少修几个 = 5 个"],
        ),
        summary=lambda: summary,
    ))
    captured: list[dict] = []
    result = _toolbox(engine, "成本压到最低，但高风险的必须全修", captured).execute("solve", {})
    assert result["status"] == "needs_confirmation"
    assert "互相拉扯" in result["data"]["markdown"]


def test_solve_started_reply_never_leaks_internal_job_id() -> None:
    """求解启动话术是系统独占输出，不许出现 job_ 这类内部编号。"""
    src = inspect.getsource(AgentRuntime._start_planning_solve)
    assert not re.search(r"任务\s*\{job\.id\}|（任务\s*\{", src), "job id 又被写回用户可见文本"
    assert "真实 CP-SAT 求解已启动，正在后台跑。算完会自动出方案卡。" in src

    toolbox_src = inspect.getsource(AgentToolbox._submit_change)
    assert "告诉用户任务号" not in toolbox_src


def test_window_fact_rule_is_in_system_prompt() -> None:
    """窗口事实只读 + 正确出口，是系统提示里的硬约束，不是模型临场发挥。"""
    src = inspect.getsource(ConversationAgent._system_prompt)
    assert "海事窗口的起止时间是已批准的业务事实" in src
    assert "最晚 11 月 19 号完成" in src and "最早开工" in src
    assert "part_hours" in src, "点名禁止甩内部术语"


def test_read_jobs_never_hands_internal_id_to_model() -> None:
    """第三次真录发现 job_ 换了个出口：read_jobs 的任务号被模型原样抄进回答。"""
    from app.core.agent_tools import _job_facts

    job = SimpleNamespace(
        id="job_32b5c96702e8", status="RUNNING", solverStatus="UNKNOWN",
        phase="running", progressPercent=37, statusMessage="正在求解",
        elapsedSeconds=15, submittedAt="", finishedAt=None,
        needsUserDecision=False, result={}, error=None,
        conversationId="c-eval",
    )
    facts = _job_facts(job, 1)
    assert facts["jobRef"] == "第1个任务"
    assert "jobId" not in facts and "job_" not in str(facts)

    fake_jobs = SimpleNamespace(
        list_by_conversation=lambda _cid: [job],
        get=lambda _id: job,
    )
    toolbox = AgentToolbox(
        conversation_id="c-eval",
        versions=SimpleNamespace(list_by_conversation=lambda _cid: []),
        model_artifacts=None, jobs=fake_jobs,
    )
    listed = toolbox.execute("read_jobs", {})
    assert listed["status"] == "ok"
    assert "job_" not in str(listed), "列表里不该出现内部编号"
    one = toolbox.execute("read_jobs", {"jobRef": "第 1 个任务"})
    assert one["status"] == "ok" and one["data"]["jobs"][0]["jobRef"] == "第1个任务"


def test_confirmation_utterance_is_not_run_as_is_request() -> None:
    """「确认生成沙盘」是确认上一份预览，不能被当成「按现状排一版」的新请求。"""
    engine = SimpleNamespace(session=SimpleNamespace(
        say=lambda text, timeout=90: _turn(refusal="大模型没有给出任何补丁"),
        summary=lambda: [],
    ))
    captured: list[dict] = []
    result = _toolbox(engine, "确认生成沙盘", captured).execute("solve", {})
    assert result["status"] == "clarify", result
    assert not captured, "确认类话术不该凭空生成一份新预览"
    assert _RUN_AS_IS_RE.search("你随便跑一个方案吧，然后让我看看模型")


def test_refusal_with_next_step_is_recognised_as_exit() -> None:
    """拒绝类回答把「那你能做什么」交回用户，也是退路；尺子补了这类说法。"""
    from app.evals.online_scorers import Evidence

    text = ("窗口03 现在的区间是 11-15 至 11-20。但海事窗口的起止时间不能改。"
            "如果你的实际目的是不让 20 号排新开工，这个可以做；要走这条路的话告诉我，"
            "我先出预览给你确认。")
    ev = Evidence(
        response={"actionType": "assistant_reply",
                  "assistantMessage": {"content": text}},
        tool_results=[], utterance="窗口03 缩短到 11月19号结束",
    )
    r = evaluate_turn("OD-41", 1, {"回答质量": {"给退路": ["19号前", "最早", "要求"]}}, ev)
    assert r.passed, r.checks
