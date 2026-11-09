"""线上 Agent 全链路评测：纵向环节 × 横向角度的矩阵定义与用例校验。

设计立场（针对「本地全绿、线上一测就坏」）：

1. 被测对象是**线上那条链路本身**，不是内核。入口固定为 /api/agent/message，
   证据必须来自真实回合的 response + trace + working memory + job + 工具返回信封。
2. 纵向 = 请求从进门到落地的 8 个环节；横向 = 每个环节都要问的 9 类问题。
   一条多轮对话可以同时打多个格，但每一行、每一列、每一个关键交叉格都必须有主。
3. 用例不是「标准答案文本」，是一组**可机械判定的断言**。断言要证据，
   证据没录到就记「未观测」，不许算通过——这是和旧评测最大的区别。

本模块不 import 任何产品运行时，纯数据 + 校验，pytest 收集期即可安全导入。
"""
from __future__ import annotations

from typing import Any

# ── 纵向：链路上的每个环节 ──────────────────────────────────────────────
# 证据列里的名字对应 online_scorers.Evidence 的字段。
STAGES: dict[str, dict[str, str]] = {
    "L1": {
        "名": "接入与定型",
        "定义": "HTTP 入参、会话/版本上下文、engine 选择、actionType 最终定型",
        "代码": "api/agent.py:23-54；agent_runtime.py:441-459、1382-1409",
        "证据": "response.actionType / trace.route / HTTP 状态码",
    },
    "L2": {
        "名": "会话状态",
        "定义": "pendingChange 预览/确认/取消/重复确认/切版、澄清选项累积、跨轮旋钮表",
        "代码": "agent_runtime.py:461-516、674-685；db/models.py:122-139",
        "证据": "memory.activeFilters.pendingChange / pendingBatch / latestResultId",
    },
    "L3": {
        "名": "理解与工具选择",
        "定义": "4 轮 tool_choice=auto、9 个工具该调谁、参数对不对、概念题不该调",
        "代码": "conversation_agent.py:154-251；agent_tools.py:209-568",
        "证据": "trace.toolCalls[].tool/arguments/status",
    },
    "L4": {
        "名": "工具契约",
        "定义": "solve 预览、submit_change 门槛、契约独占输出、公式/表格原样、状态枚举",
        "代码": "agent_tools.py:1596-1785；conversation_agent.py:59-65、144-152",
        "证据": "toolResults[].result.status / data.markdown / verbatim",
    },
    "L5": {
        "名": "求解与四态",
        "定义": "作业建单、轮询、needsUserDecision、UNKNOWN/FEASIBLE/OPTIMAL/INFEASIBLE、proof",
        "代码": "agent_runtime.py:687-705、953-979；solve_jobs.py:143-161",
        "证据": "response.solveEvidence / solveJob / jobs 快照",
    },
    "L6": {
        "名": "回答与出处",
        "定义": "数字可溯源、状态诚实、越权、禁词、给退路、说人话、不甩内部编号",
        "代码": "answer_grounding.py:92-135；conversation_agent.py:364-409",
        "证据": "response.assistantMessage.content + toolResults 原文",
    },
    "L7": {
        "名": "排程语义",
        "定义": "少修告警、不修惩罚计价窗、公式含 z_i、模型指纹、成本口径一致",
        "代码": "agent_tools.py:850-...；yxcore/pipeline.py；model_artifact.py",
        "证据": "toolResults[].result.data / 模型产物 / version.modelFingerprint",
    },
    "L8": {
        "名": "闭环与异常",
        "定义": "ledger 下发/审批/完工幂等、降级标记、key 缺失静默降级、超时、重启中断",
        "代码": "agent_tools.py:1841-1914；agent_runtime.py:532-537、234-242",
        "证据": "toolResults(ledger) / response.degraded / job.status",
    },
}

# ── 横向：每个环节都要问的角度 ────────────────────────────────────────
ANGLES: dict[str, dict[str, str]] = {
    "A": {"名": "正确性", "定义": "该做的做了、落点对、动作类型对"},
    "B": {"名": "完整性", "定义": "一句话多个诉求不丢、表格公式不缺块、多工具输出都在"},
    "C": {"名": "事实性", "定义": "数字有出处、不凭记忆补造、数据时点与性质说清"},
    "D": {"名": "状态诚实", "定义": "未证最优不说最优、算不完不说无解、proof 与状态一致"},
    "E": {"名": "权限与确认", "定义": "先预览后确认、未确认不声称完成、不越权写盘"},
    "F": {"名": "跨轮一致性", "定义": "新增/修订/撤销/取消/重复确认/切版，状态不串味"},
    "G": {"名": "稳健性", "定义": "模糊话术、指代、纠错、异常降级、兜底不静默"},
    "H": {"名": "回答质量", "定义": "可读、给下一步、说人话、不甩内部编号、篇幅合理"},
    "I": {"名": "证据链", "定义": "traceId、工具轨迹、模型指纹、产物可回放"},
}

# 关键交叉格：这些格子没有用例，矩阵就是空的，直接判覆盖不足。
CRITICAL_CELLS: set[tuple[str, str]] = {
    ("L1", "A"), ("L1", "G"), ("L1", "I"),
    ("L2", "E"), ("L2", "F"), ("L2", "G"),
    ("L3", "A"), ("L3", "B"), ("L3", "C"), ("L3", "G"),
    ("L4", "B"), ("L4", "E"), ("L4", "H"),
    ("L5", "D"), ("L5", "E"), ("L5", "I"),
    ("L6", "C"), ("L6", "D"), ("L6", "E"), ("L6", "H"),
    ("L7", "C"), ("L7", "H"), ("L7", "I"),
    ("L8", "E"), ("L8", "G"), ("L8", "I"),
}

# 覆盖度闸门：不是「差不多就行」，是逐行逐列数出来的。
MIN_PER_STAGE = 5
MIN_PER_ANGLE = 5
MIN_CELL_COVERAGE = 0.60  # 72 格至少覆盖 60%

# 来源分类：不允许把编排的话术冒充线上实录。
SOURCES = {
    "线上实录": "用户在真实网站上说过的原话（含文档明确记录的翻车原话）",
    "实录衍生": "以线上实录原话为骨架，补承接/换上下文形成的多轮对话",
    "历史话术编排": "来自既有评测集/场景矩阵/实测报告的产品话术，不是用户真说过",
    "矩阵补格": "为覆盖特定链路环节或异常分支而构造的话术",
}

# 每轮允许的断言键。判分器与校验器共用同一张表，避免写了断言却没人判。
ASSERTION_KEYS: dict[str, str] = {
    "动作": "动作：str 或 list[str]，actionType 必须落在允许集合",
    "必调工具": "必调工具：list[str]，trace.toolCalls 必须覆盖",
    "禁调工具": "禁调工具：list[str]，trace.toolCalls 不得出现",
    "工具状态": "工具状态：{工具名: status}，工具信封状态必须命中",
    "正文含": "正文含：list[str]，回复正文必须含全部片段",
    "正文不含": "正文不含：list[str]，回复正文不得含任何片段",
    "数字可溯源": "数字可溯源：true 时用 grounding_report 复算，不得有未溯源数字",
    "状态": "状态：{status, proof}，校验 solveEvidence 且自动加状态诚实禁词",
    "选项至少": "选项至少：int，clarificationOptions 条数下限",
    "预览": "预览：有 / 无 / 澄清，检查 memory.activeFilters.pendingChange.kind",
    "作业": "作业：{status?, solverStatus?, needsUserDecision?}",
    "降级": "降级：null 表示不该降级；或 {kind:...} 表示必须带该降级标记",
    "工具数至少": "工具数至少：int，该轮工具调用条数下限",
    "有指纹": "有指纹：true 时 trace.modelFingerprint 或 solveEvidence 必须带指纹",
    "回答质量": "回答质量：{给退路[], 不甩内部编号, 最长字, 必含结构[]}",
    "工具返回含": "工具返回含：{工具名: [片段]}，工具信封 data 原文必须含",
}

TERMINAL_KEYS: dict[str, str] = {
    "预览": "终局 pendingChange 应为 有 / 无 / 澄清",
    "作业数": "终局该会话累计求解作业数",
    "同一会话": "true 时所有轮必须共用同一 conversationId",
    "重复求解": "false 时不允许同一预览派生出两个作业",
    "状态": "终局作业/证据状态",
}


class CaseError(ValueError):
    """用例写得不合规——这是尺子的问题，必须单独报，不能混进产品失败。"""


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    """校验用例集本身。返回问题描述列表，空列表表示尺子合格。"""
    problems: list[str] = []
    ids: set[str] = set()
    for case in cases:
        cid = case.get("id", "(缺 id)")
        if cid in ids:
            problems.append(f"{cid}：id 重复")
        ids.add(cid)
        for key in ("id", "名", "来源", "出处", "纵", "横", "轮"):
            if key not in case:
                problems.append(f"{cid}：缺字段 {key}")
        if case.get("来源") not in SOURCES:
            problems.append(f"{cid}：来源 {case.get('来源')!r} 不在登记分类里")
        stages, angles = case.get("纵") or [], case.get("横") or []
        if not stages or not angles:
            problems.append(f"{cid}：纵/横 标签不能为空")
        for s in stages:
            if s not in STAGES:
                problems.append(f"{cid}：未知环节 {s}")
        for a in angles:
            if a not in ANGLES:
                problems.append(f"{cid}：未知角度 {a}")
        turns = case.get("轮") or []
        if not turns:
            problems.append(f"{cid}：至少一轮")
        for i, turn in enumerate(turns, 1):
            if not turn.get("话术"):
                problems.append(f"{cid} 第{i}轮：缺话术")
            expect = turn.get("期望") or {}
            if not expect:
                problems.append(f"{cid} 第{i}轮：没有任何期望判据（不许有白卷用例）")
            for key in expect:
                if key not in ASSERTION_KEYS:
                    problems.append(f"{cid} 第{i}轮：未知断言键 {key}（判分器不会判它）")
        for key in case.get("终局") or {}:
            if key not in TERMINAL_KEYS:
                problems.append(f"{cid}：未知终局断言键 {key}")
    return problems


def coverage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """数出矩阵覆盖度。行、列、关键格三个口径分开报，不许只给一个总分。"""
    row: dict[str, int] = {s: 0 for s in STAGES}
    col: dict[str, int] = {a: 0 for a in ANGLES}
    cells: set[tuple[str, str]] = set()
    sources: dict[str, int] = {k: 0 for k in SOURCES}
    turns = 0
    for case in cases:
        sources[case.get("来源", "矩阵补格")] = sources.get(case.get("来源", "矩阵补格"), 0) + 1
        turns += len(case.get("轮") or [])
        for s in case.get("纵") or []:
            if s in row:
                row[s] += 1
        for a in case.get("横") or []:
            if a in col:
                col[a] += 1
        for s in case.get("纵") or []:
            for a in case.get("横") or []:
                if s in STAGES and a in ANGLES:
                    cells.add((s, a))
    total_cells = len(STAGES) * len(ANGLES)
    missing_critical = sorted(f"{s}×{a}" for s, a in CRITICAL_CELLS if (s, a) not in cells)
    thin_rows = [f"{s}({n})" for s, n in row.items() if n < MIN_PER_STAGE]
    thin_cols = [f"{a}({n})" for a, n in col.items() if n < MIN_PER_ANGLE]
    ratio = len(cells) / total_cells
    ok = not missing_critical and not thin_rows and not thin_cols and ratio >= MIN_CELL_COVERAGE
    return {
        "cases": len(cases), "turns": turns, "sources": sources,
        "rows": row, "cols": col, "cells": len(cells), "total_cells": total_cells,
        "cell_ratio": round(ratio, 3), "missing_critical": missing_critical,
        "thin_rows": thin_rows, "thin_cols": thin_cols, "ok": ok,
    }


def matrix_text(cases: list[dict[str, Any]]) -> str:
    """文本热力图：每个格子标该格用例数，0 表示空格。"""
    counts = {(s, a): 0 for s in STAGES for a in ANGLES}
    for case in cases:
        for s in case.get("纵") or []:
            for a in case.get("横") or []:
                if (s, a) in counts:
                    counts[(s, a)] += 1
    lines = ["      " + " ".join(f"{a:>3}" for a in ANGLES) + "   行合计",
             "      " + " ".join(f"{ANGLES[a]['名'][:2]:>3}" for a in ANGLES)]
    for s in STAGES:
        row = [counts[(s, a)] for a in ANGLES]
        cells = " ".join(f"{n:>3}" if n else "  ·" for n in row)
        lines.append(f"{s} {STAGES[s]['名']:<6}" + cells + f"   {sum(row)}")
    lines.append("列合计 " + " ".join(f"{sum(counts[(s, a)] for s in STAGES):>3}" for a in ANGLES))
    return "\n".join(lines)
