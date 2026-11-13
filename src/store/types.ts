export type PageId = "P01" | "P02" | "P03" | "P04" | "P05" | "P06";

export type SortMode =
  | "latest_first"
  | "comprehensive"
  | "low_opportunity_cost"
  | "low_direct_cost"
  | "high_risk_priority";

export interface CandidateWindowMetrics {
  duration: number;
  opportunityCost: number;
  directCost: number;
  riskLevel: "low" | "medium" | "high";
  startTime: string;
  endTime: string;
}

export interface CandidateWindow {
  id: string;
  windowId: string;
  versionId: string;
  conversationId: string;
  generatedAt: string;
  focusTag: string;
  generatedFromPrompt: string;
  displayOrder: number;
  isPinned: boolean;
  isDeleted: boolean;
  metrics: CandidateWindowMetrics;
}

export interface RepairScheduleItem {
  taskId: string;
  turbineId: number;
  componentName: string;
  startHour: number;
  endHour: number;
  durationHours: number;
  startTime: string;
  endTime: string;
}

export interface WindowVersion {
  id: string;
  conversationId: string;
  sourceType: "user" | "system";
  generatedAt: string;
  userPrompt: string;
  focusTag: string;
  intentType: string;
  parameterSnapshot: Record<string, unknown>;
  resultSummary: Record<string, unknown>;
  repairSchedule: RepairScheduleItem[];
  windows: CandidateWindow[];
  includedInCandidatePool: boolean;
  solveJobId?: string | null;
  modelFingerprint?: string | null;
}

export interface ModelArtifactVariableGroup {
  id: string;
  name: string;
  type: string;
  count: number;
  lowerBound: number;
  upperBound: number;
  meaning: string;
  samples: string[];
}

export interface ModelArtifactConstraintGroup {
  kind: string;
  count: number;
  meaning: string;
  names: string[];
}

export interface ModelArtifactObjective {
  priority: number;
  name: string;
  sense: "minimize" | "maximize";
  formula: string;
  expressionCharacters: number;
  expressionTermCount: number;
  expressionPreview: string;
}

export interface ModelArtifactParameter {
  id: string;
  label: string;
  value: string | number;
  unit: string;
  source: string;
  baseSource?: string | null;
  dataNature?: string | null;
  asOf?: string | null;
  override: boolean;
}

export interface ModelArtifact {
  schemaVersion: string;
  modelType: string;
  problemStatement: string;
  strategy: string;
  mode: string;
  weights: Record<string, number> | null;
  modelFingerprint: string;
  dataSnapshot: Record<string, unknown>;
  dimensions: {
    tasks: number;
    windows: number;
    workLines: number;
    candidateChoices: number;
  };
  variables: {
    total: number;
    groups: ModelArtifactVariableGroup[];
  };
  objectives: {
    mode: string;
    stages: ModelArtifactObjective[];
    weights: Record<string, number> | null;
  };
  constraints: {
    total: number;
    groups: ModelArtifactConstraintGroup[];
    samples: Array<{
      id: string;
      expression: string;
      variableCount: number;
      lowerBound: number | null;
      upperBound: number | null;
    }>;
    expressionNote: string;
  };
  parameters: ModelArtifactParameter[];
  solverParameters: Record<string, unknown>;
  provenance: Record<string, unknown>;
  rawModel?: {
    file: string;
    sha256: string;
    sizeBytes: number;
  };
  artifactFile?: string;
}

export interface PendingModelChange {
  /** "dsl" 表示受控 DSL 变更（只有 dsl 段）；缺省或其它值表示旧规划语言变更（只有 operations）。 */
  kind?: string;
  baseVersionId: string | null;
  request?: Record<string, unknown>;
  operations?: Array<{
    kind: string;
    key: string;
    before: unknown;
    after: unknown;
  }>;
  dsl?: Record<string, unknown>;
  preview: string;
  sourcePrompt: string;
}

export interface Message {
  id: string;
  conversationId: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  versionIds?: string[];
  jobId?: string | null;
}

/** agent 过程可视化：后端 SSE `stage` 事件的一步。 */
export interface StageEvent {
  id: string;
  label: string;
  status: "running" | "done" | "error";
  detail?: string | null;
  /** 这一步实际调用的工具名；理解、整理回答这类步骤不带 */
  tool?: string | null;
}

export interface WorkingMemory {
  conversationId: string;
  currentGoal: string | null;
  currentIntent: string | null;
  activeView: PageId;
  activeFocusTag: string | null;
  selectedVersionIds: string[];
  selectedWindowIds: string[];
  activeFilters: Record<string, unknown>;
  latestResultId: string | null;
  pendingClarification: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  status: "idle" | "active" | "archived";
  lastMessagePreview: string;
  activeView: PageId;
  summary: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
  workingMemory: WorkingMemory;
}

export interface DashboardResponse {
  conversationId: string;
  sortMode: SortMode;
  versions: WindowVersion[];
  candidateWindowPool: CandidateWindow[];
  topRecommendations: CandidateWindow[];
}

export interface DegradedPayload {
  /** 机器可读的降级类别，如 agent_unavailable */
  kind: string;
  /** 给人看的一句话 */
  message: string;
  detail?: string | null;
}

/** 免费体验额度（服务端按访客计数）。enabled=false 表示不限轮次。 */
export interface QuotaInfo {
  enabled: boolean;
  limit: number;
  used: number;
  remaining: number;
  dailyCap: number;
  dailyUsed: number;
  dailyRemaining: number;
  /** 本次请求带了访客自己的密钥 */
  ownKey: boolean;
}

export interface ClarificationOption {
  label: string;
  value: string;
}

export interface AgentResponse {
  assistantMessage: {
    id: string;
    role: "assistant";
    content: string;
    versionIds?: string[];
  };
  actionType: string;
  recommendedView: PageId;
  clarificationOptions?: ClarificationOption[] | null;
  /**
   * 有值说明这一轮没有走成模型，答复是确定性兜底。
   * 兜底答复和正常答复长得一模一样，不单独标出来用户无从分辨。
   */
  degraded?: DegradedPayload | null;
  memoryPatch: Record<string, unknown>;
  newVersions: WindowVersion[];
  candidateWindowPool: CandidateWindow[];
  topRecommendations: CandidateWindow[];
  dashboardState: Record<string, unknown>;
  decisionArtifact: DecisionArtifact | null;
  solveEvidence: SolveEvidence | null;
  solveJob: SolveJob | null;
}

export interface SolveJob {
  id: string;
  conversationId: string;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  solverStatus: "OPTIMAL" | "FEASIBLE" | "UNKNOWN" | "INFEASIBLE";
  submittedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  elapsedSeconds: number;
  needsUserDecision: boolean;
  nextDecisionAt: number;
  backgrounded: boolean;
  hasIncumbent: boolean;
  modelFingerprint: string;
  phase: string;
  progressPercent: number;
  statusMessage: string;
  snapshot?: Record<string, unknown> | null;
  request: Record<string, unknown>;
  result?: {
    versionId?: string;
    summary?: Record<string, unknown> | string;
    decisionArtifact?: DecisionArtifact;
  } | null;
  error?: string | null;
  /** 一句话拆成多个场景时，这是第几个场景、一共几个 */
  batchPosition?: number | null;
  batchTotal?: number | null;
}

export interface DecisionArtifact {
  canonicalContext: Record<string, unknown>;
  canonicalDecision: Record<string, unknown>;
  canonicalModel: Record<string, unknown>;
  contextFingerprint: string;
  decisionFingerprint: string;
  modelFingerprint: string;
}

export interface SolveEvidence {
  status: "OPTIMAL" | "FEASIBLE" | "UNKNOWN" | "INFEASIBLE";
  hasIncumbent: boolean;
  proof: boolean;
  elapsedSeconds: number;
  phase: string;
  reason?: string | null;
  running: boolean;
  objectiveValue?: number | null;
  bestBound?: number | null;
  modelFingerprint?: string | null;
}
