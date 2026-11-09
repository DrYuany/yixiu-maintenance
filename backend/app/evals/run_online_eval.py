"""线上全链路评测 CLI：用例校验 / 真实录制 / 离线回放 / 退化比对。

四种模式，对应四种「不许假装」：

    check   默认。校验 50 条用例合规 + 矩阵覆盖度 + 判分器自检。
            不需要 key、不需要 trace，CI 每次都能跑，退出码诚实。
    record  真调 /api/agent/message，把每一轮的 response + trace +
            working memory + job + 工具返回信封录成 jsonl。需要 key。
    replay  只读录下来的 jsonl，确定性判分 + 矩阵报告。不需要 key。
    verify  replay 之后比对基线退化，并写回基线。

退出码：0 = 完整且通过；1 = 有失败；2 = 不完整（未录制 / 覆盖不足 /
未观测过多 / 录制基线已过期 / 模型未启用导致录的是降级路径）。
「跳过」和「没跑」在这里永远不是 0。

用法：
    py -3.12 -m app.evals.run_online_eval --mode check
    py -3.12 -m app.evals.run_online_eval --mode record --traces backend/eval_cases/online/traces.jsonl
    py -3.12 -m app.evals.run_online_eval --mode record --url https://<线上域名> --sharecode <码> --traces ...
    py -3.12 -m app.evals.run_online_eval --mode replay --traces ... --baseline ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.evals.online_matrix import (
    ANGLES, MIN_CELL_COVERAGE, MIN_PER_ANGLE, MIN_PER_STAGE, STAGES,
    coverage, matrix_text, validate_cases,
)
from app.evals.online_scorers import Evidence, evaluate_case, evaluate_turn

BACKEND = Path(__file__).resolve().parents[2]
CASES_PATH = BACKEND / "eval_cases" / "online" / "dialogues.json"
TRACES_PATH = BACKEND / "eval_cases" / "online" / "traces.jsonl"
BASELINE_PATH = BACKEND / "eval_cases" / "online" / "baseline.json"


# ── 基础 IO ─────────────────────────────────────────────────────────────

def load_cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_head() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=BACKEND.parent, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_traces(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    if not path.exists():
        return {}, {}
    meta: dict[str, Any] = {}
    turns: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "meta":
            meta = row
        elif row.get("kind") == "turn":
            turns.setdefault(row["caseId"], []).append(row)
    for case_id in turns:
        turns[case_id].sort(key=lambda r: r["index"])
    return meta, turns


# ── check：尺子自己先过一遍 ─────────────────────────────────────────────

def _selftest_scorers() -> list[str]:
    """用合成证据验判分器三态。判分器写错了比产品错更危险。"""
    problems: list[str] = []
    base = Evidence(
        response={"assistantMessage": {"content": "已按你说的改好，等待确认"},
                  "actionType": "change_preview", "clarificationOptions": [1, 2]},
        trace={"toolCalls": [{"tool": "solve", "status": "needs_confirmation"}],
               "modelFingerprint": "fp-test"},
        memory={"activeFilters": {"pendingChange": {"kind": "yx"}}},
        tool_results=[{"tool": "solve", "result": {"status": "needs_confirmation",
                                                   "data": {"markdown": "预览"}}}],
        conversation_id="c1", utterance="船租涨两成",
    )
    ok = evaluate_turn("SELF", 1, {"动作": "change_preview", "必调工具": ["solve"],
                                   "预览": "有", "选项至少": 2, "有指纹": True,
                                   "工具状态": {"solve": "needs_confirmation"}}, base)
    if not ok.passed:
        problems.append(f"判分器自检：全对证据应通过，实际 {ok.failures} / {ok.unobserved}")

    bad = evaluate_turn("SELF", 2, {"动作": "solve_completed", "禁调工具": ["solve"],
                                    "正文不含": ["已按你说的改好"]}, base)
    if bad.passed or not bad.failures:
        problems.append("判分器自检：明显错的证据必须判失败")

    blind = Evidence(response={"assistantMessage": {"content": "好"}, "actionType": "assistant_reply"})
    unobs = evaluate_turn("SELF", 3, {"必调工具": ["read_alarms"], "预览": "有",
                                       "数字可溯源": True}, blind)
    if not unobs.unobserved or unobs.passed:
        problems.append("判分器自检：证据缺失必须记未观测且不算通过")
    return problems


def mode_check(cases: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    case_problems = validate_cases(cases)
    report = coverage(cases)
    selftest = _selftest_scorers()
    coverage_problems: list[str] = []
    if report["missing_critical"]:
        coverage_problems.append("关键交叉格为空：" + ", ".join(report["missing_critical"]))
    if report["thin_rows"]:
        coverage_problems.append(f"薄行（每行至少 {MIN_PER_STAGE} 条）：{report['thin_rows']}")
    if report["thin_cols"]:
        coverage_problems.append(f"薄列（每列至少 {MIN_PER_ANGLE} 条）：{report['thin_cols']}")
    if report["cell_ratio"] < MIN_CELL_COVERAGE:
        coverage_problems.append(
            f"总覆盖率 {report['cell_ratio']} 低于闸门 {MIN_CELL_COVERAGE}")
    problems = case_problems + coverage_problems + selftest
    return (2 if problems else 0), {
        "用例问题": case_problems, "覆盖问题": coverage_problems,
        "自检问题": selftest, "覆盖": report,
    }


# ── record：真调线上链路，录全证据 ──────────────────────────────────────

class _RemoteResp:
    """给远端 HTTP 结果套一层 TestClient 风格的 .status_code / .json()。"""

    def __init__(self, body: Any, status: int) -> None:
        self._body, self.status_code = body, status

    def json(self) -> Any:
        return self._body


class RemoteClient:
    """真打线上站点。只依赖标准库，不引入新依赖。

    远端只能拿到 response 与 trace；working memory 和工具返回信封是进程内状态，
    拿不到就记「未观测」，绝不伪造。
    """

    def __init__(self, base_url: str, sharecode: str | None = None, timeout: int = 120) -> None:
        self.base = base_url.rstrip("/")
        self.sharecode = sharecode
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> _RemoteResp:
        import urllib.error
        import urllib.request

        url = self.base + path
        if self.sharecode and "?" not in path:
            url += f"?sharecode={self.sharecode}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return _RemoteResp(json.loads(raw) if raw else None, resp.status)
        except urllib.error.HTTPError as exc:
            return _RemoteResp({"error": exc.read().decode("utf-8", "replace")}, exc.code)
        except Exception as exc:  # noqa: BLE001
            return _RemoteResp({"error": str(exc)}, 0)

    def post(self, url: str, json: dict | None = None) -> _RemoteResp:  # noqa: A002
        return self._call("POST", url, json)

    def get(self, url: str) -> _RemoteResp:
        return self._call("GET", url)


def _record_one(client, runtime, memory_manager, case: dict[str, Any],
                captured: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    created = client.post("/api/conversations", json={"title": f"eval-{case['id']}"}).json()
    cid = created.get("id") if isinstance(created, dict) else None
    if not cid:
        # 远端不可达/被口令拦下时不许崩，也不许伪造轨迹：记一条错误，回放会报「缺轨迹」
        return [{"kind": "record_error", "caseId": case["id"],
                 "reason": f"会话创建失败：{str(created)[:300]}",
                 "recordedAt": datetime.now(timezone.utc).isoformat()}]
    rows: list[dict[str, Any]] = []
    job_count = 0
    for index, turn in enumerate(case.get("轮") or [], 1):
        if captured is not None:
            captured.clear()
        resp = client.post("/api/agent/message", json={
            "conversationId": cid, "message": turn["话术"],
        })
        body = resp.json() if resp.status_code == 200 else {}
        trace_id = body.get("traceId")
        trace = client.get(f"/api/agent/trace/{trace_id}").json() if trace_id else None
        memory = None
        if memory_manager is not None:
            try:
                memory = memory_manager.get_or_create(cid).model_dump()
            except Exception:  # noqa: BLE001
                memory = None
        job_resp = client.get(f"/api/agent/conversations/{cid}/jobs/latest")
        job = job_resp.json() if job_resp.status_code == 200 else None
        if isinstance(job, dict) and job.get("id"):
            job_count = len(client.get(f"/api/agent/conversations/{cid}/jobs").json() or [])
        # 用例显式声明的求解控制（如等待弹窗 → background），走真实 HTTP 端点
        record_cmd = turn.get("录制") or {}
        if record_cmd.get("动作") and job and job.get("id"):
            client.post(f"/api/agent/jobs/{job['id']}/actions",
                        json={"action": record_cmd["动作"]})
        ev = Evidence(
            response=body, trace=trace, memory=memory, job=job,
            tool_results=list(captured) if captured is not None else None,
            http_status=resp.status_code,
            conversation_id=cid, job_count=job_count, utterance=turn["话术"],
        )
        rows.append({"kind": "turn", "caseId": case["id"], "index": index,
                     "evidence": ev.to_dict(),
                     "recordedAt": datetime.now(timezone.utc).isoformat()})
    return rows


def mode_record(cases: list[dict[str, Any]], path: Path, limit: int | None,
                db_source: Path | None = None,
                cases_path: Path = CASES_PATH,
                remote_url: str | None = None,
                sharecode: str | None = None,
                timeout: int = 120) -> tuple[int, dict[str, Any]]:
    # 真·线上模式：请求打公网 URL。内部状态（working memory / 工具返回信封）
    # 远端拿不到，统一记「未观测」，回放时会如实报证据不足。
    if remote_url:
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "kind": "meta", "git": None, "mode": "remote", "baseUrl": remote_url,
            "casesDigest": _file_digest(cases_path),
            "planningAsOf": "(远端决定)", "model": "(远端决定)",
            "recordedAt": datetime.now(timezone.utc).isoformat(),
            "cases": len(cases), "schema": "online-eval-trace-v1",
        }
        lines = [json.dumps(meta, ensure_ascii=False)]
        selected = cases[:limit] if limit else cases
        remote = RemoteClient(remote_url, sharecode, timeout)
        for case in selected:
            print(f"  远端录制 {case['id']}（{len(case.get('轮') or [])} 轮）", flush=True)
            for row in _record_one(remote, None, None, case, None):
                lines.append(json.dumps(row, ensure_ascii=False, default=str))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        recorded_turns = sum(1 for r in lines[1:] if json.loads(r).get("kind") == "turn")
        errors = sum(1 for r in lines[1:] if json.loads(r).get("kind") == "record_error")
        return (2 if errors else 0), {
            "录制用例": len(selected), "成功轮数": recorded_turns, "录制失败用例": errors,
            "文件": str(path), "模式": "远端 HTTP", "meta": meta,
        }

    import shutil
    import tempfile

    from fastapi.testclient import TestClient

    from app.api import agent as agent_api
    from app.api import conversations as conversations_api
    from app.core.agent_tools import AgentToolbox
    from app.db.database import resolve_db_path
    from app.main import create_app

    # 录进真实库会把评测会话写进产品数据。默认复制一份现有库到临时目录：
    # 既保留线上那份真实方案/告警历史（read_versions 之类要用），又不写坏原库。
    src = db_source or resolve_db_path()
    work_dir = Path(tempfile.mkdtemp(prefix="yx-online-eval-"))
    work_db = work_dir / "agent.db"
    copied = False
    if src.exists():
        shutil.copy2(src, work_db)
        copied = True
    old_db = os.environ.get("MWA_DB_PATH")
    os.environ["MWA_DB_PATH"] = str(work_db)

    app = create_app()
    runtime = app.dependency_overrides[agent_api.get_runtime]()
    memory_manager = app.dependency_overrides[conversations_api.get_memory_manager]()
    if not getattr(getattr(runtime, "conversation_agent", None), "enabled", False):
        if old_db is None:
            os.environ.pop("MWA_DB_PATH", None)
        else:
            os.environ["MWA_DB_PATH"] = old_db
        return 2, {"错误": "模型未启用（key 未配/未识别），录到的会是规则降级路径，不是线上主链路；拒绝录制"}

    client = TestClient(app)
    captured: list[dict[str, Any]] = []
    original = AgentToolbox.execute

    def tracing_execute(self, name, arguments):
        result = original(self, name, dict(arguments or {}))
        captured.append({"tool": name, "arguments": dict(arguments or {}), "result": result})
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "kind": "meta", "git": _git_head(), "casesDigest": _file_digest(cases_path),
        "planningAsOf": os.environ.get("PLANNING_AS_OF", "(未钉死，按当天)"),
        "dbSource": f"{src.name}{'（已复制副本）' if copied else '（空库）'}",
        "dbWorkCopy": str(work_db),
        "model": getattr(runtime.conversation_agent, "model", "unknown"),
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "cases": len(cases), "schema": "online-eval-trace-v1",
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    selected = cases[:limit] if limit else cases
    from unittest.mock import patch
    with patch.object(AgentToolbox, "execute", tracing_execute):
        for case in selected:
            print(f"  录制 {case['id']}（{len(case.get('轮') or [])} 轮）", flush=True)
            for row in _record_one(client, runtime, memory_manager, case, captured):
                lines.append(json.dumps(row, ensure_ascii=False, default=str))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if old_db is None:
        os.environ.pop("MWA_DB_PATH", None)
    else:
        os.environ["MWA_DB_PATH"] = old_db
    recorded_turns = sum(1 for r in lines[1:] if json.loads(r).get("kind") == "turn")
    errors = sum(1 for r in lines[1:] if json.loads(r).get("kind") == "record_error")
    return (2 if errors else 0), {"录制用例": len(selected), "成功轮数": recorded_turns,
                                   "录制失败用例": errors, "文件": str(path), "meta": meta}


# ── replay：离线确定性判分 ──────────────────────────────────────────────

def mode_replay(cases: list[dict[str, Any]], path: Path, allow_stale: bool,
                baseline: Path | None, write_baseline: bool,
                cases_path: Path = CASES_PATH,
                coverage_required: bool = True) -> tuple[int, dict[str, Any]]:
    meta, traces = _load_traces(path)
    if not traces:
        return 2, {"错误": f"没有录制轨迹 {path}；先跑 --mode record。未录制不算通过。"}
    stale = []
    if meta.get("git") and meta["git"] != _git_head():
        stale.append(f"录制于 {meta['git']}，当前 {_git_head()}")
    if meta.get("casesDigest") != _file_digest(cases_path):
        stale.append("用例文件已改动，录制基线对应的不是这套题")
    if stale and not allow_stale:
        return 2, {"错误": "录制基线过期：" + "；".join(stale) + "（--allow-stale 可强制回放）", "标注": stale}

    results = []
    for case in cases:
        rows = traces.get(case["id"]) or []
        evs = [Evidence.from_dict(r["evidence"]) for r in rows]
        results.append(evaluate_case(case, evs))

    recorded = {c["id"] for c in cases if traces.get(c["id"])}
    missing = [c["id"] for c in cases if c["id"] not in recorded]
    # 「没录到」不是产品失败，但也不是通过：分两类报，别把不完整说成红灯
    failed = [r for r in results if r.failures]
    unobserved = [r for r in results if not r.passed and r not in failed]
    passed_count = sum(1 for r in results if r.passed)
    regressions: list[str] = []
    if baseline and baseline.exists() and write_baseline:
        old = {x["id"]: x["passed"] for x in json.loads(baseline.read_text(encoding="utf-8")).get("results", [])}
        now = {r.case_id: r.passed for r in results}
        regressions = sorted(k for k, v in old.items() if v and k in now and not now[k])
    if baseline and write_baseline:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps({
            "results": [{"id": r.case_id, "passed": r.passed} for r in results],
            "git": meta.get("git"), "recordedAt": meta.get("recordedAt"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    cov = coverage(cases)
    report = {
        "用例数": len(cases), "有轨迹": len(recorded), "缺轨迹": missing,
        "通过": passed_count, "失败": [r.case_id for r in failed],
        "有未观测": [r.case_id for r in unobserved],
        "退化": regressions, "录制基线": {k: meta.get(k) for k in ("mode", "baseUrl", "git", "recordedAt", "model", "planningAsOf")},
        "过期标注": stale, "覆盖": cov,
        "覆盖闸门": "生效（全量）" if coverage_required else "跳过（--only 单条调试，只看判分）",
        "明细": [r.to_dict() for r in results if not r.passed],
    }
    incomplete = bool(missing or unobserved or stale
                      or (coverage_required and not cov["ok"]) or regressions)
    if failed:
        return 1, report
    return (2 if incomplete else 0), report


# ── 报告 ────────────────────────────────────────────────────────────────

def print_coverage(cov: dict[str, Any], cases: list[dict[str, Any]] | None = None) -> None:
    print("=== 矩阵覆盖 ===")
    if cases is not None:
        print(matrix_text(cases))
    print(f"用例 {cov['cases']} 条 / {cov['turns']} 轮；来源分布 {cov['sources']}")
    print(f"覆盖格子 {cov['cells']}/{cov['total_cells']} = {cov['cell_ratio']:.0%}"
          f"（闸门：每行>={MIN_PER_STAGE}、每列>={MIN_PER_ANGLE}、总覆盖>={MIN_CELL_COVERAGE:.0%}）")
    print("  纵向：" + "  ".join(f"{s}={cov['rows'][s]}" for s in STAGES))
    print("  横向：" + "  ".join(f"{a}={cov['cols'][a]}" for a in ANGLES))
    if cov["missing_critical"]:
        print("  关键空格：" + ", ".join(cov["missing_critical"]))
    if cov["thin_rows"] or cov["thin_cols"]:
        print(f"  薄行 {cov['thin_rows']}；薄列 {cov['thin_cols']}")
    print("  结论：" + ("达标" if cov["ok"] else "不达标"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="线上 Agent 全链路评测矩阵")
    parser.add_argument("--mode", choices=["check", "record", "replay", "verify"], default="check")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--traces", type=Path, default=TRACES_PATH)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--limit", type=int, default=None, help="record 只录前 N 条")
    parser.add_argument("--only", default=None, help="只处理指定 caseId（重录/调试单条）")
    parser.add_argument("--url", default=None, help="record 打真实线上站点（如 https://xxx/?sharecode=...）")
    parser.add_argument("--sharecode", default=None, help="线上站点需要的 sharecode")
    parser.add_argument("--timeout", type=int, default=120, help="远端单次请求超时秒数")
    parser.add_argument("--db", type=Path, default=None, help="record 用作数据源的库（默认复制现有库副本）")
    parser.add_argument("--allow-stale", action="store_true")
    args = parser.parse_args(argv)

    if not args.cases.exists():
        print(f"用例文件不存在：{args.cases}")
        return 2
    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"没有这个用例：{args.only}")
            return 2

    if args.mode == "check":
        code, report = mode_check(cases)
        print_coverage(report["覆盖"], cases)
        print(f"=== 用例合规 === {len(report['用例问题'])} 项问题")
        for p in report["用例问题"]:
            print("  ✗", p)
        print(f"=== 判分器自检 === {len(report['自检问题'])} 项问题"
              "（合成证据验证 pass / fail / unobserved 三态）")
        for p in report["自检问题"]:
            print("  ✗", p)
        print(f"退出码 {code}（0=完整且通过 / 1=失败 / 2=不完整）")
        return code

    if args.mode == "record":
        code, report = mode_record(cases, args.traces, args.limit, args.db, args.cases,
                                   args.url, args.sharecode, args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return code

    write_baseline = args.mode == "verify"
    code, report = mode_replay(cases, args.traces, args.allow_stale,
                               args.baseline if write_baseline else None, write_baseline,
                               args.cases, coverage_required=args.only is None)
    print_coverage(report.get("覆盖", coverage(cases)), cases)
    print("=== 回放结果 ===")
    for key in ("用例数", "有轨迹", "缺轨迹", "通过", "失败", "有未观测", "退化",
                "过期标注", "覆盖闸门", "录制基线", "错误"):
        if key in report:
            value = report[key]
            if isinstance(value, list) and len(value) > 12:
                value = value[:12] + [f"... 共 {len(value)} 项"]
            print(f"  {key}: {value}")
    for item in report.get("明细", [])[:8]:
        print(f"  [{item['id']}] 失败 {item['failures'][:3]}；未观测 {item['unobserved'][:3]}")
    print(f"退出码 {code}（0=完整且通过 / 1=失败 / 2=不完整）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
