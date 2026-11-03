from __future__ import annotations

import re
from typing import Any, Literal

from app.schemas.agent import WindowVersion


ModelQuestionKind = Literal[
    "capability", "structure", "environment", "formula", "parameter", "solution", "status"
]

_CAPABILITY_TOKENS = (
    "有哪些目标",
    "哪些目标函数",
    "可以设置权重",
    "能设置权重",
    "怎么设置权重",
    "如何设置权重",
    "权重怎么设",
    "有哪些指标",
    "支持哪些指标",
    "有哪些策略",
    "支持哪些策略",
    "哪些策略",
    "有哪些模式",
    "支持哪些模式",
    "能调什么",
    "可以调什么",
    "能改什么",
    "可以改什么",
    "默认权重",
)

_STRUCTURE_TOKENS = (
    "长什么样",
    "是什么样",
    "怎么构成",
    "由什么组成",
    "一般形式",
    "通用形式",
    "结构是什么",
    "模型结构",
)

_VERSION_SCOPED = re.compile(
    r"方案\s*0*\d|这版|这一版|这次|本次|这个方案|该方案|当前方案|ver_[a-f0-9]{8}|刚刚那",
    re.IGNORECASE,
)

_ENV_TOKENS = (
    "风速",
    "电价",
    "海况",
    "气象",
    "风况",
    "天气",
    "风大",
    "风小",
    "刮风",
    "适合作业",
    "能不能作业",
    "能不能出海",
    "出海条件",
    "作业条件",
)
_ENV_RANGE = (
    "未来", "接下来", "下周", "这一周", "本周", "一周", "7天", "七天",
    "近7", "近七", "这几天", "明天", "后天", "走势", "曲线", "可视化", "画",
    "今天", "怎么样", "如何", "情况", "适合作业", "能不能作业", "能不能出海",
)
_ENV_EXCLUDE = ("参数", "来源", "重排", "改成", "调整", "假设", "如果", "权重")


def detect_model_question(message: str) -> ModelQuestionKind | None:
    text = "".join(message.split())
    if not text:
        return None

    version_scoped = bool(_VERSION_SCOPED.search(text))
    if not version_scoped and any(token in text for token in _CAPABILITY_TOKENS):
        return "capability"
    if (
        any(token in text for token in _ENV_TOKENS)
        and any(token in text for token in _ENV_RANGE)
        and not any(token in text for token in _ENV_EXCLUDE)
    ):
        return "environment"

    parameter_question = any(
        token in text
        for token in (
            "参数怎么",
            "参数来源",
            "参数哪里",
            "系数怎么",
            "系数来源",
            "系数哪里",
            "怎么取得",
            "怎么来的",
            "取值来源",
            "船租怎么",
            "船租来源",
        )
    )
    parameter_question = parameter_question or (
        any(
            token in text
            for token in (
                "船租",
                "人工单价",
                "作业线数",
                "维修小时数",
                "转移小时数",
                "电价",
                "库存",
            )
        )
        and any(token in text for token in ("来源", "怎么", "取值", "哪来", "为什么"))
    )
    model_question = any(
        token in text
        for token in (
            "数学模型",
            "模型公式",
            "目标函数",
            "变量是什么",
            "有哪些变量",
            "约束是什么",
            "有哪些约束",
            "模型契约",
            "模型结构",
        )
    )
    if parameter_question:
        return "parameter"
    if model_question or (
        "模型" in text
        and any(token in text for token in ("给我", "看看", "导出", "列出", "写出", "讲讲", "解释"))
    ):
        return "formula" if version_scoped else "structure"
    if not version_scoped and any(token in text for token in _STRUCTURE_TOKENS):
        return "structure"
    if not version_scoped and "权重" in text and any(
        token in text for token in ("多少", "几", "怎么", "如何", "设置", "调")
    ):
        return "capability"

    if any(
        token in text
        for token in (
            "最优吗",
            "为什么不是最优",
            "可行吗",
            "求解状态",
            "求解多久",
            "还能继续算",
            "继续求解",
            "incumbent",
        )
    ):
        return "status"

    if any(
        token in text
        for token in (
            "为什么没排",
            "为什么没有排",
            "为什么未安排",
            "未安排原因",
            "方案怎么理解",
            "怎么理解这个方案",
            "为什么选这个窗口",
            "为什么选窗口",
            "结果呢",
            "解释一下方案",
            "解释这个方案",
        )
    ):
        return "solution"

    if re.search(r"任务\s*\d{2,4}", text) and any(
        token in text for token in ("为什么", "怎么", "没排", "未安排", "安排")
    ):
        return "solution"
    return None


def render_missing_artifact(version: WindowVersion) -> str:
    return (
        f"方案 {version.id} 没有保存完整数学模型产物，因此我不能给出变量、约束行或系数。\n"
        f"当前仍可读取：求解状态 {_solver_status(version)}、总成本 "
        f"{_money(version.resultSummary.get('totalCost'))}、窗口与任务排程。\n"
        "如果要查看真实模型，需要按同一输入重新运行一次并保存 ModelArtifact；"
        "旧结果不能事后凭参数重建后冒充原模型。"
    )


def render_model_formula(version: WindowVersion, artifact: dict[str, Any]) -> str:
    dimensions = artifact.get("dimensions") or {}
    variables = (artifact.get("variables") or {}).get("groups") or []
    objectives = (artifact.get("objectives") or {}).get("stages") or []
    constraints = (artifact.get("constraints") or {}).get("groups") or []
    lines = [
        f"方案 {version.id} 的真实模型结构如下（来自本次 CP-SAT 模型产物）：",
        f"规模：{dimensions.get('tasks', 0)} 个任务、{dimensions.get('windows', 0)} 个窗口、"
        f"{dimensions.get('candidateChoices', 0)} 个任务-候选-作业线选择，"
        f"{artifact.get('variables', {}).get('total', 0)} 个变量、"
        f"{artifact.get('constraints', {}).get('total', 0)} 条约束。",
        "变量：",
    ]
    for item in variables[:5]:
        lines.append(
            f"- {item.get('meaning')}；{item.get('type')}，"
            f"范围 [{item.get('lowerBound')}, {item.get('upperBound')}]，共 {item.get('count')} 个。"
        )
    lines.append("目标：")
    for stage in objectives:
        lines.append(
            f"{stage.get('priority')}. {stage.get('sense')} {stage.get('name')}："
            f"{stage.get('formula')}"
        )
        if stage.get("expressionPreview"):
            lines.append(
                f"   实际表达式片段：{stage.get('expressionPreview')}"
            )
    lines.append("约束：")
    for item in constraints[:5]:
        lines.append(f"- {item.get('meaning')}，共 {item.get('count')} 条。")
    samples = (artifact.get("constraints") or {}).get("samples") or []
    if samples:
        lines.append("约束表达式样本：")
        for sample in samples[:3]:
            lines.append(f"- {sample.get('expression')}")
    data = artifact.get("dataSnapshot") or {}
    lines.append(
        f"数据快照：{data.get('asOf') or '未记录'}；模型指纹：{artifact.get('modelFingerprint') or '未记录'}。"
    )
    lines.append("完整变量组、参数来源和约束统计可在方案详情的“数学模型”页查看。")
    return "\n".join(lines)


def render_parameter_answer(version: WindowVersion, artifact: dict[str, Any], message: str) -> str:
    parameters = artifact.get("parameters") or []
    matches = [
        item
        for item in parameters
        if item.get("id") in message or item.get("label") in message
    ]
    if not matches:
        names = "、".join(str(item.get("label")) for item in parameters[:10])
        return (
            "这条问题没有定位到具体参数。当前模型可查询的参数包括："
            f"{names}。请说出参数名，例如“船租怎么取的”。"
        )
    lines = [f"方案 {version.id} 的参数来源："]
    for item in matches:
        lines.append(
            f"{item.get('label')} = {item.get('value')} {item.get('unit')}；"
            f"来源：{item.get('source')}；数据性质：{item.get('dataNature') or '未记录'}；"
            f"规划时点：{item.get('asOf') or '未记录'}。"
        )
        if item.get("baseSource"):
            lines.append(f"基础数据：{item.get('baseSource')}")
        if item.get("override"):
            lines.append("该参数在本轮被用户覆盖，未覆盖项继承基础数据。")
    return "\n".join(lines)


def render_solution_answer(version: WindowVersion, message: str) -> str:
    summary = version.resultSummary or {}
    text = "".join(message.split())
    task_match = re.search(r"任务(\d{2,4})", text)
    if task_match:
        task_number = task_match.group(1)
        task_id = f"任务{task_number}"
        scheduled = next(
            (item for item in version.repairSchedule if item.taskId == task_id),
            None,
        )
        if scheduled:
            return (
                f"{task_id} 已安排在 {scheduled.startTime} 至 {scheduled.endTime}，"
                f"{scheduled.turbineId} 号风机 · {scheduled.componentName}，"
                f"工时 {scheduled.durationHours} 小时。"
            )
        unscheduled = next(
            (
                item
                for item in (summary.get("unscheduledTasks") or [])
                if item.get("任务编号") == task_id
            ),
            None,
        )
        if unscheduled:
            return (
                f"{task_id} 本窗口未安排。原因：{unscheduled.get('原因') or '本窗口资源或目标竞争未选入'}。"
                "这不等同于该任务单独不可行。"
            )
        return f"当前方案的任务明细中没有找到 {task_id}，请确认任务编号。"

    if any(token in text for token in ("窗口", "为什么选")):
        window = version.windows[0] if version.windows else None
        return (
            f"方案 {version.id} 选择的是 {window.windowId if window else '目标窗口'}，"
            f"时间 {window.metrics.startTime + ' 至 ' + window.metrics.endTime if window else '待确认'}。"
            f"该窗口内完成 {summary.get('scheduledTaskCount', 0)}/{summary.get('totalTaskCount', 0)} 项，"
            f"总成本 {_money(summary.get('totalCost'))}，船舶 {summary.get('vesselDays', 0)} 艘天。"
            "具体取舍来自该版本的模型约束和求解目标，可在方案详情查看任务与证据。"
        )

    unscheduled = summary.get("unscheduledTasks") or []
    if any(token in text for token in ("没排", "未安排")):
        if not unscheduled:
            return "当前方案没有未安排任务。"
        lines = [f"当前方案有 {len(unscheduled)} 项任务未安排："]
        for item in unscheduled[:8]:
            lines.append(
                f"- {item.get('任务编号') or item.get('风机编号')}："
                f"{item.get('原因') or '本窗口资源或目标竞争未选入'}"
            )
        if len(unscheduled) > 8:
            lines.append(f"其余 {len(unscheduled) - 8} 项可在方案详情查看。")
        return "\n".join(lines)

    return (
        f"方案 {version.id} 的状态是 {_solver_status(version)}。"
        f"建议窗口 {version.windows[0].metrics.startTime + ' 至 ' + version.windows[0].metrics.endTime if version.windows else '待确认'}，"
        f"完成 {summary.get('scheduledTaskCount', 0)}/{summary.get('totalTaskCount', 0)} 项，"
        f"总成本 {_money(summary.get('totalCost'))}，船舶 {summary.get('vesselDays', 0)} 艘天，"
        f"最晚完工 {str(summary.get('latestCompletion') or '待确认')[:16]}。"
    )


def render_capability_answer() -> str:
    """能力问句的确定性回答，不依赖任何版本。小一修答的是槽位旋钮，不是 DSL 目标模板。"""
    from yxcore import slots as S

    head = (
        "strategy 四选一：" + "、".join(S.STRATEGY_NAMES) + "\n"
        "weight 的 14 个量：" + "、".join(S.METRIC_NAMES)
    )
    from yxcore.prompt import _slot_lines, _boundary_text

    return "\n\n".join([head, _slot_lines(), _boundary_text()])


def render_structure_answer() -> str:
    """还没有具体方案时的结构解释：小一修是槽位填表模型，不是自定义公式。"""
    return (
        "小一修是槽位填表模型：约束的形状预先冻结，模型只往已注册的旋钮里填数，"
        "不生成自定义公式或约束。能改什么见上面的旋钮清单。"
    )


def render_status_answer(version: WindowVersion) -> str:
    summary = version.resultSummary or {}
    solver = summary.get("solver") or {}
    stages = solver.get("阶段") or []
    lines = [f"方案 {version.id} 的求解状态：{_solver_status(version)}。"]
    if stages:
        for stage in stages:
            stage_status = str(stage.get("状态") or "UNKNOWN")
            stage_text = {
                "OPTIMAL": "已证明最优",
                "FEASIBLE": "已找到可行解，未证明最优",
                "UNKNOWN": "在时限内未证明最优",
                "INFEASIBLE": "已证明不可行",
            }.get(stage_status, stage_status)
            lines.append(
                f"- {stage.get('目标')}: {stage_text}，耗时 {stage.get('耗时秒', 0):.2f} 秒。"
            )
    if solver.get("总耗时秒"):
        lines.append(f"总用时：{float(solver.get('总耗时秒')):.1f} 秒。")
    if solver.get("终止原因"):
        lines.append(f"终止原因：{solver.get('终止原因')}。")
    if str(solver.get("方案来源") or "") == "HEURISTIC_FALLBACK":
        scheduled = summary.get("scheduledTaskCount", 0)
        total = summary.get("totalTaskCount", 0)
        lines.append(
            f"本次结果来自启发式兜底：求解器没有产出排程，当前排程只覆盖 {scheduled}/{total} 个任务，"
            "不是求解器给出的可行解，也没有最优性证明。"
        )
    elif _solver_status(version) == "FEASIBLE":
        lines.append("这是已复核可行方案，但尚未证明在所有分层目标上最优。")
    elif _solver_status(version) == "UNKNOWN":
        lines.append("UNKNOWN 只表示当前未取得结论，不代表无解。")
    return "\n".join(lines)


def _solver_status(version: WindowVersion) -> str:
    summary = version.resultSummary or {}
    solver = summary.get("solver") or {}
    explicit = solver.get("解状态")
    if explicit in {"OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"}:
        return explicit
    if solver.get("分层全部最优"):
        return "OPTIMAL"
    if solver.get("有解") or version.repairSchedule:
        return "FEASIBLE"
    return "UNKNOWN"


def _money(value: Any) -> str:
    try:
        return f"{float(value) / 10000:.2f} 万元"
    except (TypeError, ValueError):
        return "待确认"
