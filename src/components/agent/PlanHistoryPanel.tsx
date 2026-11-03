import {
  ArrowUpRight,
  Clock3,
  History,
  Layers3,
  LoaderCircle,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import type { SolveJob, WindowVersion } from "../../store/types";
import {
  formatCompactMoney,
  formatGeneratedAt,
  formatWindowRange,
  getSolverPresentation,
  sortVersionsChronologically,
} from "./plan-format";

interface Props {
  versions: WindowVersion[];
  activeJob: SolveJob | null;
  selectedVersionId: string | null;
  onOpenVersion: (versionId: string) => void;
}

export function PlanHistoryPanel({
  versions,
  activeJob,
  selectedVersionId,
  onOpenVersion,
}: Props) {
  const chronological = sortVersionsChronologically(versions);
  const newestFirst = [...chronological].reverse();
  const running = activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING";

  return (
    <aside className="plan-history-panel" aria-label="方案记录">
      <header>
        <div>
          <span className="panel-kicker"><History size={13} />本会话方案</span>
          <h2>方案记录</h2>
        </div>
        <span className="plan-count">{versions.length}</span>
      </header>

      <p className="plan-history-intro">
        每次重新排程都会新增一份方案，历史结果不被覆盖。点开任意方案查看完整排程。
      </p>

      <div className="plan-history-list">
        {running ? (
          <div className="plan-live-card" aria-live="polite">
            <div className="plan-live-head">
              <span><LoaderCircle size={15} className="spin" />正在生成新方案</span>
              <strong>{activeJob?.progressPercent ?? 5}%</strong>
            </div>
            <div className="plan-live-track">
              <i style={{ width: `${activeJob?.progressPercent ?? 5}%` }} />
            </div>
            <small>{activeJob?.statusMessage || "编译约束并执行 CP-SAT 求解"}</small>
          </div>
        ) : null}

        {newestFirst.map((version, index) => {
          const planNumber = chronological.length - index;
          const status = getSolverPresentation(version.resultSummary);
          const summary = version.resultSummary;
          const window = version.windows[0] ?? null;
          const selected = version.id === selectedVersionId;
          return (
            <button
              type="button"
              key={version.id}
              className={`plan-history-item ${selected ? "selected" : ""}`}
              aria-label={`查看方案 ${planNumber} 详情`}
              onClick={() => onOpenVersion(version.id)}
            >
              <div className="plan-history-topline">
                <span className="plan-history-number">方案 {String(planNumber).padStart(2, "0")}</span>
                <span className={`mini-solver-state ${status.tone}`}>
                  <ShieldCheck size={12} />{status.shortLabel}
                </span>
              </div>

              <strong className="plan-history-window">{formatWindowRange(window)}</strong>

              <div className="plan-history-metrics">
                <span>
                  <small>总成本</small>
                  <strong>{formatCompactMoney(summary.totalCost)}</strong>
                </span>
                <span>
                  <small>计划完成</small>
                  <strong>{String(summary.scheduledTaskCount ?? 0)}/{String(summary.totalTaskCount ?? 0)}</strong>
                </span>
              </div>

              <p className="plan-history-prompt">
                <Sparkles size={13} />
                <span>{version.userPrompt || version.focusTag || "检修窗口建议"}</span>
              </p>

              <div className="plan-history-footer">
                <span><Clock3 size={12} />{formatGeneratedAt(version.generatedAt)}</span>
                <span>查看详情<ArrowUpRight size={12} /></span>
              </div>
            </button>
          );
        })}

        {!versions.length && !running ? (
          <div className="plan-history-empty">
            <span><Layers3 size={22} /></span>
            <strong>还没有排程方案</strong>
            <p>在左侧描述检修目标与约束，第一份方案会出现在这里。</p>
          </div>
        ) : null}
      </div>

      <footer className="plan-history-note">
        <ShieldCheck size={14} />
        方案与审计证据分开展示，可行性和最优性不会混写。
      </footer>
    </aside>
  );
}
