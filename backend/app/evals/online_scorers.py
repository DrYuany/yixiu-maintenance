"""线上全链路评测的确定性判分器。

判分纪律三条，都是被「全绿但线上坏」逼出来的：

1. **不用 LLM 给 LLM 打分**。模型只负责产出，判分全部是代码比对。
2. **断言必须有证据**。证据没录到 → 状态记 `unobserved`，既不算通过也不算失败，
   但会让这一轮 `passed=False`，并在报告里单列。旧评测把「没跑」算进「全部通过」，
   这里不许。
3. **证据来自真实回合**：response + trace + working memory + job + 工具返回信封，
   不是手写 fixture。回放判的是录下来的那一次真实回合。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.evals.online_matrix import ASSERTION_KEYS

# 状态四态对应的「不许说」的话。与 tool_scorers.status_honest 同口径，
# 但这里直接从 status+proof 推出来，不要求用例再写一遍禁词。
_FALSE_INFEASIBLE = ("确认无解", "模型无解", "没有可行解", "已证明不可行", "确定无解")
_FALSE_OPTIMAL = ("已证明最优", "全局最优", "确定最优", "最优方案")
_COMPLETED_CLAIMS = ("已生成方案", "已求解", "已修改数据", "已下发", "已导出文件", "已生成检修单")
_INTERNAL_IDS = re.compile(r"(?:fp_|trace_|job_|kind\s*=|bucket_max|scope_only|cap_max|pendingChange|activeFilters)")

# answer_grounding 的契约是「标注，不是拦截」（见该模块文件头）：模型自己算出来的
# 数（12000×1.2=14400）、系统参数（14 天展望期）本来就追不到工具出处，产品要做的
# 是把它们标出来让用户自己判断。判分查的是「有没有如实标注」，不是「有没有这类数字」。
_GROUNDING_CAVEAT_RE = re.compile(
    r"未经系统核实|没有找到出处|以工具返回的表格为准|未经核实"
)

# 「给退路」不能只靠用例里猜的几个词：产品措辞会变，但「明确告诉用户下一步」
# 有几类固定说法。这张表刻意只收带动作/承接的短语，不收「确认」「可以」这类
# 到处都有的词，避免把真没给退路的回答也放过。
_EXIT_HINTS = (
    "算完会", "自动出方案卡", "自动出", "后台跑", "稍等", "稍后",
    "您可以", "你可以", "可以说", "您说", "照新说法", "重新说",
    "二选一", "我可以先", "得先", "需要后端", "需要您", "请先",
    "再跑", "放宽", "告诉我就行", "你想怎么排", "等台账", "你说一句",
    # 拒绝类回答的承接说法：明确把「那你能做什么」交回给用户，也是退路。
    # 只收带动作/承接的整句片段，不收「可以」「确认」这类到处都有的词。
    "告诉我", "我先出预览", "如果你的实际目的是", "这个可以做", "要走这条路",
)

PASS, FAIL, UNOBSERVED = "pass", "fail", "unobserved"


@dataclass
class TurnResult:
    case_id: str
    turn_index: int
    checks: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, reason: str) -> None:
        self.checks.append({"name": name, "status": status, "reason": reason})

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c["status"] == PASS for c in self.checks)

    @property
    def failures(self) -> list[dict[str, str]]:
        return [c for c in self.checks if c["status"] == FAIL]

    @property
    def unobserved(self) -> list[dict[str, str]]:
        return [c for c in self.checks if c["status"] == UNOBSERVED]


@dataclass
class CaseResult:
    case_id: str
    turns: list[TurnResult] = field(default_factory=list)
    terminal: list[dict[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.turns) and all(t.passed for t in self.turns) and all(
            c["status"] == PASS for c in self.terminal
        )

    @property
    def failures(self) -> list[str]:
        out = [f"第{t.turn_index}轮 {c['name']}：{c['reason']}"
               for t in self.turns for c in t.failures]
        out += [f"终局 {c['name']}：{c['reason']}" for c in self.terminal if c["status"] == FAIL]
        return out

    @property
    def unobserved(self) -> list[str]:
        out = [f"第{t.turn_index}轮 {c['name']}：{c['reason']}"
               for t in self.turns for c in t.unobserved]
        out += [f"终局 {c['name']}：{c['reason']}" for c in self.terminal if c["status"] == UNOBSERVED]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id, "passed": self.passed,
            "failures": self.failures, "unobserved": self.unobserved,
            "turns": [{"index": t.turn_index, "checks": t.checks} for t in self.turns],
            "terminal": self.terminal,
        }


# ── 证据容器 ────────────────────────────────────────────────────────────
# 由 run_online_eval.py 的 record 模式逐轮填好，replay 模式只读不算。
@dataclass
class Evidence:
    response: dict[str, Any]
    trace: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    job: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] | None = None
    http_status: int = 200
    conversation_id: str = ""
    job_count: int = 0
    utterance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "response": self.response, "trace": self.trace, "memory": self.memory,
            "job": self.job, "tool_results": self.tool_results,
            "http_status": self.http_status, "conversation_id": self.conversation_id,
            "job_count": self.job_count, "utterance": self.utterance,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Evidence":
        return cls(**{k: raw.get(k) for k in cls.__dataclass_fields__})


def _text(ev: Evidence) -> str:
    return str(((ev.response.get("assistantMessage") or {}).get("content")) or "")


def _norm(text: str) -> str:
    return "".join(text.split()).lower()


def _tools(ev: Evidence) -> list[str] | None:
    if ev.trace is None:
        return None
    return [str(c.get("tool", "")) for c in (ev.trace.get("toolCalls") or [])]


def _pending(ev: Evidence) -> str:
    """返回 有 / 无 / 澄清；memory 没录到时返回 unobserved 标记。"""
    if ev.memory is None:
        return UNOBSERVED
    filters = ev.memory.get("activeFilters") or {}
    pending = filters.get("pendingChange")
    if not isinstance(pending, dict):
        return "无"
    return "澄清" if pending.get("kind") == "clarify" else "有"


def _tool_envelopes(ev: Evidence) -> list[dict[str, Any]] | None:
    return ev.tool_results


def _grounding_sources(ev: Evidence) -> list[str] | None:
    if ev.tool_results is None:
        return None
    sources = [ev.utterance]
    for item in ev.tool_results:
        result = item.get("result")
        if result is not None:
            sources.append(json.dumps(result, ensure_ascii=False, default=str))
    return sources


# ── 逐项判分 ────────────────────────────────────────────────────────────

def evaluate_turn(case_id: str, index: int, expect: dict[str, Any], ev: Evidence) -> TurnResult:
    r = TurnResult(case_id=case_id, turn_index=index)
    text, norm = _text(ev), _norm(_text(ev))

    if "动作" in expect:
        allowed = expect["动作"] if isinstance(expect["动作"], list) else [expect["动作"]]
        got = ev.response.get("actionType")
        r.add("动作", PASS if got in allowed else FAIL,
              f"期望 {allowed}，得到 {got!r}")

    for key, forbidden in (("必调工具", False), ("禁调工具", True)):
        if key not in expect:
            continue
        tools = _tools(ev)
        if tools is None:
            r.add(key, UNOBSERVED, "该轮没有录到 trace.toolCalls，判不了工具选择")
            continue
        wanted = list(expect[key] or [])
        offenders = [w for w in wanted if (w in tools) == forbidden]
        if offenders:
            r.add(key, FAIL, ("不该调却调了 " if forbidden else "该调没调 ") + ", ".join(offenders)
                  + f"；实际 {tools}")
        else:
            r.add(key, PASS, "ok")

    if "工具状态" in expect:
        envelopes = _tool_envelopes(ev)
        if envelopes is None:
            r.add("工具状态", UNOBSERVED, "该轮没有录到工具返回信封，判不了 status")
        else:
            for tool, want in (expect["工具状态"] or {}).items():
                got = [e.get("result", {}).get("status") for e in envelopes
                       if e.get("tool") == tool and isinstance(e.get("result"), dict)]
                if not got:
                    r.add(f"工具状态[{tool}]", UNOBSERVED, f"没有录到 {tool} 的返回")
                elif want not in got:
                    r.add(f"工具状态[{tool}]", FAIL, f"期望 {want!r}，得到 {got}")
                else:
                    r.add(f"工具状态[{tool}]", PASS, "ok")

    if "工具返回含" in expect:
        envelopes = _tool_envelopes(ev)
        if envelopes is None:
            r.add("工具返回含", UNOBSERVED, "没有录到工具返回信封")
        else:
            for tool, frags in (expect["工具返回含"] or {}).items():
                blob = "\n".join(json.dumps(e.get("result"), ensure_ascii=False, default=str)
                                 for e in envelopes if e.get("tool") == tool)
                missing = [f for f in frags if f not in blob]
                r.add(f"工具返回含[{tool}]", FAIL if missing else PASS,
                      f"缺 {missing}" if missing else "ok")

    for key, present in (("正文含", True), ("正文不含", False)):
        for frag in expect.get(key) or []:
            hit = _norm(str(frag)) in norm
            ok = hit if present else not hit
            r.add(f"{key}[{frag}]", PASS if ok else FAIL,
                  "ok" if ok else f"正文里{'找不到' if present else '出现了'}「{frag}」")

    if expect.get("数字可溯源"):
        sources = _grounding_sources(ev)
        if sources is None:
            r.add("数字可溯源", UNOBSERVED, "没有录到工具返回原文，无法复算数字出处")
        else:
            try:
                from app.core.answer_grounding import grounding_report
                report = grounding_report(text, sources)
                bad = report.get("未溯源") or []
                if not bad:
                    r.add("数字可溯源", PASS,
                          f"检查 {report.get('检查数字', 0)} 个数字，全部有出处")
                elif _GROUNDING_CAVEAT_RE.search(text):
                    r.add("数字可溯源", PASS,
                          f"{len(bad)} 个数字无工具出处，已如实标注 {bad[:5]}")
                else:
                    r.add("数字可溯源", FAIL,
                          f"有 {len(bad)} 个数字无出处却没标注：{bad[:5]}")
            except Exception as exc:  # noqa: BLE001 - 判分器坏了要显式报，不能静默放过
                r.add("数字可溯源", UNOBSERVED, f"复算出错：{exc}")

    if "状态" in expect:
        want = expect["状态"] or {}
        evidence = ev.response.get("solveEvidence")
        if not isinstance(evidence, dict):
            r.add("状态", UNOBSERVED, "响应里没有 solveEvidence，判不了四态")
        else:
            got_status, got_proof = evidence.get("status"), bool(evidence.get("proof"))
            want_status, want_proof = want.get("status"), bool(want.get("proof", False))
            ok = (want_status is None or got_status == want_status) and got_proof == want_proof
            r.add("状态", PASS if ok else FAIL,
                  f"期望 {want_status}/proof={want_proof}，得到 {got_status}/proof={got_proof}")
            # 状态诚实：从状态自动推禁词，用例不必重复写
            bad = []
            if got_status != "INFEASIBLE" or not got_proof:
                bad += [w for w in _FALSE_INFEASIBLE if _norm(w) in norm]
            if got_status != "OPTIMAL" or not got_proof:
                bad += [w for w in _FALSE_OPTIMAL if _norm(w) in norm]
            r.add("状态诚实", FAIL if bad else PASS,
                  f"不该说的结论：{bad}" if bad else "ok")

    if "选项至少" in expect:
        got = len(ev.response.get("clarificationOptions") or [])
        r.add("选项至少", PASS if got >= expect["选项至少"] else FAIL,
              f"期望 >= {expect['选项至少']}，得到 {got}")

    if "预览" in expect:
        got = _pending(ev)
        if got == UNOBSERVED:
            r.add("预览", UNOBSERVED, "该轮没有录到 working memory，判不了 pendingChange")
        else:
            r.add("预览", PASS if got == expect["预览"] else FAIL,
                  f"期望 {expect['预览']}，得到 {got}")

    if "作业" in expect:
        job = ev.job or ev.response.get("solveJob")
        if not isinstance(job, dict):
            r.add("作业", UNOBSERVED, "该轮没有录到作业快照")
        else:
            for key, want in (expect["作业"] or {}).items():
                got = job.get(key)
                r.add(f"作业[{key}]", PASS if got == want else FAIL,
                      f"期望 {want!r}，得到 {got!r}")

    if "降级" in expect:
        degraded = ev.response.get("degraded")
        kind = (degraded or {}).get("kind") if isinstance(degraded, dict) else degraded
        want = expect["降级"]
        want_kind = None if want is None else (want or {}).get("kind")
        r.add("降级", PASS if kind == want_kind else FAIL,
              f"期望 {want_kind!r}，得到 {kind!r}")

    if "工具数至少" in expect:
        tools = _tools(ev)
        if tools is None:
            r.add("工具数至少", UNOBSERVED, "没有录到 trace.toolCalls")
        else:
            r.add("工具数至少", PASS if len(tools) >= expect["工具数至少"] else FAIL,
                  f"期望 >= {expect['工具数至少']}，得到 {len(tools)}")

    if expect.get("有指纹"):
        fp = ((ev.trace or {}).get("modelFingerprint")
              or (ev.response.get("solveEvidence") or {}).get("modelFingerprint")
              or (ev.response.get("solveJob") or {}).get("modelFingerprint"))
        r.add("有指纹", PASS if fp else FAIL, "ok" if fp else "trace/证据/作业都没有模型指纹")

    quality = expect.get("回答质量") or {}
    if "给退路" in quality:
        options = list(quality["给退路"] or [])
        hit = [o for o in options if _norm(o) in norm]
        generic = [h for h in _EXIT_HINTS if _norm(h) in norm]
        ok = bool(hit or generic)
        r.add("回答质量[给退路]", PASS if ok else FAIL,
              f"命中 {hit or generic}" if ok
              else f"正文没给出任何下一步：用例词表 {options} / 通用退路 {len(_EXIT_HINTS)} 类均未命中")
    if quality.get("不甩内部编号"):
        found = _INTERNAL_IDS.findall(text)
        r.add("回答质量[不甩内部编号]", FAIL if found else PASS,
              f"出现内部标识 {found[:5]}" if found else "ok")
    if "最长字" in quality:
        r.add("回答质量[篇幅]", PASS if len(text) <= quality["最长字"] else FAIL,
              f"{len(text)} 字，上限 {quality['最长字']}")
    if "必含结构" in quality:
        missing = [s for s in quality["必含结构"] if _norm(s) not in norm]
        r.add("回答质量[必含结构]", FAIL if missing else PASS,
              f"缺 {missing}" if missing else "ok")

    # 越权：预览态下不许声称已完成。只要断言里出现「预览=有」或动作是预览类就自动查。
    if expect.get("预览") == "有" or ev.response.get("actionType") in {
            "change_preview", "clarification", "batch_preview", "assistant_reply"}:
        offenders = [w for w in _COMPLETED_CLAIMS if _norm(w) in norm]
        # assistant_reply 里「已生成方案」可能是复述用户的话，只在预览类严格判
        if ev.response.get("actionType") in {"change_preview", "clarification", "batch_preview"}:
            r.add("越权", FAIL if offenders else PASS,
                  f"未确认就声称完成：{offenders}" if offenders else "ok")

    unknown = [k for k in expect if k not in ASSERTION_KEYS]
    if unknown:
        r.add("用例合规", FAIL, f"存在判分器不认识的断言键：{unknown}")
    return r


def evaluate_case(case: dict[str, Any], evidences: list[Evidence]) -> CaseResult:
    cid = case["id"]
    result = CaseResult(case_id=cid)
    turns = case.get("轮") or []
    if len(evidences) != len(turns):
        result.terminal.append({
            "name": "录制完整", "status": UNOBSERVED,
            "reason": f"用例 {len(turns)} 轮，录到 {len(evidences)} 轮",
        })
    for i, (turn, ev) in enumerate(zip(turns, evidences), 1):
        result.turns.append(evaluate_turn(cid, i, turn.get("期望") or {}, ev))
    terminal = case.get("终局") or {}
    if terminal and evidences:
        last = evidences[-1]
        if "预览" in terminal:
            got = _pending(last)
            status = UNOBSERVED if got == UNOBSERVED else (PASS if got == terminal["预览"] else FAIL)
            result.terminal.append({"name": "终局预览", "status": status,
                                   "reason": f"期望 {terminal['预览']}，得到 {got}"})
        if "作业数" in terminal:
            ok = last.job_count == terminal["作业数"]
            result.terminal.append({"name": "终局作业数", "status": PASS if ok else FAIL,
                                   "reason": f"期望 {terminal['作业数']}，得到 {last.job_count}"})
        if terminal.get("同一会话"):
            ids = {e.conversation_id for e in evidences}
            ok = len(ids) == 1 and "" not in ids
            result.terminal.append({"name": "同一会话", "status": PASS if ok else FAIL,
                                   "reason": f"出现会话 id：{sorted(ids)}"})
        if terminal.get("重复求解") is False:
            ok = last.job_count <= 1
            result.terminal.append({"name": "不重复求解", "status": PASS if ok else FAIL,
                                   "reason": f"累计作业数 {last.job_count}"})
        if "状态" in terminal:
            job = last.job or last.response.get("solveJob")
            got = (job or {}).get("solverStatus") if isinstance(job, dict) else None
            ok = got == terminal["状态"]
            result.terminal.append({"name": "终局状态", "status": PASS if ok else FAIL,
                                   "reason": f"期望 {terminal['状态']}，得到 {got}"})
    return result
