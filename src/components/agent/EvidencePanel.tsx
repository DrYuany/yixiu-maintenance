import { CheckCircle2, CircleDashed, FileJson2, Fingerprint, ShieldCheck } from "lucide-react";
import type { DecisionArtifact, SolveEvidence, SolveJob, WindowVersion } from "../../store/types";

function statusTitle(evidence: SolveEvidence | null, job: SolveJob | null, version: WindowVersion | null) {
  if (job?.status === "QUEUED") return "任务已排队";
  if (job?.status === "RUNNING") return "真实求解进行中";
  if (job?.status === "FAILED") return "执行失败，未判定可行性";
  if (job?.status === "CANCELLED") return "求解已停止";
  if (!evidence) return "等待生成模型";
  const solver = (version?.resultSummary?.solver ?? {}) as Record<string, unknown>;
  if (String(solver["方案来源"] ?? "") === "HEURISTIC_FALLBACK" && evidence.hasIncumbent) {
    return "启发式兜底方案 · 未证明最优";
  }
  if (evidence.status === "OPTIMAL" && evidence.proof) return "已证明最优";
  if (evidence.status === "FEASIBLE" || evidence.hasIncumbent) return "已找到可行方案";
  if (evidence.status === "INFEASIBLE" && evidence.proof) return "已证明当前约束不可行";
  return "未找到方案，未证明无解";
}

export function EvidencePanel({ artifact, evidence, job, version }: { artifact: DecisionArtifact | null; evidence: SolveEvidence | null; job: SolveJob | null; version: WindowVersion | null }) {
  const fingerprint = artifact?.modelFingerprint ?? job?.modelFingerprint;
  const shortHash = fingerprint ? `${fingerprint.slice(0, 12)}…` : "等待生成";
  const running = job?.status === "QUEUED" || job?.status === "RUNNING";
  const summary = version?.resultSummary;
  const totalCost = Number(summary?.totalCost ?? 0);
  const completed = Number(summary?.scheduledTaskCount ?? 0);
  const totalTasks = Number(summary?.totalTaskCount ?? 0);
  const savings = summary?.baselineSavings as Record<string, number | null> | undefined;
  return (
    <aside className="evidence-panel" aria-label="决策证据">
      <header>
        <div><strong>本轮决策证据</strong><span>每一步均可回放与复核</span></div>
        <span className="evidence-badge">审计模式</span>
      </header>
      <section className="evidence-hero">
        <div><CircleDashed size={17} /><span>求解状态</span></div>
        <strong>{statusTitle(evidence, job, version)}</strong>
        <p>{running
          ? `任务 ${job?.id} 已运行 ${job?.elapsedSeconds ?? 0} 秒。${job?.nextDecisionAt ? `超过 ${job.nextDecisionAt} 秒仍没算完才会询问是否继续` : "时限内不会打断求解"}。`
          : evidence?.status === "UNKNOWN"
            ? "UNKNOWN 仅表示当前未取得结论，不等于无解。"
            : "可行性、最优性与复核状态分别记录，不混为一谈。"}</p>
      </section>
      {version ? (
        <section className="evidence-section result-detail-section">
          <h3>排程结果</h3>
          <div className="result-metric-grid">
            <div><span>总成本</span><strong>{(totalCost / 10000).toFixed(2)} 万元</strong></div>
            <div><span>完成任务</span><strong>{completed}/{totalTasks}</strong></div>
            <div><span>船舶艘天</span><strong>{String(summary?.vesselDays ?? "-")}</strong></div>
            <div><span>最晚完工</span><strong>{String(summary?.latestCompletion ?? "-").slice(0, 10)}</strong></div>
          </div>
          <div className="result-subtitle">建议窗口</div>
          <ul className="window-result-list">
            {version.windows.map((item) => <li key={item.id}><strong>{item.windowId}</strong><span>{item.metrics.startTime.slice(5)} 至 {item.metrics.endTime.slice(5)}</span></li>)}
          </ul>
          <div className="result-subtitle">基准差异</div>
          <ul className="window-result-list">
            {Object.entries(savings ?? {}).map(([name, value]) => <li key={name}><strong>{name}</strong><span>{value === null ? "任务集合不同，不宣称节省" : `${(value / 10000).toFixed(2)} 万元`}</span></li>)}
          </ul>
          <details className="schedule-details">
            <summary>查看任务明细（{version.repairSchedule.length}）</summary>
            {version.repairSchedule.slice(0, 10).map((item) => (
              <div key={item.taskId}><strong>{item.taskId} · {item.componentName}</strong><span>{item.startTime.slice(5)} 至 {item.endTime.slice(5)}</span></div>
            ))}
            {version.repairSchedule.length > 10 ? <p>其余 {version.repairSchedule.length - 10} 项已保存在本次版本中。</p> : null}
          </details>
        </section>
      ) : null}
      <section className="evidence-section">
        <h3><FileJson2 size={16} />模型契约</h3>
        <dl>
          <div><dt>策略</dt><dd>{String(job?.request.strategy ?? "待确认")}</dd></div>
          <div><dt>目标模式</dt><dd>{String(job?.request.mode ?? "待确认")}</dd></div>
          <div><dt>时间预算</dt><dd>{job ? `${String(job.request.budgetSeconds ?? job.request.seconds ?? "待确认")} 秒` : "待确认"}</dd></div>
        </dl>
      </section>
      <section className="evidence-section">
        <h3><ShieldCheck size={16} />复核清单</h3>
        <ul className="audit-list">
          <li><CheckCircle2 />自然语言已编译为规范契约</li>
          <li><CheckCircle2 />模型指纹已锁定</li>
          <li className={evidence?.hasIncumbent ? "" : "pending"}>{evidence?.hasIncumbent ? <CheckCircle2 /> : <CircleDashed />}排程可行性复核</li>
          <li className={evidence?.hasIncumbent ? "" : "pending"}>{evidence?.hasIncumbent ? <CheckCircle2 /> : <CircleDashed />}费用独立复算</li>
        </ul>
      </section>
      <section className="fingerprint-card"><Fingerprint size={17} /><div><span>模型指纹</span><code>{shortHash}</code></div></section>
    </aside>
  );
}
