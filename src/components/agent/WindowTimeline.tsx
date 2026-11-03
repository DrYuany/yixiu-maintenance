import { useMemo } from "react";
import { Anchor, Moon, Sun, Wrench } from "lucide-react";
import type { CandidateWindow, RepairScheduleItem } from "../../store/types";
import { formatDateTime } from "./plan-format";

const HOUR_MS = 3_600_000;
const WEEK_LABELS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

interface Props {
  window: CandidateWindow | null;
  schedule: RepairScheduleItem[];
}

/** 按本地时区输出 "M月D日 HH:mm"，避免 toISOString 的 UTC 偏移。 */
function localLabel(ms: number) {
  const date = new Date(ms);
  return `${date.getMonth() + 1}月${date.getDate()}日 ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function hourLabel(hours: number) {
  if (!Number.isFinite(hours) || hours <= 0) return "0 小时";
  if (hours < 24) return `${Math.round(hours * 10) / 10} 小时`;
  const days = Math.floor(hours / 24);
  const rest = Math.round(hours - days * 24);
  return rest ? `${days} 天 ${rest} 小时` : `${days} 天`;
}

function shiftLabel(hours: number) {
  if (!Number.isFinite(hours) || hours <= 0) return "0 小时";
  return hourLabel(hours);
}

export function WindowTimeline({ window, schedule }: Props) {
  const model = useMemo(() => {
    if (!window) return null;
    const domainStart = Date.parse(window.metrics.startTime);
    const domainEnd = Date.parse(window.metrics.endTime);
    if (!Number.isFinite(domainStart) || !Number.isFinite(domainEnd) || domainEnd <= domainStart) {
      return null;
    }
    const span = domainEnd - domainStart;
    const pct = (ms: number) => (ms / span) * 100;

    const days: Array<{ left: number; label: string; week: string }> = [];
    const firstDay = new Date(domainStart);
    firstDay.setHours(0, 0, 0, 0);
    const cursorEnd = domainEnd + 24 * HOUR_MS;
    for (let cursor = firstDay.getTime(); cursor < cursorEnd; cursor += 24 * HOUR_MS) {
      const date = new Date(cursor);
      days.push({
        left: Math.min(100, Math.max(0, pct(cursor - domainStart))),
        label: `${date.getMonth() + 1}/${date.getDate()}`,
        week: WEEK_LABELS[date.getDay()],
      });
    }

    const segments = schedule
      .map((task) => ({
        start: Date.parse(task.startTime),
        end: Date.parse(task.endTime),
      }))
      .filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start)
      .map((item) => ({
        start: Math.min(Math.max(item.start, domainStart), domainEnd),
        end: Math.min(Math.max(item.end, domainStart), domainEnd),
      }))
      .filter((item) => item.end > item.start)
      .sort((left, right) => left.start - right.start);
    // 合并重叠与相接的区段，排程条只保留“开工→收工”内真实干活的时间块。
    const merged: Array<{ start: number; end: number }> = [];
    segments.forEach((item) => {
      const last = merged.at(-1);
      if (last && item.start <= last.end) {
        last.end = Math.max(last.end, item.end);
      } else {
        merged.push({ ...item });
      }
    });

    const allStarts = schedule
      .map((task) => Date.parse(task.startTime))
      .filter((value) => Number.isFinite(value));
    const allEnds = schedule
      .map((task) => Date.parse(task.endTime))
      .filter((value) => Number.isFinite(value));
    const hasSchedule = allStarts.length > 0 && allEnds.length > 0;

    const scheduleStart = hasSchedule ? Math.max(Math.min(...allStarts), domainStart) : null;
    const scheduleEnd = hasSchedule ? Math.min(Math.max(...allEnds), domainEnd) : null;

    const stats =
      scheduleStart !== null && scheduleEnd !== null && scheduleEnd > scheduleStart
        ? {
            start: scheduleStart,
            end: scheduleEnd,
            usedHours: (scheduleEnd - scheduleStart) / HOUR_MS,
            idleBeforeHours: (scheduleStart - domainStart) / HOUR_MS,
            idleAfterHours: (domainEnd - scheduleEnd) / HOUR_MS,
          }
        : null;

    return {
      domainStart,
      domainEnd,
      spanHours: span / HOUR_MS,
      days,
      segments: merged.map((item) => ({
        left: pct(item.start - domainStart),
        width: Math.max(pct(item.end - item.start), 0.6),
        title: `${localLabel(item.start)} → ${localLabel(item.end)}`,
      })),
      scheduleBar: stats
        ? { left: pct(stats.start - domainStart), width: Math.max(pct(stats.end - stats.start), 0.8) }
        : null,
      stats,
    };
  }, [schedule, window]);

  if (!window || !model) {
    return <div className="wt-shell wt-empty">窗口时间数据不完整，暂不绘制时间轴。</div>;
  }

  const { days, segments, scheduleBar, stats, spanHours } = model;

  return (
    <section className="wt-shell" aria-label="海事可作业窗口与实际排程时间轴">
      <header className="wt-head">
        <div className="wt-head-main">
          <span className="wt-eyebrow">
            <Anchor size={13} />
            建议检修窗口
          </span>
          <strong>{window.windowId}</strong>
          <span className="wt-range">
            {formatDateTime(window.metrics.startTime)} → {formatDateTime(window.metrics.endTime)}
          </span>
        </div>
        <div className="wt-head-stats">
          <span className="wt-chip window">海事批准 · 可作业 {hourLabel(spanHours)}</span>
          {stats ? (
            <span className="wt-chip schedule">
              <Wrench size={12} />
              实际排程 {hourLabel(stats.usedHours)}
            </span>
          ) : null}
        </div>
      </header>

      <div className="wt-axis" aria-hidden="true">
        <span className="wt-axis-gutter" />
        <div className="wt-axis-track">
          {days.map((day) => (
            <span
              className={`wt-axis-day${day.left >= 99 ? " end" : ""}`}
              key={`${day.label}-${day.left}`}
              style={{ left: `${day.left}%` }}
            >
              <b>{day.label}</b>
              <small>{day.week}</small>
            </span>
          ))}
        </div>
      </div>

      <div className="wt-body">
        <div className="wt-row">
          <span className="wt-row-label">
            <Anchor size={13} />
            <b>海事</b>
          </span>
          <div className="wt-row-track">
            {days.map((day) => (
              <i className="wt-grid-line" key={`g-${day.left}`} style={{ left: `${day.left}%` }} />
            ))}
            <div
              className="wt-window-bar"
              title={`海事批准可作业：${formatDateTime(window.metrics.startTime)} → ${formatDateTime(window.metrics.endTime)}，共 ${hourLabel(spanHours)}`}
            >
              <span>海事批准，整个窗口都可作业</span>
            </div>
          </div>
        </div>

        <div className="wt-row">
          <span className="wt-row-label">
            <Wrench size={12} />
            <b>排程</b>
          </span>
          <div className="wt-row-track">
            {days.map((day) => (
              <i className="wt-grid-line" key={`t-${day.left}`} style={{ left: `${day.left}%` }} />
            ))}
            {segments.map((segment, index) => (
              <div
                className="wt-task"
                key={`seg-${index}`}
                title={segment.title}
                style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
              />
            ))}
            {scheduleBar ? (
              <div
                className="wt-span-bracket"
                style={{ left: `${scheduleBar.left}%`, width: `${scheduleBar.width}%` }}
                aria-hidden="true"
              />
            ) : null}
          </div>
        </div>
      </div>

      <footer className="wt-foot">
        {stats ? (
          <>
            <span className="wt-foot-item">
              <Sun size={13} />
              开工比窗口晚 {shiftLabel(stats.idleBeforeHours)}
            </span>
            <span className="wt-foot-item">
              <Moon size={13} />
              收工比窗口早 {shiftLabel(stats.idleAfterHours)}
            </span>
            <span className="wt-foot-item strong">
              实际占用 {localLabel(stats.start)} → {localLabel(stats.end)}（{hourLabel(stats.usedHours)}）
            </span>
          </>
        ) : (
          <span className="wt-foot-item">本方案没有任务级排程，无法标注实际占用时段。</span>
        )}
      </footer>

      <div className="wt-legend" aria-hidden="true">
        <span><i className="dot window" />海事可作业</span>
        <span><i className="dot task" />实际排程</span>
        <span><i className="dot idle" />未排空档</span>
      </div>
    </section>
  );
}
