import type { CandidateWindow, WindowVersion } from "../../store/types";

export type SolverCode = "OPTIMAL" | "FEASIBLE" | "UNKNOWN" | "INFEASIBLE";

export interface SolverPresentation {
  code: SolverCode;
  label: string;
  shortLabel: string;
  tone: "success" | "info" | "warning" | "danger";
  description: string;
}

export interface BaselineComparison {
  name: string;
  value: number | null;
  label: string;
  tone: "save" | "increase" | "neutral";
  detail: string;
}

export function formatMoney(value: unknown) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "待确认";
  return `${(amount / 10000).toFixed(2)} 万元`;
}

export function formatCompactMoney(value: unknown) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "待确认";
  return `${(amount / 10000).toFixed(2)} 万`;
}

export function formatDateTime(value: unknown) {
  const text = String(value ?? "").trim();
  if (!text) return "待确认";
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!match) return text;
  return `${Number(match[2])}月${match[3]}日 ${match[4]}:${match[5]}`;
}

export function formatShortDate(value: unknown) {
  const text = String(value ?? "").trim();
  if (!text) return "待确认";
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return text;
  return `${Number(match[2])}月${match[3]}日`;
}

export function formatGeneratedAt(value: unknown) {
  const text = String(value ?? "").trim();
  if (!text) return "时间待确认";
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!match) return text;
  return `${match[2]}-${match[3]} ${match[4]}:${match[5]}`;
}

export function formatWindowRange(window: CandidateWindow | null | undefined) {
  if (!window) return "窗口待确认";
  return `${formatDateTime(window.metrics.startTime)} → ${formatDateTime(window.metrics.endTime)}`;
}

export function windowDurationDays(window: CandidateWindow | null | undefined) {
  if (!window) return null;
  const duration = Number(window.metrics.duration);
  if (!Number.isFinite(duration) || duration <= 0) return null;
  return Math.max(1, Math.round(duration / 24));
}

export function getSolverPresentation(summary: Record<string, unknown>): SolverPresentation {
  const solver = (summary.solver ?? {}) as Record<string, unknown>;
  const stages = Array.isArray(solver["阶段"])
    ? (solver["阶段"] as Array<Record<string, unknown>>)
    : [];
  const lastStage = stages.at(-1) ?? {};
  const explicit = String(
    solver["解状态"] ?? lastStage["状态"] ?? summary["solverStatus"] ?? "",
  ).toUpperCase();
  const hasSolution =
    Boolean(solver["有解"] ?? solver["分层全部最优"]) ||
    Number(summary.scheduledTaskCount ?? 0) > 0 ||
    Number(summary.windowCount ?? 0) > 0;
  const allOptimal = Boolean(solver["分层全部最优"]);
  // 求解器没给出排程、只回落到启发式基线时，方案来源会写成 HEURISTIC_FALLBACK。
  // 这种结果既不是求解器给出的可行解，也没有最优性证明，不能按“可行方案”展示。
  const heuristicFallback = String(solver["方案来源"] ?? "").toUpperCase() === "HEURISTIC_FALLBACK";
  const scheduled = Number(summary.scheduledTaskCount ?? 0);
  const totalTasks = Number(summary.totalTaskCount ?? 0);
  const coverage = totalTasks > 0 ? `仅排入 ${scheduled}/${totalTasks} 个任务` : "只覆盖部分任务";

  let code: SolverCode = "UNKNOWN";
  if (explicit === "OPTIMAL" || explicit === "FEASIBLE" || explicit === "INFEASIBLE") {
    code = explicit;
  } else if (allOptimal) {
    code = "OPTIMAL";
  } else if (hasSolution) {
    code = "FEASIBLE";
  }

  if (heuristicFallback) {
    // 来源与解状态各司其职，不能用一个字段替另一个背书：
    // 启发式基线不是 CP-SAT 解，不能挂 FEASIBLE 标签；除"已证明全量不可行"
    // 之外，状态保持 UNKNOWN（有方案、但求解器没有给出任何结论）。
    const infeasible = code === "INFEASIBLE";
    return {
      code: infeasible ? "INFEASIBLE" : "UNKNOWN",
      label: infeasible ? "全量不可行 · 启发式部分方案" : "启发式兜底方案 · 未证明最优",
      shortLabel: infeasible ? "部分方案" : "启发式兜底",
      tone: infeasible ? "danger" : "warning",
      description: infeasible
        ? `求解器已证明全量排程不可行，当前方案是启发式基线给出的部分排程，${coverage}，不代表最优。`
        : `求解器在时限内没有产出排程，当前方案由启发式基线生成，${coverage}；它不是求解器给出的可行解，也没有最优性证明。`,
    };
  }

  if (code === "OPTIMAL") {
    return {
      code,
      label: "已证明最优",
      shortLabel: "已证明最优",
      tone: "success",
      description: "所有已声明的分层目标均已满足，并通过求解器与独立复核。",
    };
  }
  if (code === "FEASIBLE") {
    return {
      code,
      label: "可行方案 · 未证明最优",
      shortLabel: "可行方案",
      tone: "info",
      description: "已找到可执行排程并通过复核，但尚未证明在全部目标上最优。",
    };
  }
  if (code === "INFEASIBLE") {
    return {
      code,
      label: "已证明不可行",
      shortLabel: "不可行",
      tone: "danger",
      description: "模型已被求解器证明在当前约束下不存在可行解。",
    };
  }
  return {
    code,
    label: "未找到方案 · 未证明无解",
    shortLabel: "待确认",
    tone: "warning",
    description: "当前搜索尚未取得结论，不代表模型无解，可延长求解时间继续。",
  };
}

export function getBaselineComparisons(version: WindowVersion): BaselineComparison[] {
  const summary = version.resultSummary ?? {};
  const savings = (summary.baselineSavings ?? {}) as Record<string, number | null>;
  return Object.entries(savings).map(([name, rawValue]) => {
    if (rawValue === null || rawValue === undefined || !Number.isFinite(Number(rawValue))) {
      return {
        name,
        value: null,
        label: "任务集合不同",
        tone: "neutral" as const,
        detail: "完成的任务集合不同，不作“节省”结论；可在方案详情中对比完成数。",
      };
    }
    const value = Number(rawValue);
    if (value === 0) {
      return {
        name,
        value,
        label: "成本持平",
        tone: "neutral" as const,
        detail: "完成任务集合一致，当前方案与该人工规则基准的总经济成本相同。",
      };
    }
    if (value >= 0) {
      return {
        name,
        value,
        label: `节省 ${formatMoney(value)}`,
        tone: "save" as const,
        detail: "在完成任务集合一致的前提下，相对人工规则基准的总经济成本差额。",
      };
    }
    return {
      name,
      value,
      label: `增加 ${formatMoney(Math.abs(value))}`,
      tone: "increase" as const,
      detail: "在完成任务集合一致的前提下，相对人工规则基准的总经济成本差额。",
    };
  });
}

export function getPlanningBoundary(version: WindowVersion) {
  const data = (version.resultSummary?.data ?? {}) as Record<string, unknown>;
  return String(data["规划边界"] ?? "").trim();
}

export function sortVersionsChronologically(versions: WindowVersion[]) {
  if (!Array.isArray(versions)) return [];
  return [...versions].sort((left, right) => {
    const delta = Date.parse(left.generatedAt) - Date.parse(right.generatedAt);
    return Number.isFinite(delta) && delta !== 0 ? delta : left.id.localeCompare(right.id);
  });
}
