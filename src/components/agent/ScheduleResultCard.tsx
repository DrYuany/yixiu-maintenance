import {
  ArrowUpRight,
  CheckCircle2,
  Clock3,
  Coins,
  ShieldCheck,
  Ship,
  Sparkles,
  Wrench,
} from "lucide-react";
import type { WindowVersion } from "../../store/types";
import {
  formatDateTime,
  formatMoney,
  getBaselineComparisons,
  getSolverPresentation,
} from "./plan-format";
import { WindowTimeline } from "./WindowTimeline";

interface Props {
  version: WindowVersion;
  planNumber: number;
  onOpen: (versionId: string) => void;
}

export function ScheduleResultCard({ version, planNumber, onOpen }: Props) {
  const summary = version.resultSummary;
  const window = version.windows[0] ?? null;
  const status = getSolverPresentation(summary);
  const baselines = getBaselineComparisons(version);

  return (
    <article
      className="schedule-result-card window-answer-card"
      aria-label={`检修排程结果，方案 ${planNumber}`}
    >
      <div className="window-answer-head">
        <div className="window-plan-mark">
          <span><Sparkles size={15} /></span>
          <div>
            <small>排程方案 {String(planNumber).padStart(2, "0")}</small>
            <strong>{version.focusTag || "检修窗口建议"}</strong>
          </div>
        </div>
        <span className={`plan-status-pill ${status.tone}`}>
          <ShieldCheck size={13} />
          {status.label}
        </span>
      </div>

      <WindowTimeline window={window} schedule={version.repairSchedule} />

      <div className="window-answer-metrics">
        <div>
          <Coins size={17} />
          <span>总经济成本<strong>{formatMoney(summary.totalCost)}</strong></span>
        </div>
        <div>
          <Wrench size={17} />
          <span>
            计划完成
            <strong>{String(summary.scheduledTaskCount ?? 0)} / {String(summary.totalTaskCount ?? 0)} 项</strong>
          </span>
        </div>
        <div>
          <Ship size={17} />
          <span>船舶占用<strong>{String(summary.vesselDays ?? 0)} 艘天</strong></span>
        </div>
        <div>
          <Clock3 size={17} />
          <span>最晚完工<strong>{formatDateTime(summary.latestCompletion)}</strong></span>
        </div>
      </div>

      {baselines.length ? (
        <div className="window-baseline-strip">
          {baselines.map((item) => (
            <span
              className={`baseline-pill ${item.tone}`}
              key={item.name}
              title={item.detail}
            >
              <small>较{item.name}</small>
              <strong>{item.label}</strong>
            </span>
          ))}
          {baselines.some((item) => item.value === null) ? (
            <span className="baseline-footnote">
              <CheckCircle2 size={13} />存在任务集合不一致的基准，不作节省结论
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="window-answer-footer">
        <span>修什么、怎么排、审计证据都在方案详情里</span>
        <button
          type="button"
          onClick={() => onOpen(version.id)}
          aria-label={`查看方案 ${planNumber} 详情`}
        >
          查看完整方案
          <ArrowUpRight size={15} />
        </button>
      </div>
    </article>
  );
}
