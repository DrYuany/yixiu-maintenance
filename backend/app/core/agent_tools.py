from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.version_compare import compare_versions
from app.core.solve_jobs import SolveJobRepository
from app.core.tool_runtime import ToolRuntime, ToolRuntimeError
from app.core.version_reference import chronological_versions
from app.db.models import ModelArtifactRepository, VersionRepository
from app.schemas.agent import WindowVersion


SHANGHAI_TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 任务状态翻成人话。机器码给程序看，这句话给模型看——
# 模型照着 "RUNNING" 编不出一句自然的中文，照着"正在算"可以。
_JOB_STATUS_ZH = {
    "QUEUED": "排队中",
    "RUNNING": "正在算",
    "COMPLETED": "算完了",
    "FAILED": "算失败了",
    "CANCELLED": "已取消",
}


def _job_facts(job) -> dict[str, Any]:
    """一个求解任务的事实摘要。

    刻意不吐 result 全文——那是整份排程，几十 KB，塞进上下文会把用户
    这句话本身挤掉。这里只回答「算出来是哪一版、结论是什么」，
    要看细节让模型去调 read_versions。
    """
    result = job.result or {}
    summary = result.get("summary")
    if isinstance(summary, dict):
        summary_text = json.dumps(summary, ensure_ascii=False)[:400]
    else:
        summary_text = str(summary or "")[:400]
    return {
        "jobId": job.id,
        "status": job.status,
        "statusZh": _JOB_STATUS_ZH.get(job.status, job.status),
        "solverStatus": job.solverStatus,
        "phase": job.phase,
        "progressPercent": job.progressPercent,
        "statusMessage": job.statusMessage,
        "elapsedSeconds": job.elapsedSeconds,
        "submittedAt": job.submittedAt,
        "finishedAt": job.finishedAt,
        "needsUserDecision": job.needsUserDecision,
        "versionId": result.get("versionId"),
        "summary": summary_text,
        "error": job.error,
    }


def _cost_items_from_output(version: WindowVersion) -> dict[str, Any] | None:
    """旧版本摘要是分项上线前写的，回读该次求解的输出快照补上，不重算。"""
    fingerprint = version.modelFingerprint
    if not fingerprint:
        return None
    path = (
        PROJECT_ROOT
        / "product-output"
        / f"agent-model-{fingerprint[:16]}"
        / "结果.json"
    )
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recommendation = report.get("推荐") or {}
    if not isinstance(recommendation, dict) or not recommendation:
        return None
    return {
        "船租": recommendation.get("船租"),
        "人工": recommendation.get("人工"),
        "备件": recommendation.get("备件"),
        "其他现场费用": recommendation.get("其他费用"),
        "发电损失": recommendation.get("发电损失"),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _money(value: Any) -> float | None:
    try:
        return round(float(value) / 10_000, 2)
    except (TypeError, ValueError):
        return None


def _solver(version: WindowVersion) -> dict[str, Any]:
    return _dict((version.resultSummary or {}).get("solver"))


def _status(version: WindowVersion) -> str:
    value = (
        _solver(version).get("解状态")
        or _solver(version).get("求解状态")
        or _solver(version).get("状态")
    )
    return str(value) if value else "UNKNOWN"


def _window(version: WindowVersion) -> dict[str, Any]:
    windows = version.windows or []
    if not windows:
        return {"id": None, "start": None, "end": None}
    item = windows[0]
    return {
        "id": item.windowId,
        "start": item.metrics.startTime,
        "end": item.metrics.endTime,
    }


def _fmt_value(value: Any, unit: str | None = None) -> str:
    if value is None:
        return "未记录"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text} {unit}" if unit else text


class AgentToolbox:
    """Read-only facts for one conversation, exposed to the conversational agent."""

    def __init__(
        self,
        *,
        conversation_id: str,
        versions: VersionRepository,
        model_artifacts: ModelArtifactRepository | None,
        selected_version_id: str | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        on_pending_change: Callable[[dict[str, Any]], None] | None = None,
        on_tool_trace: Callable[[dict[str, Any]], None] | None = None,
        jobs: SolveJobRepository | None = None,
        on_submit_change: Callable[[str, dict[str, Any], str], dict[str, Any]] | None = None,
        yx_engine: Any | None = None,
        user_message: str | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.versions = versions
        self.model_artifacts = model_artifacts
        self.selected_version_id = selected_version_id
        self.on_tool = on_tool
        self.on_pending_change = on_pending_change
        self.on_tool_trace = on_tool_trace
        # 用户这一轮的**原话**。solve 拿它跟解析出的 DSL 对账，不能拿模型自己
        # 转写的 instruction 当原话——那等于让被核查的人自己写核查材料。
        self.user_message = (user_message or "").strip() or None
        # 求解任务仓库：read_jobs 靠它看活，submit_change 靠它派活。
        # 不传就是「这套工具箱没有求解能力」，两个工具会明确说做不到，而不是假装有。
        self.jobs = jobs
        # 派活的出口交给 runtime——它才拿得到 memory、job_pool 和求解执行体。
        # 在 toolbox 里另写一套派活逻辑，等于复制出一条迟早会分叉的路。
        self.on_submit_change = on_submit_change
        # 槽位引擎：solve 工具靠它把「改排程」翻译成槽位补丁（小一修的理解层）。
        self.yx_engine = yx_engine
        self.pending_change: dict[str, Any] | None = None
        # 这一轮到底动过待确认没有：动过但最后没剩下，说明要清掉上一轮遗留的那份。
        self.pending_touched = False
        self.ordered = chronological_versions(
            versions.list_by_conversation(conversation_id)
        )
        self.plan_numbers = {
            item.id: index for index, item in enumerate(self.ordered, start=1)
        }
        self._trace: list[dict[str, Any]] = []
        self._specs = {spec["name"]: spec for spec in self.tool_specs()}
        self.runtime = ToolRuntime(read_only_tools={"read_versions", "read_environment", "read_catalog", "read_alarms", "read_site", "read_jobs"})

    # ------------------------------------------------------------------ tools
    def tool_specs(self) -> list[dict[str, Any]]:
        version_arg = {
            "type": "string",
            "description": "方案引用：方案01、01、1、最新、上一版，或 ver_xxxxxxxx。省略时使用当前选中方案。",
        }
        return [
            {
                "name": "read_versions",
                "description": (
                    "读取方案事实，只读。不传 versions 列出本会话全部方案（编号、版本 ID、状态、关键指标）；"
                    "传 1 个给单方案事实；传 2 个及以上给同一张指标矩阵、相对基准的差值、"
                    "任务存在性变化和可比性限制，供你解读，不替你下结论。"
                    "include 按需追加：tasks、unscheduled、model、costs、parameters、constraints、status。"
                    "多版同时读 model 时返回每版的结构化投影，并额外给出 modelDiffs"
                    "（模型指纹、目标层级、约束分组差异），差异已算好，你只负责解读。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "versions": {
                            "type": "array",
                            "items": version_arg,
                            "description": (
                                "要读的方案。省略或空数组表示列出全部方案；"
                                "对比时把用户点到的方案全部放进来，不要只取前两个，也不要逐个分开读。"
                            ),
                        },
                        "baseline": {
                            **version_arg,
                            "description": (
                                "多方案对比时的基准方案，省略则用列表第一个。"
                                "差值一律相对基准计算，不要自己减。"
                            ),
                        },
                        "include": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "tasks",
                                    "unscheduled",
                                    "model",
                                    "costs",
                                    "parameters",
                                    "constraints",
                                    "status",
                                ],
                            },
                            "description": (
                                "附加内容。model 只支持单个方案，多方案时会被拒绝并提示；"
                                "只要目标函数时可配 section=objective。"
                            ),
                        },
                        "section": {
                            "type": "string",
                            "enum": ["all", "objective", "constraints", "variables", "costs"],
                            "description": "include=model 时只返回目标函数、约束、变量或成本构成。",
                        },
                        "focus": {
                            "type": "string",
                            "description": "用户关心的侧重点，例如成本、风险、船天或某个任务；只影响你的解读，不影响事实。",
                        },
                    },
                },
            },
            {
                "name": "read_environment",
                "description": (
                    "读取基座里的环境数据，只读：逐时风速、预测电价、每日海况与可用小时。"
                    "和求解器用同一份快照，带数据时点、数据性质和输入哈希。"
                    "daily/hourly 返回现成 Markdown；chart 返回消息内可交互图表协议。"
                    "data.verbatim=true 时原样输出，不要重排数字。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "description": "从规划时点起取多少天，默认 7，最多 31。",
                        },
                        "detail": {
                            "type": "string",
                            "enum": ["daily", "hourly", "chart", "window"],
                            "description": (
                                "daily 给每日聚合（默认）；hourly 追加逐时序列，最多 7 天；"
                                "chart 一次返回 30 天图表数据，供消息内切换 3/7/14/30 天；"
                                "window 按指定海事窗口返回逐日风况与电价。"
                            ),
                        },
                        "window": {
                            "type": "string",
                            "description": (
                                "按海事窗口取数，例如“窗口02”“目标窗口”。"
                                "传了 window 时返回该窗口内的逐日风况、电价和可用小时，"
                                "而不是规划时点之后的 N 天。"
                            ),
                        },
                        "defaultRange": {
                            "type": "integer",
                            "enum": [3, 7, 14, 30],
                            "description": "detail=chart 时首次展示的范围，默认 7。",
                        },
                    },
                },
            },
            {
                "name": "read_catalog",
                "description": (
                    "读取可配置的目标目录，只读：四种策略、每种的分层顺序、"
                    "可加权指标与默认权重。回答“有哪些目标/指标能加权/怎么设置”时用。"
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "read_alarms",
                "description": (
                    "读取告警与待修任务，只读。覆盖“现在有哪些告警、今天/最近新增了什么、"
                    "高风险待修有哪些、某台风机或某个部件的告警”。"
                    "数据已按规划时点过滤，不会读取未来批次告警；"
                    "返回截至规划时点的待安排清单、严重度分布、每日接收统计和任务状态。"
                    "需要已完成、已取消或历史告警时传 includeClosed=true。"
                    "这是计划快照，不是实时 SCADA 推送。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "enum": ["backlog", "recent", "summary"],
                            "description": (
                                "backlog=当前全部待安排告警（默认）；"
                                "recent=最近 N 天新接收的告警；summary=告警概况与分布。"
                            ),
                        },
                        "days": {
                            "type": "integer",
                            "description": "scope=recent 时回看多少天，默认 7，最大 30。",
                        },
                        "date": {
                            "type": "string",
                            "description": (
                                "按接收日期取新增告警：today、yesterday 或 YYYY-MM-DD。"
                                "传 date 时只看该日，不再按 days 回看。"
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["高", "中", "低"],
                            "description": "只看某个严重等级。",
                        },
                        "status": {
                            "type": "string",
                            "description": (
                                "按任务状态筛选，例如“待安排”“待补充窗口”“已完成”“已取消”。"
                                "查已完成/已取消时会自动包含已关闭任务。"
                            ),
                        },
                        "includeClosed": {
                            "type": "boolean",
                            "description": "是否包含已完成、已取消的历史告警；查历史时设为 true。",
                        },
                        "turbineId": {
                            "type": "string",
                            "description": "按风机编号筛选，例如“风机018”。",
                        },
                        "component": {
                            "type": "string",
                            "description": "按部件筛选，可写部件名称（如“叶片”）或部件编号。",
                        },
                        "taskId": {
                            "type": "string",
                            "description": "按任务编号筛选，例如“任务018”。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少行，默认 40，最大 60。",
                        },
                    },
                },
            },
            {
                "name": "read_site",
                "description": (
                    "读取现场基础数据，只读。scope 覆盖：overview（数据概况与来源）、"
                    "fleet（风机台账）、components（部件与维修工时）、"
                    "windows（海事窗口、目标窗口与跳过规则）、inventory（备件库存与收发计划）、"
                    "work_orders（检修单与出海清单）、execution（执行反馈）、"
                    "parameters（船租、人工、作业线、人员上限等系统参数）。"
                    "检修单或执行反馈为空时，工具会如实说明“当前没有”，"
                    "这不是没有数据域。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "enum": [
                                "overview",
                                "fleet",
                                "components",
                                "windows",
                                "inventory",
                                "work_orders",
                                "execution",
                                "parameters",
                            ],
                            "description": "要读取的现场数据域，默认 overview。",
                        },
                        "turbineId": {
                            "type": "string",
                            "description": "scope=fleet 时只看某台风机，例如“风机018”。",
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "scope=parameters 时按参数名搜索，例如“船租”“候选开工粒度”"
                                "“最多作业线数”；会在 122 项建模参数总表中匹配。"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少行，默认 20，最大 100。",
                        },
                    },
                },
            },
            {
                "name": "read_jobs",
                "description": (
                    "查本对话派出去的求解任务，只读。传 jobId 查指定那一个；不传就列出最近的几个。"
                    "返回状态（排队中/正在算/算完了/失败了/已取消）、进度、已用时、结果摘要。"
                    "用户问「算完了吗」「刚才那个算得怎么样」「还要多久」时用它，"
                    "不要凭印象回答，也不要说「请到方案页面查看」。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "jobId": {
                            "type": "string",
                            "description": "任务号（job_ 开头）。省略时列最近的几个。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "列几个，默认 3，最多 20。",
                        },
                    },
                },
            },
            {
                "name": "solve",
                "description": (
                    "把用户明确提出的排程变更（策略、目标模式、权重、参数、约束、重新排程）"
                    "编译成确定性变更预览。只预览，不会求解、不会写库；"
                    "用户回复确认后系统才会真正求解。引用必须原样保留用户的关键数字和说法。\n"
                    "用户一句话里要算**多个方案**（「加权和分层各算一版，对比」"
                    "「成本最低优先和最短完成优先都比一下」）时，用 scenarios 把每个方案"
                    "各写一条；每条都要能独立成立，不要写「同上」「第二版」。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "用户这次要改的内容，尽量原文；不要自行补参数。单方案时用它。",
                        },
                        "scenarios": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "多方案时用：每个方案一条，按用户说的顺序。"
                                "例如「加权和分层各算一版」→ [「改成加权，成本最低优先」，"
                                "「改成分层，成本最低优先」]。给了它就不必再给 instruction。"
                            ),
                        },
                        "baseVersion": {
                            **version_arg,
                            "description": "变更基于哪个方案；省略时用当前选中方案。",
                        },
                    },
                },
            },
            {
                "name": "submit_change",
                "description": (
                    "把上一步 solve 生成的变更预览真正派出去求解。\n"
                    "**必须先调过 solve**：没有待确认的变更就调它，会被直接拒绝——"
                    "这是硬规矩，不许凭空发起一次求解。\n"
                    "只在两种情况下调：\n"
                    "  ① 用户在同一句话里既说了改什么、又说了直接算"
                    "（「船租改8万，直接算，不用问我」）；\n"
                    "  ② 用户回复确认（「算吧」「可以」「开始」）。\n"
                    "用户只是在问、在犹豫、在比较、在讨价还价时，不许调。\n"
                    "调完要告诉用户任务号和大概等多久。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "用户原话里授权这次提交的那一句，原样引用，例如「直接算吧」。"
                                "写不出这一句，就说明没被授权——那就不要调。"
                            ),
                        },
                    },
                },
            },
            {
                "name": "ledger",
                "description": (
                    "业务台账，唯一会写盘的动作。四条路："
                    "board 看当前台账（待安排几项、已排程几项、几张检修单）；"
                    "dispatch 把某一版方案下发成待审批检修单和出海清单，任务状态随之变已排程；"
                    "approve 审批一张检修单（批准锁定计划，拒绝放回待安排）；"
                    "feedback 回填现场实际开工/完工，故障消除的任务就此关闭。"
                    "只在用户明确要求下发、审批、报完工时调用，不要自己替用户决定。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["board", "dispatch", "approve", "feedback"],
                            "description": "要执行的动作，默认 board。",
                        },
                        "version": {
                            **version_arg,
                            "description": "action=dispatch 时下发哪一版方案；省略用当前选中方案。",
                        },
                        "orderId": {
                            "type": "string",
                            "description": "action=approve/feedback 的检修单号，例如 WO-1A2B3C4D。省略时用最新一张待审批的单。",
                        },
                        "decision": {
                            "type": "boolean",
                            "description": "action=approve 时 true 批准、false 拒绝。",
                        },
                        "actualStart": {
                            "type": "string",
                            "description": "action=feedback 的实际开工时间，带时区，例如 2026-10-29T08:00:00+08:00。",
                        },
                        "actualFinish": {
                            "type": "string",
                            "description": "action=feedback 的实际完工时间，带时区。",
                        },
                        "consumed": {
                            "type": "integer",
                            "description": "action=feedback 的实际消耗维修包数量，默认 1。",
                        },
                        "returned": {
                            "type": "integer",
                            "description": "action=feedback 的退料数量，默认 0。",
                        },
                        "resolved": {
                            "type": "boolean",
                            "description": "action=feedback 的故障是否消除，默认 true。未消除则任务退回待安排。",
                        },
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "read_versions": self._read_versions,
            "read_environment": self._read_environment,
            "read_catalog": self._read_catalog,
            "read_alarms": self._read_alarms,
            "read_site": self._read_site,
            "read_jobs": self._read_jobs,
            "solve": self._solve,
            "submit_change": self._submit_change,
            "ledger": self._ledger,
        }
        handler = handlers.get(name)
        if not handler:
            return self._envelope(
                "unknown",
                {"error": f"未知工具 {name}"},
                warnings=["工具不存在，未执行任何读取。"],
            )
        spec = self._specs.get(name, {"input_schema": {"type": "object", "properties": {}}})
        cached = False
        retried = False
        latency_ms = 0.0
        try:
            outcome = self.runtime.run(name, arguments or {}, spec, handler)
            result = outcome.data
            cached = outcome.cached
            retried = outcome.retried
            latency_ms = outcome.latency_ms
        except ToolRuntimeError as exc:
            result = self._envelope(
                exc.status,
                {"message": exc.message},
                warnings=[exc.message],
            )
        self._trace.append({
            "tool": name,
            "arguments": arguments,
            "status": result.get("status") if isinstance(result, dict) else None,
            "latencyMs": round(latency_ms, 1),
            "cached": cached,
            "retried": retried,
        })
        if self.on_tool_trace:
            self.on_tool_trace(self._trace[-1])
        if self.on_tool:
            self.on_tool(name, result)
        return result

    def trace(self) -> list[dict[str, Any]]:
        """返回本工具箱本次会话累计的工具调用轨迹（只读副本）。"""
        return list(self._trace)

    # ------------------------------------------------------------- resolution
    def resolve_version(self, reference: str | None) -> WindowVersion | None:
        if not self.ordered:
            return None
        if not reference:
            if self.selected_version_id:
                selected = next(
                    (item for item in self.ordered if item.id == self.selected_version_id),
                    None,
                )
                if selected:
                    return selected
            return self.ordered[-1]

        text = "".join(str(reference).split())
        id_match = re.search(r"ver_[a-f0-9]{8}", text, flags=re.IGNORECASE)
        if id_match:
            target = id_match.group(0).lower()
            return next((item for item in self.ordered if item.id.lower() == target), None)

        if any(token in text for token in ("最新", "最后一版", "最后一个")):
            return self.ordered[-1]
        if any(token in text for token in ("上一版", "前一个")):
            if not self.selected_version_id:
                return None
            index = next(
                (
                    position
                    for position, item in enumerate(self.ordered)
                    if item.id == self.selected_version_id
                ),
                None,
            )
            return self.ordered[index - 1] if index not in (None, 0) else None

        ordinal_match = re.search(r"(?:方案|版本|第)?\s*0*(\d{1,3})\s*(?:个|版)?", text)
        if ordinal_match:
            ordinal = int(ordinal_match.group(1))
            if 1 <= ordinal <= len(self.ordered):
                return self.ordered[ordinal - 1]
        return None

    # ---------------------------------------------------------------- helpers
    def _envelope(
        self,
        status: str,
        data: dict[str, Any],
        *,
        version: WindowVersion | None = None,
        warnings: list[str] | None = None,
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        data_snapshot = _dict((version.resultSummary or {}).get("data")) if version else {}
        return {
            "status": status,
            "data": data,
            "provenance": {
                "conversationId": self.conversation_id,
                "versionId": version.id if version else None,
                "modelFingerprint": version.modelFingerprint if version else None,
                "dataAsOf": (
                    data_snapshot.get("规划时点")
                    or data_snapshot.get("数据时点")
                    or data_snapshot.get("asOf")
                ),
                "generatedAt": datetime.now(SHANGHAI_TZ).isoformat(),
            },
            "warnings": warnings or [],
            "nextActions": next_actions or [],
        }

    def _missing(self, message: str, status: str = "not_found") -> dict[str, Any]:
        return self._envelope(
            status,
            {"message": message},
            warnings=[message],
            next_actions=["确认方案序号或先完成一次求解。"],
        )

    # ---------------------------------------------------------- read_versions
    def _facts(self, version: WindowVersion) -> dict[str, Any]:
        summary = version.resultSummary or {}
        plan_number = self.plan_numbers.get(version.id)
        return {
            "planNumber": plan_number,
            "label": f"方案{plan_number:02d}" if plan_number else None,
            "versionId": version.id,
            "isSelected": version.id == self.selected_version_id,
            "generatedAt": version.generatedAt,
            "prompt": version.userPrompt,
            "strategy": summary.get("strategy"),
            "objectiveMode": summary.get("objectiveMode"),
            "status": _status(version),
            "verified": summary.get("verified"),
            "window": _window(version),
            "totalCostWan": _money(summary.get("totalCost")),
            "cashCostWan": _money(summary.get("cashCost")),
            "generationLossWan": _money(summary.get("generationLoss")),
            "scheduled": summary.get("scheduledTaskCount"),
            "totalTasks": summary.get("totalTaskCount"),
            "vesselDays": summary.get("vesselDays"),
            "latestCompletion": summary.get("latestCompletion"),
            "baselineSavings": summary.get("baselineSavings"),
            "modelFingerprint": version.modelFingerprint,
            "dataAsOf": (
                _dict(summary.get("data")).get("规划时点")
                or _dict(summary.get("data")).get("数据时点")
            ),
        }

    def _read_versions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_versions = arguments.get("versions")
        if isinstance(raw_versions, str):
            raw_versions = [raw_versions]
        references = [
            str(item).strip()
            for item in _list(raw_versions)
            if str(item).strip()
        ]
        include = {
            str(item)
            for item in _list(arguments.get("include"))
            if str(item).strip()
        }
        section = str(arguments.get("section") or "all")
        focus = arguments.get("focus")

        if not references:
            if include:
                current = self.resolve_version(None)
                if not current:
                    return self._missing("本会话还没有可读取的方案。")
                return self._read_single(current, include, section, focus, [])
            listed = self._list_versions({})
            listed["data"]["mode"] = "index"
            return listed

        resolved: list[WindowVersion] = []
        unresolved: list[str] = []
        for reference in references:
            version = self.resolve_version(reference)
            if not version:
                unresolved.append(reference)
            elif version.id not in {item.id for item in resolved}:
                resolved.append(version)

        if not resolved:
            result = self._missing(
                "没有找到你提到的方案。请给出方案序号，例如“方案01”。"
            )
            result["data"]["unresolved"] = unresolved
            return result

        if len(resolved) == 1:
            return self._read_single(
                resolved[0], include, section, focus, unresolved
            )
        baseline = self.resolve_version(arguments.get("baseline"))
        if baseline is not None and baseline.id not in {item.id for item in resolved}:
            resolved.append(baseline)
        return self._read_many(resolved, include, focus, unresolved, baseline)

    # ------------------------------------------------- scope: environment
    def _selected_snapshot(self) -> tuple[str, dict[str, Any]]:
        version = self.resolve_version(None)
        snapshot = dict(version.parameterSnapshot or {}) if version else {}
        base = str(snapshot.get("base") or "基座01")
        overrides = snapshot.get("overrides")
        return base, dict(overrides) if isinstance(overrides, dict) else {}

    def _read_environment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.core import environment_data

        base, overrides = self._selected_snapshot()
        window_ref = str(arguments.get("window") or "").strip()
        detail = str(arguments.get("detail") or "daily")
        if window_ref:
            detail = "window"
        try:
            days = int(arguments.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        if detail == "chart":
            days = 30
        else:
            days = max(1, min(days, 7 if detail == "hourly" else 31))
        try:
            summary = environment_data.environment_summary(
                base=base,
                overrides=overrides,
                days=days,
                hourly=detail in {"hourly", "chart"},
                window=window_ref or ("target" if detail == "window" else None),
            )
        except Exception as exc:  # pragma: no cover - 数据文件缺失/损坏
            return self._envelope(
                "not_available",
                {"message": f"环境数据读取失败：{exc}"},
                warnings=["没有取到风况或电价序列，不能据此推断。"],
            )
        if detail == "window":
            chart_spec = None
            markdown = environment_data.render_window_markdown(summary)
        elif detail == "chart":
            try:
                default_range = int(arguments.get("defaultRange") or 7)
            except (TypeError, ValueError):
                default_range = 7
            chart_spec = environment_data.build_chart_spec(
                summary,
                default_range=default_range,
            )
            markdown = environment_data.render_chart_markdown(summary, chart_spec)
        else:
            chart_spec = None
            markdown = environment_data.render_markdown(summary)
        data = {"markdown": markdown, "verbatim": True}
        data.update(summary)
        if chart_spec is not None:
            data["chartSpec"] = chart_spec
        result = self._envelope("ok", data)
        result["provenance"]["dataAsOf"] = summary.get("asOf")
        result["provenance"]["inputHash"] = summary.get("inputHash")
        return result

    def _read_catalog(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        # 小一修的「目录」是槽位注册表（38 个旋钮），不是大一修的目标模板。
        from yxcore import slots as S
        from yxcore.prompt import _slot_lines, _disambiguation, _boundary_text

        head = (
            "strategy 四选一：" + "、".join(S.STRATEGY_NAMES) + "\n"
            "weight 的 14 个量：" + "、".join(S.METRIC_NAMES)
        )
        markdown = "\n\n".join([head, _slot_lines(), _disambiguation(), _boundary_text()])
        return self._envelope(
            "ok",
            {
                "strategies": list(S.STRATEGY_NAMES),
                "weightedMetrics": list(S.METRIC_NAMES),
                "markdown": markdown,
                "verbatim": True,
            },
        )

    # ------------------------------------------------------ alarms and site data
    def _site_snapshot(self):
        from app.core import site_data

        base, overrides = self._selected_snapshot()
        return site_data.load_site_snapshot(base, overrides)

    @staticmethod
    def _severity(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if "高" in text or "严重" in text:
            return "高"
        if "中" in text:
            return "中"
        if "低" in text:
            return "低"
        return text

    def _read_alarms(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.core import site_data

        try:
            snapshot = self._site_snapshot()
            data = site_data.alarm_report(
                snapshot,
                scope=str(arguments.get("scope") or "backlog"),
                days=int(arguments.get("days") or 7),
                severity=self._severity(arguments.get("severity")),
                status=(
                    str(arguments["status"]).strip()
                    if arguments.get("status")
                    else None
                ),
                turbine_id=(
                    str(arguments["turbineId"]).strip()
                    if arguments.get("turbineId")
                    else None
                ),
                component=(
                    str(arguments["component"]).strip()
                    if arguments.get("component")
                    else None
                ),
                task_id=(
                    str(arguments["taskId"]).strip()
                    if arguments.get("taskId")
                    else None
                ),
                limit=int(arguments.get("limit") or 40),
                include_closed=bool(arguments.get("includeClosed")),
                date=(
                    str(arguments["date"]).strip()
                    if arguments.get("date")
                    else None
                ),
            )
        except Exception as exc:  # pragma: no cover - data file missing/corrupt
            return self._envelope(
                "not_available",
                {"message": f"告警数据读取失败：{exc}"},
                warnings=["没有取到告警清单，不能据此推断当前缺陷情况。"],
            )
        result = self._envelope("ok", data)
        result["provenance"]["dataAsOf"] = data.get("asOf")
        result["provenance"]["inputHash"] = data.get("inputHash")
        return result

    def _read_site(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.core import site_data

        aliases = {
            "台账": "fleet",
            "风机": "fleet",
            "部件": "components",
            "部件库": "components",
            "窗口": "windows",
            "海事窗口": "windows",
            "库存": "inventory",
            "备件": "inventory",
            "检修单": "work_orders",
            "工单": "work_orders",
            "执行": "execution",
            "参数": "parameters",
            "系统参数": "parameters",
        }
        scope = str(arguments.get("scope") or "overview").strip()
        scope = aliases.get(scope, scope)
        if scope not in {
            "overview",
            "fleet",
            "components",
            "windows",
            "inventory",
            "work_orders",
            "execution",
            "parameters",
        }:
            scope = "overview"
        try:
            snapshot = self._site_snapshot()
            data = site_data.site_report(
                snapshot,
                scope=scope,
                turbine_id=(
                    str(arguments["turbineId"]).strip()
                    if arguments.get("turbineId")
                    else None
                ),
                limit=int(arguments.get("limit") or 20),
                query=(
                    str(arguments["query"]).strip()
                    if arguments.get("query")
                    else None
                ),
            )
        except Exception as exc:  # pragma: no cover - data file missing/corrupt
            return self._envelope(
                "not_available",
                {"message": f"现场数据读取失败：{exc}"},
                warnings=["没有取到现场基础数据，不能据此推断设备或资源状态。"],
            )
        result = self._envelope("ok", data)
        result["provenance"]["dataAsOf"] = data.get("asOf")
        result["provenance"]["inputHash"] = data.get("inputHash")
        return result

    # ------------------------------------------------------------- read_jobs
    def _read_jobs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """看本对话派出去的求解活。

        以前模型手里只有 solve（生成预览），预览之后发生了什么它一概看不见。
        用户问「算完了吗」，它只能让人自己去方案页面看——这不是它偷懒，
        是这个能力从来没交给它。
        """
        if not self.jobs:
            return self._envelope(
                "unavailable",
                {"message": "这套工具箱没有接入求解任务仓库，查不到求解进展。"},
                warnings=["jobs 未注入，read_jobs 不可用。"],
            )
        job_id = str(arguments.get("jobId") or "").strip()
        if job_id:
            job = self.jobs.get(job_id)
            if not job:
                return self._envelope(
                    "not_found",
                    {"message": f"没有找到求解任务 {job_id}。", "jobId": job_id},
                    warnings=["任务号可能记错了。"],
                    next_actions=["不传 jobId 再查一次，看最近派出去的是哪几个。"],
                )
            if job.conversationId != self.conversation_id:
                # 别的对话的活不在这儿露，免得串场答错。
                return self._envelope(
                    "not_found",
                    {"message": f"任务 {job_id} 不属于当前对话。"},
                )
            return self._envelope("ok", {"mode": "one", "count": 1, "jobs": [_job_facts(job)]})

        try:
            limit = int(arguments.get("limit") or 3)
        except (TypeError, ValueError):
            limit = 3
        limit = max(1, min(limit, 20))
        jobs = self.jobs.list_by_conversation(self.conversation_id)[:limit]
        if not jobs:
            return self._envelope(
                "ok",
                {"mode": "index", "count": 0, "jobs": []},
                next_actions=["本对话还没派出过求解；要改排程先调 solve 生成变更预览。"],
            )
        return self._envelope(
            "ok",
            {"mode": "index", "count": len(jobs), "jobs": [_job_facts(job) for job in jobs]},
        )

    def _read_single(
        self,
        version: WindowVersion,
        include: set[str],
        section: str,
        focus: Any,
        unresolved: list[str],
    ) -> dict[str, Any]:
        detail = "summary"
        if "tasks" in include:
            detail = "tasks"
        elif "unscheduled" in include:
            detail = "unscheduled"
        result = self._get_version({"version": version.id, "detail": detail})
        data = result["data"]
        data["mode"] = "single"
        if focus:
            data["focus"] = str(focus)
        if "status" in include:
            data["solve"] = self._solve_facts(version)

        want_costs = "costs" in include or (section == "costs" and "model" in include)
        if want_costs:
            costs = self._get_model({"version": version.id, "view": "costs"})
            if costs["status"] == "ok":
                data["costs"] = costs["data"]
                if "model" not in include:
                    data["markdown"] = costs["data"]["markdown"]
                    data["verbatim"] = True
            else:
                result["warnings"].extend(costs.get("warnings") or [])
        if "model" in include and section != "costs":
            model = self._get_model(
                {"version": version.id, "view": "math", "section": section}
            )
            if model["status"] == "ok":
                data["markdown"] = model["data"]["markdown"]
                data["latex"] = model["data"]["latex"]
                data["modelFingerprint"] = model["data"]["modelFingerprint"]
                data["dimensions"] = model["data"]["dimensions"]
            else:
                data["modelUnavailable"] = model["data"].get("message")
                result["warnings"].extend(model.get("warnings") or [])
        if "parameters" in include:
            params = self._get_model({"version": version.id, "view": "parameters"})
            if params["status"] == "ok":
                data["parameters"] = params["data"]["parameters"]
                data["parameterCount"] = params["data"]["count"]
            else:
                result["warnings"].extend(params.get("warnings") or [])
        if "constraints" in include:
            constraints = self._get_model({"version": version.id, "view": "constraints"})
            if constraints["status"] == "ok":
                data["constraints"] = constraints["data"]
            else:
                result["warnings"].extend(constraints.get("warnings") or [])

        if unresolved:
            result["warnings"].append(
                "以下引用没有找到，已忽略：" + "、".join(unresolved)
            )
            data["unresolved"] = unresolved
        return result

    def _model_projection(self, version: WindowVersion) -> dict[str, Any]:
        """单版模型的结构化投影（目标层级 / 约束分组 / 变量规模），用于 N 版对照。"""
        artifact = self.model_artifacts.get(version.id) if self.model_artifacts else None
        if not artifact:
            return {
                "available": False,
                "modelFingerprint": getattr(version, "modelFingerprint", None),
            }
        stages = _list(_dict(artifact.get("objectives")).get("stages"))
        groups = _list(_dict(artifact.get("constraints")).get("groups"))
        variables = _dict(artifact.get("variables"))
        return {
            "available": True,
            "modelFingerprint": getattr(version, "modelFingerprint", None),
            "objective": [
                {
                    "priority": item.get("priority"),
                    "name": item.get("name"),
                    "sense": item.get("sense"),
                    "terms": item.get("expressionTermCount"),
                }
                for item in stages[:10]
            ],
            "constraints": [
                {
                    "kind": item.get("kind") or item.get("id"),
                    "count": item.get("count"),
                    "meaning": item.get("meaning"),
                }
                for item in groups[:12]
            ],
            "variables": {
                "total": variables.get("total"),
                "groupCount": len(_list(variables.get("groups"))),
            },
        }

    @staticmethod
    def _model_diffs(projections: list[dict[str, Any]], baseline_index: int) -> list[dict[str, Any]]:
        """模型层差异：目标层级、约束分组、模型指纹。差异在工具侧算好，模型只解读。"""
        if len(projections) < 2:
            return []
        diffs: list[dict[str, Any]] = []

        fingerprints = [item.get("modelFingerprint") for item in projections]
        if len({fp for fp in fingerprints if fp}) > 1:
            diffs.append({
                "dimension": "model.fingerprint",
                "key": "modelFingerprint",
                "baselineValue": fingerprints[baseline_index],
                "values": fingerprints,
                "changed": True,
            })

        max_stages = max((len(item.get("objective") or []) for item in projections), default=0)
        for index in range(max_stages):
            values = []
            for item in projections:
                stages = item.get("objective") or []
                stage = stages[index] if index < len(stages) else None
                values.append(
                    None if stage is None else
                    {"name": stage.get("name"), "sense": stage.get("sense"), "terms": stage.get("terms")}
                )
            signatures = {
                ((value or {}).get("name"), (value or {}).get("sense"), (value or {}).get("terms"))
                for value in values if value is not None
            }
            if len(signatures) > 1:
                diffs.append({
                    "dimension": "model.objective",
                    "key": f"priority{index + 1}",
                    "baselineValue": values[baseline_index],
                    "values": values,
                    "changed": True,
                })

        kinds = sorted({item.get("kind") for proj in projections for item in (proj.get("constraints") or []) if item.get("kind")})
        for kind in kinds:
            counts = []
            for proj in projections:
                found = next((c for c in (proj.get("constraints") or []) if c.get("kind") == kind), None)
                counts.append(found.get("count") if found else 0)
            if len(set(counts)) > 1:
                diffs.append({
                    "dimension": "model.constraints",
                    "key": kind,
                    "baselineValue": counts[baseline_index],
                    "values": counts,
                    "changed": True,
                })
        return diffs

    def _read_many(
        self,
        versions: list[WindowVersion],
        include: set[str],
        focus: Any,
        unresolved: list[str],
        baseline: WindowVersion | None = None,
    ) -> dict[str, Any]:
        facts = [self._facts(version) for version in versions]
        warnings: list[str] = []
        for fact, version in zip(facts, versions):
            if "model" in include:
                fact["model"] = self._model_projection(version)
            if "tasks" in include:
                fact["tasks"] = [
                    {
                        "taskId": item.taskId,
                        "componentName": item.componentName,
                        "startTime": item.startTime,
                        "endTime": item.endTime,
                    }
                    for item in (version.repairSchedule or [])[:8]
                ]
                fact["taskCount"] = len(version.repairSchedule or [])
            if "unscheduled" in include:
                unscheduled = _list((version.resultSummary or {}).get("unscheduledTasks"))
                fact["unscheduled"] = unscheduled[:8]
                fact["unscheduledCount"] = len(unscheduled)
            if "status" in include:
                fact["solve"] = self._solve_facts(version)

        matrix = self._metric_matrix(facts)
        changed = [
            row for row in matrix if row["changed"]
        ]
        changed.sort(
            key=lambda row: (
                0 if row["always"] else 1,
                -(row["spreadPct"] if row["spreadPct"] is not None else -1),
            )
        )
        identical = [row["key"] for row in matrix if not row["changed"]]
        comparability = self._comparability(versions, facts)
        baseline_index = next(
            (
                index
                for index, version in enumerate(versions)
                if baseline is not None and version.id == baseline.id
            ),
            0,
        )
        deltas = self._deltas_vs_baseline(matrix, facts, baseline_index)
        presence = self._task_presence(versions, facts)
        model_diffs = (
            self._model_diffs([fact.get("model") or {} for fact in facts], baseline_index)
            if "model" in include
            else []
        )
        if unresolved:
            warnings.append("以下引用没有找到，已忽略：" + "、".join(unresolved))
        return self._envelope(
            "ok",
            {
                "mode": "compare",
                "focus": str(focus) if focus else None,
                "plans": facts,
                "matrix": {
                    "columns": [
                        {
                            "planNumber": fact["planNumber"],
                            "label": fact["label"],
                            "versionId": fact["versionId"],
                        }
                        for fact in facts
                    ],
                    "rows": matrix,
                },
                "differences": {
                    "changed": [
                        {
                            "key": row["key"],
                            "label": row["label"],
                            "unit": row["unit"],
                            "values": row["values"],
                            "bestIndex": row["bestIndex"],
                            "spreadPct": row["spreadPct"],
                            "alwaysWatch": row["always"],
                        }
                        for row in changed[:8]
                    ],
                    "identical": identical,
                },
                "comparability": comparability,
                "deltasVsBaseline": deltas,
                "modelDiffs": model_diffs,
                "taskPresence": presence,
                "unresolved": unresolved,
            },
            version=versions[-1],
            warnings=warnings,
        )

    @staticmethod
    def _deltas_vs_baseline(
        matrix: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        baseline_index: int,
    ) -> dict[str, Any]:
        rows = []
        for row in matrix:
            values = row["values"]
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            ):
                continue
            base_value = values[baseline_index]
            plans = []
            for index, fact in enumerate(facts):
                if index == baseline_index:
                    continue
                delta = round(float(values[index]) - float(base_value), 4)
                plans.append(
                    {
                        "label": fact["label"],
                        "value": values[index],
                        "delta": delta,
                        "deltaPct": (
                            round(delta / abs(base_value) * 100, 2)
                            if base_value
                            else None
                        ),
                    }
                )
            rows.append(
                {
                    "key": row["key"],
                    "label": row["label"],
                    "unit": row["unit"],
                    "better": row["better"],
                    "baseline": base_value,
                    "plans": plans,
                }
            )
        return {
            "baselineLabel": facts[baseline_index]["label"],
            "baselineVersionId": facts[baseline_index]["versionId"],
            "rows": rows,
            "note": "差值一律相对基准方案计算；未列出的指标为文本或缺失，不做减法。",
        }

    @staticmethod
    def _task_presence(
        versions: list[WindowVersion],
        facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sets = [set(item.taskId for item in (version.repairSchedule or [])) for version in versions]
        everything = set().union(*sets) if sets else set()
        common = set.intersection(*sets) if sets else set()
        only_in: dict[str, list[str]] = {}
        for index, fact in enumerate(facts):
            others = set().union(
                *[item for pos, item in enumerate(sets) if pos != index]
            ) if len(sets) > 1 else set()
            unique = sorted(sets[index] - others)
            if unique:
                only_in[fact["label"]] = unique[:12]
        starts: dict[str, dict[int, str]] = {}
        for index, version in enumerate(versions):
            for item in version.repairSchedule or []:
                starts.setdefault(item.taskId, {})[index] = item.startTime
        moved = []
        for task_id, per_plan in sorted(starts.items()):
            if len(per_plan) < 2:
                continue
            if len({value for value in per_plan.values()}) > 1:
                moved.append(
                    {
                        "taskId": task_id,
                        "starts": {
                            facts[index]["label"]: value
                            for index, value in sorted(per_plan.items())
                        },
                    }
                )
        return {
            "commonCount": len(common),
            "common": sorted(common)[:20],
            "onlyIn": only_in,
            "timeChanged": moved[:12],
            "timeChangedCount": len(moved),
            "universeNote": (
                "各方案任务全集一致"
                if len(sets) == len(facts)
                and len({frozenset(item) for item in sets}) == 1
                else "各方案完成的任务集合不同，不能宣称同等服务下的节省"
            ),
            "coveredTasks": len(everything),
        }

    @staticmethod
    def _metric_matrix(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def build(
            key: str,
            label: str,
            values: list[Any],
            *,
            unit: str | None = None,
            better: str | None = None,
            always: bool = False,
        ) -> dict[str, Any]:
            numeric = all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in values
            )
            changed = len({str(value) for value in values}) > 1
            best_index = None
            spread_pct = None
            if numeric:
                low, high = min(values), max(values)
                if better == "lower":
                    best_index = values.index(low)
                elif better == "higher":
                    best_index = values.index(high)
                if low:
                    spread_pct = round((high - low) / abs(low) * 100, 2)
                else:
                    spread_pct = 0.0 if high == 0 else None
            return {
                "key": key,
                "label": label,
                "unit": unit,
                "better": better,
                "always": always,
                "values": values,
                "changed": changed,
                "bestIndex": best_index if changed else None,
                "spreadPct": spread_pct if changed else 0.0,
            }

        windows = [
            (
                f"{fact['window'].get('start')} 至 {fact['window'].get('end')}"
                if fact["window"].get("start")
                else "未记录"
            )
            for fact in facts
        ]
        return [
            build("window", "建议窗口", windows),
            build(
                "totalCost",
                "总成本",
                [fact["totalCostWan"] for fact in facts],
                unit="万元",
                better="lower",
                always=True,
            ),
            build(
                "cashCost",
                "现金支出",
                [fact["cashCostWan"] for fact in facts],
                unit="万元",
                better="lower",
            ),
            build(
                "generationLoss",
                "停机损失",
                [fact["generationLossWan"] for fact in facts],
                unit="万元",
                better="lower",
            ),
            build(
                "scheduled",
                "完成任务",
                [fact["scheduled"] for fact in facts],
                unit="项",
                better="higher",
                always=True,
            ),
            build(
                "totalTasks",
                "待修总数",
                [fact["totalTasks"] for fact in facts],
                unit="项",
            ),
            build(
                "vesselDays",
                "船舶艘天",
                [fact["vesselDays"] for fact in facts],
                unit="艘天",
                better="lower",
                always=True,
            ),
            build(
                "latestCompletion",
                "最晚完工",
                [fact["latestCompletion"] for fact in facts],
            ),
            build(
                "status",
                "求解状态",
                [fact["status"] for fact in facts],
                always=True,
            ),
            build(
                "verified",
                "独立复核",
                [fact["verified"] for fact in facts],
                always=True,
            ),
            build(
                "strategy",
                "优化策略",
                [fact["strategy"] for fact in facts],
            ),
            build(
                "objectiveMode",
                "目标模式",
                [fact["objectiveMode"] for fact in facts],
            ),
        ]

    def _comparability(
        self,
        versions: list[WindowVersion],
        facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshots = [
            (
                _dict((item.resultSummary or {}).get("data")).get("输入哈希"),
                fact.get("dataAsOf"),
            )
            for item, fact in zip(versions, facts)
        ]
        hashes = [item[0] for item in snapshots]
        stamps = [item[1] for item in snapshots]
        if all(hashes):
            same_data = len(set(hashes)) == 1
        elif all(stamps):
            same_data = len(set(stamps)) == 1
        else:
            same_data = False
        same_universe = len({fact["totalTasks"] for fact in facts}) == 1
        fingerprints = {fact["modelFingerprint"] for fact in facts if fact["modelFingerprint"]}
        same_model = len(fingerprints) == 1 if fingerprints else None
        task_sets = [tuple(sorted(item.taskId for item in (version.repairSchedule or []))) for version in versions]
        same_tasks = len(set(task_sets)) == 1
        cost_comparable = same_data and same_universe
        notes: list[str] = []
        if not same_data:
            notes.append("几次运行的数据快照不同，成本差异不能全部归因于参数或目标。")
        if not same_universe:
            notes.append("各次运行的待修任务总数不同，完成量不能直接横比。")
        if not same_tasks:
            notes.append("各次完成的任务集合不同，不能宣称同等服务下的节省。")
        if same_model:
            notes.append("这些方案的模型指纹相同，结论应一致；差异更可能来自求解时间或随机性。")
        gaps = [_dict(fact.get("convergence")).get("costGap") for fact in facts]
        if any(gap is None for gap in gaps):
            notes.append("至少一版没有收敛记录，成本差值未核实，不能当结论。")
        elif gaps and max(gaps) > 0:
            notes.append(
                f"有方案未证明最优（最大成本相对差距 {max(gaps):.2%}）；"
                "成本差值小于该容差时，不能判定谁更省。"
            )
        return {
            "sameDataSnapshot": same_data,
            "sameTaskUniverse": same_universe,
            "sameModelFingerprint": same_model,
            "sameScheduledTasks": same_tasks,
            "costComparable": cost_comparable,
            "savingsClaimAllowed": cost_comparable and same_tasks,
            "notes": notes,
        }

    @staticmethod
    def _solve_facts(version: WindowVersion) -> dict[str, Any]:
        solver = _solver(version)
        summary = version.resultSummary or {}
        from app.core.version_compare import convergence_of

        return {
            "status": _status(version),
            "verified": summary.get("verified"),
            "provenOptimal": _status(version) == "OPTIMAL",
            "elapsedSeconds": solver.get("总耗时秒") or solver.get("总耗时"),
            "stopReason": solver.get("终止原因"),
            "solverParameters": solver.get("求解器参数"),
            # 收敛到哪一步、差多少，是"能不能说谁更省"的前提；
            # 不放进 facts，模型只能看到两个成本数字，然后自己编结论。
            "convergence": convergence_of(version),
            "modelFingerprint": version.modelFingerprint,
        }

    # ------------------------------------------------------------------ solve
    def _solve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """solve 的所有出口都必须由系统给出用户可见文字。

        只给成功路径配 markdown 是不够的：DSL 没解析出来时返回 ``clarify``，
        data 里只有给模型看的 message。此时模型可以自由发挥成"已排除了 9-13"，
        用户看到的就是一句谁也没打算说的话——真正的契约（这句没听懂）根本没
        送到用户眼前。所以这里对**每一个**出口补 markdown，并且它属于
        AUTHORITATIVE_TOOLS，独占本轮输出。
        """
        return self._as_contract(self._solve_inner_cleared(arguments))

    def _solve_inner_cleared(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """solve 的统一出口：这一轮没产出新预览，就把上一轮那份待确认清掉。

        不清的后果很具体：上一轮留着一份“分层→加权”，这一轮用户说“改成分层”
        没被识别，屏幕上没有新预览，但内存里那份还在；接着一句“确认”派出去的
        就是上一轮那份——用户确认的和他以为的不是同一件事。
        """
        self.pending_touched = True
        result = self._solve_inner(arguments)
        if result.get("status") != "needs_confirmation":
            self.pending_change = None
        return result

    @staticmethod
    def _as_contract(result: dict[str, Any]) -> dict[str, Any]:
        data = result.get("data")
        if isinstance(data, dict) and not data.get("markdown"):
            message = str(data.get("message") or "").strip()
            if message:
                data["markdown"] = message
                data["verbatim"] = True
        return result

    def _solve_inner(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """solve 工具：把「改排程」翻译成槽位补丁，出预览，等确认（小一修理解层）。"""
        instruction = str(arguments.get("instruction") or "").strip()
        if not instruction:
            return self._envelope(
                "clarify",
                {"message": "还需要一句明确的排程变更，例如「船租涨两成」「最多修 8 个」。",
                 "requiresUserConfirmation": False},
            )
        if self.yx_engine is None:
            return self._envelope(
                "unavailable",
                {"message": "槽位引擎未接入，无法准备变更。"},
                warnings=["槽位引擎未注入。"],
            )
        try:
            turn = self.yx_engine.say(instruction, timeout=90)
        except Exception as exc:  # noqa: BLE001
            return self._as_contract(self._envelope(
                "clarify", {"message": f"这句没接：{exc}"},
            ))
        echo = turn.echo()
        if turn.错误 or turn.拒绝 or not (turn.新增 or turn.修订 or turn.撤销):
            text = "\n".join(f"- {line}" for line in echo)
            return self._as_contract(self._envelope(
                "clarify",
                {"message": text, "markdown": text, "verbatim": True, "requiresUserConfirmation": False},
            ))
        summary = self.yx_engine.session.summary()
        lines = [f"- {line}" for line in echo]
        if summary:
            lines.append("")
            lines.append("现在按这些在排：")
            lines.extend(f"- {line}" for line in summary)
        preview = "\n".join(lines)
        payload = {
            "kind": "yx",
            "sourcePrompt": instruction,
            "utterance": self.user_message or instruction,
            "preview": preview,
        }
        self.pending_change = payload
        if self.on_pending_change:
            self.on_pending_change(payload)
        return self._envelope(
            "needs_confirmation",
            {"preview": preview, "markdown": preview, "verbatim": True, "requiresUserConfirmation": True},
            next_actions=["等待用户确认；确认前不得声称已求解或已生成方案。"],
        )

    def _submit_change(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """把待确认的变更真正派出去。

        门槛不是参数，是**状态**：必须先有 pending_change，也就是这一轮
        已经调过 solve、生成过预览。没有就拒绝。

        为什么卡这条：允许模型凭一句「直接算」凭空发起求解，等于绕开
        「先看预览再确认」这条线。真排程算一次三分钟、占算力、还会生成
        新方案并覆盖当前选中版本——这种动作不能是模型自己想起来就做的。
        """
        if not self.jobs or not self.on_submit_change:
            return self._as_contract(self._envelope(
                "unavailable",
                {"message": "这套工具箱没有接入求解服务，派不出去；变更预览仍然有效。"},
                warnings=["submit_change 不可用。"],
                next_actions=["请用户在界面上确认这份变更，由系统发起求解。"],
            ))
        pending = self.pending_change
        if not isinstance(pending, dict) or not pending:
            return self._as_contract(self._envelope(
                "clarify",
                {"message": "还没有待确认的变更。先调 solve 生成预览，再调这个。"},
                warnings=["凭空发起求解被拒绝：必须先有变更预览。"],
                next_actions=["先用 solve 把用户要的改动编译成预览。"],
            ))

        reason = str(arguments.get("reason") or "").strip()
        try:
            outcome = self.on_submit_change(self.conversation_id, pending, reason)
        except Exception as exc:  # noqa: BLE001 - 派活失败不能让整轮对话崩
            return self._as_contract(self._envelope(
                "error",
                {"message": f"派活失败：{type(exc).__name__}: {exc}"},
                warnings=["求解没有启动，当前方案没有变化。"],
            ))
        if not outcome.get("ok"):
            return self._as_contract(self._envelope(
                "error",
                {"message": outcome.get("message") or "派活失败"},
                warnings=["求解没有启动，当前方案没有变化。"],
            ))
        message = outcome.get("message") or "已派出，正在后台求解。"
        return self._envelope(
            "submitted",
            {
                "jobId": outcome.get("jobId"),
                "message": message,
                # "已派出/正在求解"是对用户的承诺，也不许模型改口径。
                "markdown": message,
                "verbatim": True,
            },
            next_actions=["告诉用户任务号和大概要等多久；不要再让用户确认一次。"],
        )

    @staticmethod
    def _ledger_module():
        """台账模块和求解器放在同一目录，按需挂到 sys.path。"""
        import sys

        scripts = PROJECT_ROOT / "maintenance-milp-modeler" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from app.core import product_writeback

        return product_writeback

    def _report_of(self, version: WindowVersion) -> dict[str, Any]:
        """取这次求解落盘的原始报告。

        下发要用完整排程（作业线、部件编号、窗口编号、分钟级时间），
        这些信息在摘要里已经被压掉了，只能读原文，不能凭摘要反推。
        """
        fingerprint = version.modelFingerprint or ""
        stem = f"agent-model-{fingerprint[:16]}"
        folder_root = PROJECT_ROOT / "product-output"
        candidates = [folder_root / stem] + sorted(folder_root.glob(f"{stem}-*"))
        for folder in candidates:
            path = folder / "结果.json"
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        raise ValueError("找不到这一版方案的排程原文，无法下发；请重新求解一次。")

    @staticmethod
    def _stamp_builder(report: dict[str, Any]):
        """把排程里的分钟数还原成带时区的绝对时间。"""
        windows = report.get("窗口") or []
        if not windows:
            raise ValueError("报告里没有窗口信息，无法还原计划时间。")
        window = windows[0]
        base = datetime.fromisoformat(window["开始日期"]) - timedelta(
            minutes=int(window.get("开始") or 0)
        )
        return lambda minute: (base + timedelta(minutes=int(minute))).isoformat()

    def _open_order_id(self) -> str | None:
        ledger = self._ledger_module()
        pending = [
            row for row in ledger.read_rows(
                ledger.DEMO_DIR / "检修单.csv", ledger.ORDER_FIELDS
            )
            if row.get("审批状态") == ledger.ORDER_PENDING
        ]
        return pending[0]["检修单号"] if pending else None

    def _ledger(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ledger = self._ledger_module()
        action = str(arguments.get("action") or "board")

        if action == "board":
            data = ledger.board()
            return self._envelope(
                "ok",
                data,
                next_actions=["要下发时说“把方案02下发”，要报完工时说“WO-xxx 修完了”。"],
            )

        if action == "dispatch":
            version = self.resolve_version(arguments.get("version"))
            if version is None:
                return self._missing("本会话还没有可下发的方案，请先求解一次。")
            report = self._report_of(version)
            schedule = (report.get("推荐") or {}).get("排程") or []
            if not schedule:
                return self._envelope(
                    "empty",
                    {"message": "这一版方案没有排到任何任务，不建空单。"},
                    version=version,
                    warnings=["空方案不生成检修单。"],
                )
            number = self.plan_numbers.get(version.id, 1)
            result = ledger.dispatch(
                None,
                schedule=schedule,
                stamp=self._stamp_builder(report),
                plan_version=f"方案{number:02d}-{version.id[-6:]}",
            )
            return self._envelope(
                "ok",
                result,
                version=version,
                next_actions=["等审批：说“批准”或“拒绝这张单”。"],
            )

        if action == "approve":
            order_id = str(arguments.get("orderId") or "").strip() or self._open_order_id()
            if not order_id:
                return self._missing("当前没有待审批的检修单。")
            result = ledger.approve(
                None,
                order_id=order_id,
                decision=bool(arguments.get("decision", True)),
            )
            return self._envelope("ok", result, next_actions=["出海完成后说“登记完工”。"])

        if action == "feedback":
            order_id = str(arguments.get("orderId") or "").strip()
            if not order_id:
                return self._missing("请给出要回填的检修单号，例如 WO-1A2B3C4D。")
            start = str(arguments.get("actualStart") or "").strip()
            finish = str(arguments.get("actualFinish") or "").strip()
            if not start or not finish:
                return self._missing("实际开工时间和实际完工时间都要给，否则无法销账。")
            result = ledger.record_feedback(
                None,
                order_id=order_id,
                actual_start=start,
                actual_finish=finish,
                consumed=int(arguments.get("consumed") or 1),
                returned=int(arguments.get("returned") or 0),
                resolved=bool(arguments.get("resolved", True)),
            )
            return self._envelope("ok", result)

        return self._envelope(
            "unknown",
            {"error": f"未知台账动作 {action}"},
            warnings=["未执行任何写操作。"],
        )

    def compare_pair(
        self,
        left: WindowVersion,
        right: WindowVersion,
    ) -> dict[str, Any]:
        left_artifact = (
            self.model_artifacts.get(left.id) if self.model_artifacts else None
        )
        right_artifact = (
            self.model_artifacts.get(right.id) if self.model_artifacts else None
        )
        return compare_versions(
            left,
            right,
            left_number=self.plan_numbers.get(left.id, 1),
            right_number=self.plan_numbers.get(right.id, 1),
            left_artifact=left_artifact,
            right_artifact=right_artifact,
        )

    # ---------------------------------------------------------------- handlers
    def _list_versions(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for item in self.ordered:
            summary = item.resultSummary or {}
            rows.append(
                {
                    "planNumber": self.plan_numbers[item.id],
                    "versionId": item.id,
                    "generatedAt": item.generatedAt,
                    "prompt": item.userPrompt,
                    "strategy": summary.get("strategy"),
                    "objectiveMode": summary.get("objectiveMode"),
                    "status": _status(item),
                    "verified": summary.get("verified"),
                    "totalCostWan": _money(summary.get("totalCost")),
                    "cashCostWan": _money(summary.get("cashCost")),
                    "generationLossWan": _money(summary.get("generationLoss")),
                    "scheduled": summary.get("scheduledTaskCount"),
                    "totalTasks": summary.get("totalTaskCount"),
                    "vesselDays": summary.get("vesselDays"),
                    "latestCompletion": summary.get("latestCompletion"),
                    "window": _window(item),
                }
            )
        return self._envelope("ok", {"versions": rows, "count": len(rows)})

    def _get_version(self, arguments: dict[str, Any]) -> dict[str, Any]:
        version = self.resolve_version(arguments.get("version"))
        if not version:
            return self._missing("没有找到对应方案。请给出方案序号，例如“方案01”。")
        detail = str(arguments.get("detail") or "summary")
        summary = version.resultSummary or {}
        data: dict[str, Any] = {
            "planNumber": self.plan_numbers.get(version.id),
            "label": (
                f"方案{self.plan_numbers[version.id]:02d}"
                if version.id in self.plan_numbers
                else None
            ),
            "versionId": version.id,
            "generatedAt": version.generatedAt,
            "prompt": version.userPrompt,
            "strategy": summary.get("strategy"),
            "objectiveMode": summary.get("objectiveMode"),
            "status": _status(version),
            "verified": summary.get("verified"),
            "window": _window(version),
            "totalCostWan": _money(summary.get("totalCost")),
            "cashCostWan": _money(summary.get("cashCost")),
            "generationLossWan": _money(summary.get("generationLoss")),
            "scheduled": summary.get("scheduledTaskCount"),
            "totalTasks": summary.get("totalTaskCount"),
            "vesselDays": summary.get("vesselDays"),
            "latestCompletion": summary.get("latestCompletion"),
            "baselineSavings": summary.get("baselineSavings"),
            "modelFingerprint": version.modelFingerprint,
            "dataAsOf": (
                _dict(summary.get("data")).get("规划时点")
                or _dict(summary.get("data")).get("数据时点")
            ),
        }
        if detail == "tasks":
            data["tasks"] = [
                {
                    "taskId": item.taskId,
                    "turbineId": item.turbineId,
                    "componentName": item.componentName,
                    "startTime": item.startTime,
                    "endTime": item.endTime,
                    "durationHours": item.durationHours,
                }
                for item in version.repairSchedule[:30]
            ]
            data["taskCount"] = len(version.repairSchedule)
        elif detail == "unscheduled":
            unscheduled = _list(summary.get("unscheduledTasks"))
            data["unscheduled"] = unscheduled[:20]
            data["unscheduledCount"] = len(unscheduled)
        return self._envelope("ok", data, version=version)

    def _get_model(self, arguments: dict[str, Any]) -> dict[str, Any]:
        version = self.resolve_version(arguments.get("version"))
        if not version:
            return self._missing("没有找到对应方案。")
        artifact = self.model_artifacts.get(version.id) if self.model_artifacts else None
        if not artifact:
            return self._envelope(
                "not_available",
                {
                    "message": "本次运行没有保存完整模型产物，不能给出变量、约束行或系数。",
                    "versionId": version.id,
                },
                version=version,
                warnings=["缺少 ModelArtifact；不能用参数事后重建并冒充原模型。"],
                next_actions=["按相同入参重新运行一次并保存模型产物。"],
            )
        view = str(arguments.get("view") or "math")
        if view == "costs":
            costs = self._cost_facts(version, artifact)
            return self._envelope("ok", costs, version=version)
        if view == "parameters":
            parameters = _list(artifact.get("parameters"))
            return self._envelope(
                "ok",
                {
                    "versionId": version.id,
                    "parameters": parameters[:24],
                    "count": len(parameters),
                },
                version=version,
            )
        if view == "variables":
            groups = _list(_dict(artifact.get("variables")).get("groups"))
            return self._envelope(
                "ok",
                {
                    "versionId": version.id,
                    "total": _dict(artifact.get("variables")).get("total"),
                    "groups": groups[:12],
                    "count": len(groups),
                },
                version=version,
            )
        if view == "constraints":
            groups = _list(_dict(artifact.get("constraints")).get("groups"))
            samples = _list(_dict(artifact.get("constraints")).get("samples"))
            return self._envelope(
                "ok",
                {
                    "versionId": version.id,
                    "total": _dict(artifact.get("constraints")).get("total"),
                    "groups": groups[:10],
                    "samples": [
                        {
                            "id": item.get("id"),
                            "expression": str(item.get("expression") or "")[:400],
                            "lowerBound": item.get("lowerBound"),
                            "upperBound": item.get("upperBound"),
                        }
                        for item in samples[:5]
                    ],
                },
                version=version,
            )
        return self._envelope(
            "ok",
            {
                "versionId": version.id,
                "markdown": self._render_math_markdown(
                    version,
                    artifact,
                    str(arguments.get("section") or "all"),
                ),
                "latex": self._render_math_latex(version, artifact),
                "modelFingerprint": artifact.get("modelFingerprint"),
                "dimensions": artifact.get("dimensions"),
                "solverParameters": artifact.get("solverParameters"),
            },
            version=version,
        )

    # ------------------------------------------------------------- math render
    def _cost_facts(
        self,
        version: WindowVersion,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        summary = version.resultSummary or {}
        items = _dict(summary.get("costItems"))
        recovered_from_output = False
        if not any(value is not None for value in items.values()):
            recovered = _cost_items_from_output(version)
            if recovered:
                items = recovered
                recovered_from_output = True
        parameters = {
            str(item.get("label") or item.get("id")): item
            for item in _list(artifact.get("parameters"))
        }
        bases = {
            "船租": ("船队艘天 × 船租单价", "船租"),
            "人工": ("人时 × 人工单价", "人工单价"),
            "备件": ("领用数量 × 备件单价", None),
            "其他现场费用": ("每任务固定其他费用", None),
            "发电损失": ("停机小时 × 电价 × 出力（机会成本）", "电价"),
        }
        rows = []
        for name, (basis, parameter_label) in bases.items():
            amount = items.get(name)
            parameter = parameters.get(parameter_label) if parameter_label else None
            rows.append(
                {
                    "name": name,
                    "amountWan": _money(amount),
                    "basis": basis,
                    "source": (parameter or {}).get("source"),
                    "asOf": (parameter or {}).get("asOf"),
                    "known": amount is not None,
                }
            )
        total = _money(summary.get("totalCost"))
        cash = _money(summary.get("cashCost"))
        loss = _money(summary.get("generationLoss"))
        missing = [row["name"] for row in rows if not row["known"]]
        lines = [
            f"**方案 {self.plan_numbers.get(version.id, 1):02d} 的成本构成**"
            f"（总经济成本 {total if total is not None else '待确认'} 万元）",
            "",
            "| 成本项 | 金额（万元） | 计算口径 |",
            "| --- | --- | --- |",
        ]
        for row in rows:
            amount_text = row["amountWan"] if row["amountWan"] is not None else "未记录"
            lines.append(f"| {row['name']} | {amount_text} | {row['basis']} |")
        lines.append("")
        lines.append(
            f"现金支出 {cash if cash is not None else '待确认'} 万元 = 船租 + 人工 + 备件 + 其他现场费用；"
            f"发电损失 {loss if loss is not None else '待确认'} 万元是机会成本，计入总经济成本但不计入现金支出。"
        )
        if missing:
            lines.append(
                "以下分项没有随该次求解一起保存："
                + "、".join(missing)
                + "；总额与现金支出仍来自同一次运行，不能据此拆分。"
            )
        elif recovered_from_output:
            lines.append("分项金额取自该次求解的输出快照，与总额同源。")
        return {
            "versionId": version.id,
            "planNumber": self.plan_numbers.get(version.id),
            "items": rows,
            "totalCostWan": total,
            "cashCostWan": cash,
            "generationLossWan": loss,
            "missingItems": missing,
            "recoveredFromOutput": recovered_from_output,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    @staticmethod
    def _stage_symbols(artifact: dict[str, Any]) -> list[tuple[str, str]]:
        names = [
            str(item.get("name") or "")
            for item in _list(_dict(artifact.get("objectives")).get("stages"))
        ]
        mapping = {
            "总经济成本": "F_1",
            "完成任务数": "F_2",
            "最后完工时间": "F_3",
            "完工时间和": "F_4",
        }
        used: list[tuple[str, str]] = []
        counter = 5
        for name in names:
            symbol = mapping.get(name)
            if not symbol:
                symbol = f"F_{counter}"
                counter += 1
            used.append((name, symbol))
        return used

    def _render_math_latex(
        self,
        version: WindowVersion,
        artifact: dict[str, Any],
    ) -> dict[str, str]:
        mode = str(artifact.get("mode") or "分层")
        stages = _list(_dict(artifact.get("objectives")).get("stages"))
        symbols = self._stage_symbols(artifact)
        terms = {
            "F_1": (
                r"\sum_{i\in I}\sum_{k\in K_i}\sum_{l\in L} c_{i,k,l}\,x_{i,k,l}"
                r"+\sum_{l\in L}\sum_{d\in D} r_l\,y_{l,d}+C_0"
            ),
            "F_2": r"\sum_{i\in I}\sum_{k\in K_i}\sum_{l\in L} x_{i,k,l}",
            "F_3": r"T",
            "F_4": r"\sum_{i\in I}\sum_{k\in K_i}\sum_{l\in L} t_{i,k}\,x_{i,k,l}",
        }
        objective_lines: list[str] = []
        if mode == "加权":
            weighted = " + ".join(
                rf"w_{{{index+1}}}\,{symbol}"
                for index, (_name, symbol) in enumerate(symbols)
            )
            objective_lines.append(rf"\min\;&{weighted}")
        else:
            ordered = []
            for name, symbol in symbols:
                stage = next(
                    (item for item in stages if item.get("name") == name),
                    {},
                )
                sign = "-" if stage.get("sense") == "maximize" else ""
                ordered.append(f"{sign}{symbol}")
            objective_lines.append(
                r"\operatorname{lexmin}\;&\left(" + ",".join(ordered) + r"\right)"
            )
        for name, symbol in symbols:
            expression = terms.get(symbol)
            if expression:
                objective_lines.append(rf"{symbol}&={expression}")
            else:
                objective_lines.append(
                    rf"{symbol}&=\text{{{name}}}"
                )

        variables = r"""\begin{aligned}
&x_{i,k,l}\in\{0,1\} && \text{任务 }i\text{ 在候选 }k\text{、作业线 }l\text{ 上执行}\\
&s_{l,d},\,e_{l,d},\,y_{l,d}\in\{0,1\} && \text{开工、未完工与租船日}\\
&T\ge 0 && \text{最后完工时间}
\end{aligned}"""
        constraints = r"""\begin{aligned}
&\sum_{k\in K_i}\sum_{l\in L}x_{i,k,l}=1, && \forall i\\
&T\ge t_{i,k}\,x_{i,k,l}, && \forall i,k,l\\
&y_{l,d}\ge s_{l,d},\quad y_{l,d}\ge e_{l,d}, && \forall l,d\\
&s_{l,d}\ge x_{i,k,l}, && \text{作业在 }d\text{ 开工}\\
&\sum_{(i,k)\text{ 已执行}}q_{i,k}\le Q_d, && \forall d\\
&\text{同一作业线或同一风机上的作业不得重叠}, && \forall \text{资源}
\end{aligned}"""
        return {
            "objective": "\\begin{aligned}\n" + "\\\\\n".join(objective_lines) + "\n\\end{aligned}",
            "variables": variables,
            "constraints": constraints,
        }

    def _render_math_markdown(
        self,
        version: WindowVersion,
        artifact: dict[str, Any],
        section: str = "all",
    ) -> str:
        summary = version.resultSummary or {}
        dimensions = _dict(artifact.get("dimensions"))
        latex = self._render_math_latex(version, artifact)
        status = _status(version)
        proof = "已证明最优" if status == "OPTIMAL" else "尚未证明最优"
        fingerprint = artifact.get("modelFingerprint") or version.modelFingerprint or "未记录"
        pointer = self.plan_numbers.get(version.id, 1)
        stages = _list(_dict(artifact.get("objectives")).get("stages"))
        stage_text = "；".join(
            f"{item.get('priority')}. {item.get('sense')} {item.get('name')}"
            for item in stages
        )
        constraints_total = _dict(artifact.get("constraints")).get("total")
        variables_total = _dict(artifact.get("variables")).get("total")
        show_variables = section in {"all", "variables"}
        show_objective = section in {"all", "objective"}
        show_constraints = section in {"all", "constraints"}
        title = {
            "objective": "目标函数",
            "constraints": "约束条件",
            "variables": "决策变量",
        }.get(section, "数学模型")
        lines = [
            f"**方案 {pointer:02d} 的{title}**",
            "",
            f"求解状态：`{status}`，{proof}；模型指纹 `{str(fingerprint)[:12]}…`。",
            "",
        ]
        if show_variables:
            lines.extend([
                "**集合**",
                "",
                r"$I$：任务集合；$K_i$：任务 $i$ 的候选时间窗；$L$：作业线集合；$D$：日期集合。",
                "",
                "**决策变量**",
                "",
                "$$",
                latex["variables"],
                "$$",
                "",
            ])
        if show_objective:
            lines.extend([
                "**目标函数（分层字典序）**",
                "",
                "$$",
                latex["objective"],
                "$$",
                "",
            ])
        if show_constraints:
            lines.extend([
                "**主要约束**",
                "",
                "$$",
                latex["constraints"],
                "$$",
                "",
            ])
        lines.extend([
            "**本次规模**",
            "",
            (
                f"{dimensions.get('tasks', summary.get('totalTaskCount', 0))} 个任务、"
                f"{dimensions.get('windows', 0)} 个窗口、"
                f"{dimensions.get('candidateChoices', 0)} 个任务-候选-作业线选择；"
                f"{variables_total or 0} 个变量、{constraints_total or 0} 条约束。"
            ),
            "",
            "**符号含义**",
            "",
            (
                r"$c_{i,k,l}$：直接成本与停机损失变化；$r_l$：船租；"
                r"$t_{i,k}$：完工分钟；$q_{i,k}$：备件消耗；$Q_d$：第 $d$ 天库存上限。"
            ),
            "",
            f"目标层级：{stage_text or '本次产物未记录目标层'}。",
            "",
            "参数取值与来源可在方案详情“数学模型”页核对；本回答不重建系数。",
        ])
        return "\n".join(lines)

