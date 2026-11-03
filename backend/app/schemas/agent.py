from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class CandidateWindowMetrics(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    duration: float
    opportunityCost: float = Field(validation_alias=AliasChoices("opportunityCost", "opportunity_cost"))
    directCost: float = Field(validation_alias=AliasChoices("directCost", "direct_cost"))
    riskLevel: str = Field(validation_alias=AliasChoices("riskLevel", "risk_level"))
    startTime: str = Field(validation_alias=AliasChoices("startTime", "start_time"))
    endTime: str = Field(validation_alias=AliasChoices("endTime", "end_time"))


class CandidateWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    windowId: str = Field(validation_alias=AliasChoices("windowId", "window_id"))
    versionId: str = Field(validation_alias=AliasChoices("versionId", "version_id"))
    conversationId: str = Field(validation_alias=AliasChoices("conversationId", "conversation_id"))
    generatedAt: str = Field(validation_alias=AliasChoices("generatedAt", "generated_at"))
    focusTag: str = Field(validation_alias=AliasChoices("focusTag", "focus_tag"))
    generatedFromPrompt: str = Field(validation_alias=AliasChoices("generatedFromPrompt", "generated_from_prompt"))
    displayOrder: int = Field(validation_alias=AliasChoices("displayOrder", "display_order"))
    isPinned: bool = Field(default=False, validation_alias=AliasChoices("isPinned", "is_pinned"))
    isDeleted: bool = Field(default=False, validation_alias=AliasChoices("isDeleted", "is_deleted"))
    metrics: CandidateWindowMetrics


class RepairScheduleItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    taskId: str = Field(validation_alias=AliasChoices("taskId", "task_id"))
    turbineId: int = Field(validation_alias=AliasChoices("turbineId", "turbine_id"))
    componentName: str = Field(validation_alias=AliasChoices("componentName", "component_name"))
    startHour: int = Field(validation_alias=AliasChoices("startHour", "start_hour"))
    endHour: int = Field(validation_alias=AliasChoices("endHour", "end_hour"))
    durationHours: int = Field(validation_alias=AliasChoices("durationHours", "duration_hours"))
    startTime: str = Field(validation_alias=AliasChoices("startTime", "start_time"))
    endTime: str = Field(validation_alias=AliasChoices("endTime", "end_time"))


class WindowVersion(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    conversationId: str = Field(validation_alias=AliasChoices("conversationId", "conversation_id"))
    sourceType: Literal["user", "system"] = Field(default="user", validation_alias=AliasChoices("sourceType", "source_type"))
    generatedAt: str = Field(validation_alias=AliasChoices("generatedAt", "generated_at"))
    userPrompt: str = Field(validation_alias=AliasChoices("userPrompt", "user_prompt"))
    focusTag: str = Field(validation_alias=AliasChoices("focusTag", "focus_tag"))
    intentType: str = Field(validation_alias=AliasChoices("intentType", "intent_type"))
    parameterSnapshot: dict[str, Any] = Field(validation_alias=AliasChoices("parameterSnapshot", "parameter_snapshot"))
    resultSummary: dict[str, Any] = Field(validation_alias=AliasChoices("resultSummary", "result_summary"))
    repairSchedule: list[RepairScheduleItem] = Field(
        default_factory=list,
        validation_alias=AliasChoices("repairSchedule", "repair_schedule"),
    )
    windows: list[CandidateWindow] = Field(default_factory=list)
    includedInCandidatePool: bool = Field(default=True, validation_alias=AliasChoices("includedInCandidatePool", "included_in_candidate_pool"))
    solveJobId: str | None = Field(default=None, validation_alias=AliasChoices("solveJobId", "solve_job_id"))
    modelFingerprint: str | None = Field(default=None, validation_alias=AliasChoices("modelFingerprint", "model_fingerprint"))


class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    conversationId: str = Field(validation_alias=AliasChoices("conversationId", "conversation_id"))
    role: Literal["user", "assistant"]
    content: str
    createdAt: str = Field(validation_alias=AliasChoices("createdAt", "created_at"))
    versionIds: list[str] = Field(default_factory=list, validation_alias=AliasChoices("versionIds", "version_ids"))
    jobId: str | None = Field(default=None, validation_alias=AliasChoices("jobId", "job_id"))


class WorkingMemory(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conversationId: str = Field(validation_alias=AliasChoices("conversationId", "conversation_id"))
    currentGoal: str | None = Field(default=None, validation_alias=AliasChoices("currentGoal", "current_goal"))
    currentIntent: str | None = Field(default=None, validation_alias=AliasChoices("currentIntent", "current_intent"))
    activeView: str = Field(default="P01", validation_alias=AliasChoices("activeView", "active_view"))
    activeFocusTag: str | None = Field(default=None, validation_alias=AliasChoices("activeFocusTag", "active_focus_tag"))
    selectedVersionIds: list[str] = Field(default_factory=list, validation_alias=AliasChoices("selectedVersionIds", "selected_version_ids"))
    selectedWindowIds: list[str] = Field(default_factory=list, validation_alias=AliasChoices("selectedWindowIds", "selected_window_ids"))
    activeFilters: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("activeFilters", "active_filters"))
    latestResultId: str | None = Field(default=None, validation_alias=AliasChoices("latestResultId", "latest_result_id"))
    pendingClarification: str | None = Field(default=None, validation_alias=AliasChoices("pendingClarification", "pending_clarification"))


class AssistantMessagePayload(BaseModel):
    id: str
    role: Literal["assistant"] = "assistant"
    content: str
    versionIds: list[str] = Field(default_factory=list)


class DecisionArtifactPayload(BaseModel):
    canonicalContext: dict[str, Any]
    canonicalDecision: dict[str, Any]
    canonicalModel: dict[str, Any]
    contextFingerprint: str
    decisionFingerprint: str
    modelFingerprint: str


class SolveEvidencePayload(BaseModel):
    status: Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"]
    hasIncumbent: bool = False
    proof: bool = False
    elapsedSeconds: float = 0
    phase: str = "solve"
    reason: str | None = None
    running: bool = False
    objectiveValue: float | None = None
    bestBound: float | None = None
    modelFingerprint: str | None = None


class SolveJobPayload(BaseModel):
    id: str
    conversationId: str
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
    solverStatus: Literal["OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"] = "UNKNOWN"
    submittedAt: str
    startedAt: str | None = None
    finishedAt: str | None = None
    elapsedSeconds: int = 0
    needsUserDecision: bool = False
    nextDecisionAt: int = 180
    backgrounded: bool = False
    hasIncumbent: bool = False
    modelFingerprint: str = ""
    phase: str = "queued"
    progressPercent: int = 5
    statusMessage: str = "正在锁定数据快照"
    snapshot: dict[str, Any] | None = None
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    # 一句话拆成多个场景时，告诉前端这是第几个、一共几个
    batchPosition: int | None = None
    batchTotal: int | None = None


class DegradedPayload(BaseModel):
    """这一轮没能走模型，答复是确定性兜底。

    为什么要有这个字段：兜底产出的句子看上去和正常答复一模一样（都是完整的
    中文、都带数字），用户没法分辨"模型读过你的话"和"正则猜的"。以前这里
    是 `except ConversationAgentError: pass`——调用失败静默滑到兜底，
    界面上什么痕迹都没有，于是同一个问题时好时坏，还查不出原因。

    kind 是给机器看的（agent_unavailable / model_question_fallback …），
    message 是给人看的一句话，detail 只放异常摘要，别塞 prompt 或密钥。
    """

    kind: str
    message: str
    detail: str | None = None


class AgentResponse(BaseModel):
    assistantMessage: AssistantMessagePayload
    actionType: str
    recommendedView: str
    memoryPatch: dict[str, Any] = Field(default_factory=dict)
    newVersions: list[WindowVersion] = Field(default_factory=list)
    candidateWindowPool: list[CandidateWindow] = Field(default_factory=list)
    topRecommendations: list[CandidateWindow] = Field(default_factory=list)
    dashboardState: dict[str, Any] = Field(default_factory=dict)
    decisionArtifact: DecisionArtifactPayload | None = None
    solveEvidence: SolveEvidencePayload | None = None
    solveJob: SolveJobPayload | None = None
    traceId: str | None = None
    degraded: DegradedPayload | None = None
