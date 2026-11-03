import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CalendarRange,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Coins,
  FileCode2,
  LoaderCircle,
  ShieldCheck,
  Ship,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import type {
  DecisionArtifact,
  ModelArtifact,
  SolveJob,
  WindowVersion,
} from "../../store/types";
import { apiClient } from "../../lib/api-client";
import {
  formatDateTime,
  formatGeneratedAt,
  formatMoney,
  formatWindowRange,
  getBaselineComparisons,
  getPlanningBoundary,
  getSolverPresentation,
  windowDurationDays,
} from "./plan-format";

interface Props {
  version: WindowVersion | null;
  planNumber: number | null;
  decisionArtifact: DecisionArtifact | null;
  job: SolveJob | null;
  onClose: () => void;
}

type DetailTab = "overview" | "tasks" | "model" | "audit";

const tabs: Array<{ id: DetailTab; label: string }> = [
  { id: "overview", label: "方案概览" },
  { id: "tasks", label: "检修排程" },
  { id: "model", label: "数学模型" },
  { id: "audit", label: "求解与审计" },
];

export function PlanDetailDrawer({
  version,
  planNumber,
  decisionArtifact,
  job,
  onClose,
}: Props) {
  const [tab, setTab] = useState<DetailTab>("overview");
  const [modelArtifact, setModelArtifact] = useState<ModelArtifact | null>(null);
  const [modelState, setModelState] = useState<"idle" | "loading" | "ready" | "missing" | "error">("idle");

  useEffect(() => {
    if (!version) return;
    setTab("overview");
    setModelArtifact(null);
    setModelState("idle");
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [version?.id, onClose]);

  useEffect(() => {
    if (!version || tab !== "model" || modelState !== "idle") {
      return;
    }
    let cancelled = false;
    setModelState("loading");
    apiClient
      .getModelArtifact(version.id)
      .then((artifact) => {
        if (cancelled) return;
        setModelArtifact(artifact);
        setModelState("ready");
      })
      .catch((error) => {
        if (cancelled) return;
        setModelArtifact(null);
        setModelState(
          error instanceof Error && error.message.includes("detail")
            ? "missing"
            : "error",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [tab, version?.id]);

  if (!version) return null;

  const summary = version.resultSummary;
  const status = getSolverPresentation(summary);
  const primaryWindow = version.windows[0] ?? null;
  const baselines = getBaselineComparisons(version);
  const planningBoundary = getPlanningBoundary(version);
  const solver = (summary.solver ?? {}) as Record<string, unknown>;
  const stages = Array.isArray(solver["阶段"])
    ? (solver["阶段"] as Array<Record<string, unknown>>)
    : [];
  const unscheduled = Array.isArray(summary.unscheduledTasks)
    ? (summary.unscheduledTasks as Array<Record<string, unknown>>)
    : [];
  const fingerprint =
    version.modelFingerprint ??
    (job?.result?.versionId === version.id ? decisionArtifact?.modelFingerprint : null) ??
    job?.modelFingerprint ??
    null;
  const jobId = version.solveJobId ?? (job?.result?.versionId === version.id ? job.id : null);
  const verified = Boolean(summary.verified);
  const durationDays = windowDurationDays(primaryWindow);

  return (
    <div
      className="plan-detail-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="plan-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`方案 ${planNumber ?? ""} 详情`}
      >
        <header className="plan-detail-header">
          <div>
            <span>方案 {String(planNumber ?? 1).padStart(2, "0")} · 创建于 {formatGeneratedAt(version.generatedAt)}</span>
            <h2>{version.focusTag || "检修窗口建议"}</h2>
          </div>
          <button type="button" aria-label="关闭方案详情" onClick={onClose}><X size={19} /></button>
        </header>

        <div className="plan-detail-status">
          <div className={`detail-status-mark ${status.tone}`}>
            <ShieldCheck size={19} />
          </div>
          <div>
            <strong>{status.label}</strong>
            <p>{status.description}</p>
          </div>
          <span className={`detail-status-code ${status.tone}`}>{status.code}</span>
        </div>

        <nav className="plan-detail-tabs" role="tablist" aria-label="方案详情分区">
          {tabs.map((item) => (
            <button
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              className={tab === item.id ? "active" : ""}
              key={item.id}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="plan-detail-content" role="tabpanel">
          {tab === "overview" ? (
            <>
              <section className="detail-section">
                <div className="detail-section-heading">
                  <div><span>核心结论</span><h3>这段时间做哪些、花多少钱</h3></div>
                </div>
                <div className="detail-kpi-grid">
                  <div><Coins size={18} /><span>总经济成本<strong>{formatMoney(summary.totalCost)}</strong></span></div>
                  <div><Wrench size={18} /><span>计划完成<strong>{String(summary.scheduledTaskCount ?? 0)} / {String(summary.totalTaskCount ?? 0)} 项</strong></span></div>
                  <div><Ship size={18} /><span>船舶占用<strong>{String(summary.vesselDays ?? 0)} 艘天</strong></span></div>
                  <div><Clock3 size={18} /><span>最晚完工<strong>{formatDateTime(summary.latestCompletion)}</strong></span></div>
                </div>
              </section>

              <section className="detail-section">
                <div className="detail-section-heading">
                  <div><span>时间窗口</span><h3>优先执行哪一个窗口</h3></div>
                  <span className="section-meta">{version.windows.length} 个候选窗口</span>
                </div>
                <div className="detail-window-primary">
                  <div className="detail-window-icon"><CalendarRange size={21} /></div>
                  <div>
                    <span>{primaryWindow?.windowId ?? "目标窗口"}</span>
                    <strong>{formatWindowRange(primaryWindow)}</strong>
                    <small>
                      {durationDays ? `${durationDays} 天可作业` : "窗口时长待确认"}
                      {planningBoundary ? ` · 规划边界 ${formatDateTime(planningBoundary)} 之后` : ""}
                    </small>
                  </div>
                </div>
                {version.windows.length > 1 ? (
                  <div className="detail-window-list">
                    {version.windows.slice(1).map((item) => (
                      <div key={item.id}>
                        <span>{item.windowId}</span>
                        <strong>{formatWindowRange(item)}</strong>
                        <small>{item.metrics.riskLevel === "high" ? "高风险任务" : "常规窗口"}</small>
                      </div>
                    ))}
                  </div>
                ) : null}
              </section>

              <section className="detail-section">
                <div className="detail-section-heading">
                  <div><span>成本对照</span><h3>相对两套人工基准差多少</h3></div>
                </div>
                {baselines.length ? (
                  <div className="baseline-comparison-list">
                    {baselines.map((item) => (
                      <div className="baseline-comparison-row" key={item.name}>
                        <div>
                          <strong>{item.name}</strong>
                          <span>{item.detail}</span>
                        </div>
                        <em className={item.tone}>{item.label}</em>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="detail-empty-note">该历史方案没有保存基准对照数据。</div>
                )}
              </section>

              <section className="detail-section compact">
                <div className="detail-prompt">
                  <span><Sparkles size={14} />本轮需求</span>
                  <p>{version.userPrompt || "未记录原始需求"}</p>
                </div>
              </section>
            </>
          ) : null}

          {tab === "tasks" ? (
            <>
              <section className="detail-section">
                <div className="detail-section-heading">
                  <div><span>窗口内排程</span><h3>按开始时间执行的任务</h3></div>
                  <span className="section-meta">{version.repairSchedule.length} 项</span>
                </div>
                {version.repairSchedule.length ? (
                  <div className="detail-task-list">
                    <div className="detail-task-head">
                      <span>任务 / 部件</span><span>开始</span><span>结束</span><span>工时</span>
                    </div>
                    {version.repairSchedule.map((task, index) => (
                      <div className="detail-task-row" key={`${task.taskId}-${index}`}>
                        <div>
                          <span className="task-index">{String(index + 1).padStart(2, "0")}</span>
                          <span>
                            <strong>{task.taskId}</strong>
                            <small>{task.turbineId ? `${task.turbineId} 号风机 · ` : ""}{task.componentName}</small>
                          </span>
                        </div>
                        <span>{formatDateTime(task.startTime)}</span>
                        <span>{formatDateTime(task.endTime)}</span>
                        <strong>{task.durationHours}h</strong>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="detail-empty-note">该方案没有保存任务级排程。</div>
                )}
              </section>

              {unscheduled.length ? (
                <section className="detail-section">
                  <details className="unscheduled-detail" open>
                    <summary>
                      <span><AlertTriangle size={15} />未安排任务（{unscheduled.length}）</span>
                      <ChevronRight size={16} />
                    </summary>
                    <div className="unscheduled-detail-list">
                      {unscheduled.map((task, index) => (
                        <div key={`${String(task["任务编号"] ?? index)}-${index}`}>
                          <strong>{String(task["任务编号"] ?? task["风机编号"] ?? "任务")}</strong>
                          <span>{String(task["原因"] ?? "本窗口资源竞争未选入")}</span>
                        </div>
                      ))}
                    </div>
                  </details>
                </section>
              ) : null}
            </>
          ) : null}

          {tab === "model" ? (
            <>
              {modelState === "loading" ? (
                <section className="detail-section model-loading">
                  <LoaderCircle size={20} className="spin" />
                  <span>正在读取本次 CP-SAT 模型产物…</span>
                </section>
              ) : null}
              {modelState === "missing" || modelState === "error" ? (
                <section className="detail-section">
                  <div className="model-unavailable">
                    <FileCode2 size={24} />
                    <strong>本次运行未保存完整模型产物</strong>
                    <p>
                      旧版本不能凭请求参数重建模型并冒充原模型。要查看真实变量、目标和约束，
                      需要按相同输入重新运行一次。
                    </p>
                    <code>{version.id}</code>
                  </div>
                </section>
              ) : null}
              {modelState === "ready" && modelArtifact ? (
                <>
                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <div><span>模型定义</span><h3>{modelArtifact.modelType}</h3></div>
                      <span className="section-meta">{modelArtifact.mode}</span>
                    </div>
                    <p className="model-problem-statement">{modelArtifact.problemStatement}</p>
                    <div className="model-dimension-grid">
                      <div><span>任务</span><strong>{modelArtifact.dimensions.tasks}</strong></div>
                      <div><span>窗口</span><strong>{modelArtifact.dimensions.windows}</strong></div>
                      <div><span>候选选择</span><strong>{modelArtifact.dimensions.candidateChoices}</strong></div>
                      <div><span>变量 / 约束</span><strong>{modelArtifact.variables.total} / {modelArtifact.constraints.total}</strong></div>
                    </div>
                  </section>

                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <div><span>目标函数</span><h3>分层或加权目标</h3></div>
                      <span className="section-meta">{modelArtifact.objectives.stages.length} 层</span>
                    </div>
                    <div className="model-objective-list">
                      {modelArtifact.objectives.stages.map((stage) => (
                        <div key={`${stage.priority}-${stage.name}`}>
                          <span>{String(stage.priority).padStart(2, "0")}</span>
                          <div>
                            <strong>{stage.sense} · {stage.name}</strong>
                            <p>{stage.formula}</p>
                            <code className="model-expression-preview">
                              {stage.expressionPreview}
                            </code>
                            <small className="model-expression-meta">
                              {stage.expressionTermCount} 项 · {stage.expressionCharacters} 字符
                            </small>
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <div><span>决策变量</span><h3>变量组与有限上下界</h3></div>
                      <span className="section-meta">{modelArtifact.variables.total} 个</span>
                    </div>
                    <div className="model-variable-list">
                      {modelArtifact.variables.groups.map((variable) => (
                        <div key={variable.id}>
                          <div>
                            <strong>{variable.name}</strong>
                            <p>{variable.meaning}</p>
                          </div>
                          <span>{variable.type}</span>
                          <span>[{variable.lowerBound}, {variable.upperBound}]</span>
                          <strong>{variable.count}</strong>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <div><span>约束</span><h3>约束组与业务含义</h3></div>
                      <span className="section-meta">{modelArtifact.constraints.total} 条</span>
                    </div>
                    <div className="model-constraint-list">
                      {modelArtifact.constraints.groups.map((constraint) => (
                        <div key={constraint.kind}>
                          <span>{constraint.kind}</span>
                          <p>{constraint.meaning}</p>
                          <strong>{constraint.count}</strong>
                        </div>
                      ))}
                    </div>
                    {modelArtifact.constraints.samples.length ? (
                      <details className="model-constraint-samples">
                        <summary>
                          查看真实约束表达式样本（{modelArtifact.constraints.samples.length}）
                        </summary>
                        <p>{modelArtifact.constraints.expressionNote}</p>
                        <div>
                          {modelArtifact.constraints.samples.map((sample) => (
                            <article key={sample.id}>
                              <header>
                                <strong>{sample.id}</strong>
                                <span>{sample.variableCount} 个变量</span>
                              </header>
                              <code>{sample.expression}</code>
                            </article>
                          ))}
                        </div>
                      </details>
                    ) : null}
                  </section>

                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <div><span>参数来源</span><h3>取值、单位与数据来源</h3></div>
                    </div>
                    <div className="model-parameter-table">
                      {modelArtifact.parameters.map((parameter) => (
                        <div key={parameter.id}>
                          <div>
                            <strong>{parameter.label}</strong>
                            <small>{parameter.source}{parameter.override ? " · 本轮覆盖" : ""}</small>
                          </div>
                          <strong>{String(parameter.value)} {parameter.unit}</strong>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="detail-section compact">
                    <div className="model-artifact-foot">
                      <div>
                        <span>模型指纹</span>
                        <code>{modelArtifact.modelFingerprint}</code>
                      </div>
                      {modelArtifact.rawModel ? (
                        <div>
                          <span>原始求解器模型</span>
                          <code>{modelArtifact.rawModel.file} · {modelArtifact.rawModel.sizeBytes} bytes</code>
                        </div>
                      ) : null}
                    </div>
                  </section>
                </>
              ) : null}
            </>
          ) : null}

          {tab === "audit" ? (
            <>
              <section className="detail-section">
                <div className="detail-section-heading">
                  <div><span>确定性证据</span><h3>求解、复核与模型指纹</h3></div>
                </div>
                <div className="audit-summary-grid">
                  <div>
                    <span>求解状态</span>
                    <strong>{status.code}</strong>
                  </div>
                  <div>
                    <span>独立复核</span>
                    <strong className={verified ? "positive" : "warning"}>
                      {verified ? "通过" : "未通过/未记录"}
                    </strong>
                  </div>
                  <div>
                    <span>策略</span>
                    <strong>{String(summary.strategy ?? version.focusTag ?? "待确认")}</strong>
                  </div>
                  <div>
                    <span>目标模式</span>
                    <strong>{String(summary.objectiveMode ?? "待确认")}</strong>
                  </div>
                </div>
                <div className="audit-checks">
                  <span><CheckCircle2 />自然语言已编译为规范契约</span>
                  <span><CheckCircle2 />求解状态与最优性分开记录</span>
                  <span className={verified ? "" : "pending"}>
                    {verified ? <CheckCircle2 /> : <AlertTriangle />}
                    排程可行性独立复核
                  </span>
                  <span className={verified ? "" : "pending"}>
                    {verified ? <CheckCircle2 /> : <AlertTriangle />}
                    费用独立复算
                  </span>
                </div>
              </section>

              <section className="detail-section">
                <div className="audit-technical-list">
                  <div>
                    <span>方案 ID</span>
                    <code>{version.id}</code>
                  </div>
                  <div>
                    <span>求解任务 ID</span>
                    <code>{jobId ?? "历史记录未保存"}</code>
                  </div>
                  <div>
                    <span>模型指纹</span>
                    <code>{fingerprint ?? "历史记录未保存"}</code>
                  </div>
                  <div>
                    <span>输入哈希</span>
                    <code>{String(((summary.data ?? {}) as Record<string, unknown>)["输入哈希"] ?? "未记录")}</code>
                  </div>
                </div>
              </section>

              {stages.length ? (
                <section className="detail-section">
                  <div className="detail-section-heading">
                    <div><span>分层求解</span><h3>每个目标的求解结果</h3></div>
                  </div>
                  <div className="solver-stage-list">
                    {stages.map((stage, index) => (
                      <div key={`${String(stage["目标"] ?? "stage")}-${index}`}>
                        <span>{String(stage["目标"] ?? `第 ${index + 1} 层`)}</span>
                        <strong>{String(stage["状态"] ?? "UNKNOWN")}</strong>
                        <small>
                          {stage["目标值"] !== undefined ? `目标值 ${String(stage["目标值"])}` : ""}
                          {stage["界"] !== undefined ? ` · 界 ${String(stage["界"])}` : ""}
                        </small>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}

              <details className="raw-evidence">
                <summary>查看原始求解摘要</summary>
                <pre>{JSON.stringify(solver, null, 2)}</pre>
              </details>
            </>
          ) : null}
        </div>
      </section>
    </div>
  );
}
