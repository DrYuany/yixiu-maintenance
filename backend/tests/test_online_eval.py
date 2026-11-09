"""线上全链路评测系统的 pytest 钩子。

这里测的是「尺子」，不是产品：用例合规、矩阵覆盖、判分器三态、
录制器证据装配、基线过期拦截、以及没录到轨迹时不许报全绿。
全部离线，不需要 key，不需要真求解，所以每次提交都跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evals.online_matrix import coverage, validate_cases
from app.evals.online_scorers import Evidence, evaluate_case, evaluate_turn
from app.evals.run_online_eval import (
    CASES_PATH, _record_one, load_cases, mode_check, mode_replay,
)


def test_cases_and_matrix_pass_release_gate() -> None:
    cases = load_cases(CASES_PATH)
    assert len(cases) == 50, "线上评测集应当是 50 条真实链路对话"
    code, report = mode_check(cases)
    assert report["用例问题"] == []
    assert report["自检问题"] == []
    assert report["覆盖问题"] == []
    assert code == 0
    cov = report["覆盖"]
    assert cov["ok"] and cov["cells"] >= 60
    assert cov["missing_critical"] == []


def test_every_case_has_source_and_traceable_assertions() -> None:
    """每条用例都得说清话术从哪来，且断言键判分器认识。"""
    cases = load_cases(CASES_PATH)
    assert validate_cases(cases) == []
    counts = coverage(cases)["sources"]
    assert counts["线上实录"] >= 8, "线上实录必须保留，不许被编排用例稀释"
    assert sum(counts.values()) == 50


def test_scorer_three_states_are_distinct() -> None:
    ev = Evidence(
        response={"assistantMessage": {"content": "这是预览，等待确认"},
                  "actionType": "change_preview", "clarificationOptions": [1, 2]},
        trace={"toolCalls": [{"tool": "solve", "status": "needs_confirmation"}],
               "modelFingerprint": "fp-x"},
        memory={"activeFilters": {"pendingChange": {"kind": "yx"}}},
        tool_results=[{"tool": "solve", "result": {"status": "needs_confirmation",
                                                   "data": {"markdown": "预览"}}}],
        utterance="船租涨两成",
    )
    good = evaluate_turn("T", 1, {"动作": "change_preview", "必调工具": ["solve"],
                                 "预览": "有", "有指纹": True}, ev)
    assert good.passed and not good.failures and not good.unobserved

    bad = evaluate_turn("T", 2, {"动作": "solve_completed", "正文不含": ["这是预览"]}, ev)
    assert bad.failures and not bad.passed

    blind = Evidence(response={"assistantMessage": {"content": "好"}, "actionType": "assistant_reply"})
    blind_result = evaluate_turn("T", 3, {"必调工具": ["read_alarms"], "预览": "有",
                                           "数字可溯源": True}, blind)
    assert blind_result.unobserved and not blind_result.passed
    assert all(c["status"] != "fail" for c in blind_result.checks), "证据缺失不是产品失败"


def test_incomplete_recording_is_unobserved_not_pass() -> None:
    case = {"id": "T", "轮": [{"话术": "一", "期望": {"动作": "assistant_reply"}},
                              {"话术": "二", "期望": {"动作": "assistant_reply"}}],
            "终局": {"同一会话": True}}
    only_one = [Evidence(response={"actionType": "assistant_reply"}, conversation_id="c1",
                         utterance="一")]
    result = evaluate_case(case, only_one)
    assert not result.passed
    assert any("录制完整" in s for s in result.unobserved)


def test_replay_without_traces_returns_incomplete(tmp_path: Path) -> None:
    code, report = mode_replay(load_cases(CASES_PATH)[:2], tmp_path / "nope.jsonl",
                               False, None, False)
    assert code == 2, "没录到轨迹绝不许算通过"
    assert "没有录制轨迹" in report["错误"]


def test_stale_recording_baseline_is_rejected(tmp_path: Path) -> None:
    cases = load_cases(CASES_PATH)[:1]
    trace = tmp_path / "traces.jsonl"
    trace.write_text("\n".join([
        json.dumps({"kind": "meta", "git": "deadbeef", "casesDigest": "wrong",
                    "schema": "online-eval-trace-v1"}),
        json.dumps({"kind": "turn", "caseId": cases[0]["id"], "index": 1,
                    "evidence": {"response": {"actionType": "assistant_reply"},
                                 "utterance": cases[0]["轮"][0]["话术"]}}),
    ]) + "\n", encoding="utf-8")
    code, report = mode_replay(cases, trace, False, None, False)
    assert code == 2
    assert "录制基线过期" in report["错误"]


def test_recorder_assembles_every_evidence_field() -> None:
    """录制器必须把 response/trace/memory/job/工具信封都装上，缺一样就判不了。"""
    class Resp:
        def __init__(self, body: Any, status: int = 200) -> None:
            self._body, self.status_code = body, status

        def json(self) -> Any:
            return self._body

    captured: list[dict[str, Any]] = []

    class Client:
        def post(self, url: str, json: dict | None = None) -> Resp:  # noqa: A002
            if url == "/api/conversations":
                return Resp({"id": "conv-eval"})
            if url == "/api/agent/message":
                # 真实录制靠 patch AgentToolbox.execute 填这个列表；这里模拟一次调用
                captured.append({"tool": "solve",
                                 "result": {"status": "needs_confirmation"}})
                return Resp({"actionType": "change_preview", "traceId": "trace-1",
                             "assistantMessage": {"content": "预览"}})
            return Resp({"ok": True})

        def get(self, url: str) -> Resp:
            if url.startswith("/api/agent/trace/"):
                return Resp({"toolCalls": [{"tool": "solve", "status": "needs_confirmation"}]})
            if url.endswith("/jobs/latest"):
                return Resp({"id": "job-1", "status": "QUEUED"})
            if url.endswith("/jobs"):
                return Resp([{"id": "job-1"}])
            return Resp(None, 404)

    class Memory:
        def model_dump(self) -> dict:
            return {"activeFilters": {"pendingChange": {"kind": "yx"}}}

    class Manager:
        def get_or_create(self, cid: str) -> Memory:  # noqa: ARG002
            return Memory()

    case = {"id": "OD-TEST", "轮": [{"话术": "船租涨两成", "期望": {"预览": "有"}}]}
    rows = _record_one(Client(), None, Manager(), case, captured)
    ev = rows[0]["evidence"]
    assert ev["response"]["actionType"] == "change_preview"
    assert ev["trace"]["toolCalls"][0]["tool"] == "solve"
    assert ev["memory"]["activeFilters"]["pendingChange"]["kind"] == "yx"
    assert ev["job"]["id"] == "job-1"
    assert ev["tool_results"][0]["result"]["status"] == "needs_confirmation"
    assert ev["conversation_id"] == "conv-eval" and ev["job_count"] == 1
