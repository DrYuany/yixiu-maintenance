export type StrategyId = "economic" | "risk" | "weighted";

export interface MaintenancePlan {
  id: string;
  strategy: StrategyId;
  title: string;
  dateRange: string;
  departureLabel: string;
  totalEconomicCost: number;
  severityBaselineDelta: number;
  lowWindBaselineDelta: number;
  taskCount: number;
  turbineCount: number;
  windowCount: number;
  primaryReason: string;
  status: "recommended" | "alternative";
}

export const strategyLabels: Record<StrategyId, string> = {
  economic: "经济性优先",
  risk: "高风险优先",
  weighted: "加权最优",
};

export const prototypePlans: Record<StrategyId, MaintenancePlan> = {
  economic: {
    id: "PLAN-20261023-ECO",
    strategy: "economic",
    title: "推荐安排 A",
    dateRange: "10月26日 06:00 — 10月28日 18:00",
    departureLabel: "3 天 · 2 个可作业窗口",
    totalEconomicCost: 2821043.19,
    severityBaselineDelta: 186000,
    lowWindBaselineDelta: 98000,
    taskCount: 25,
    turbineCount: 12,
    windowCount: 2,
    primaryReason: "减少船舶等待并避开发电高价值时段",
    status: "recommended",
  },
  risk: {
    id: "PLAN-20261023-RISK",
    strategy: "risk",
    title: "高风险抢修安排",
    dateRange: "10月24日 07:30 — 10月27日 17:00",
    departureLabel: "4 天 · 3 个可作业窗口",
    totalEconomicCost: 3168000,
    severityBaselineDelta: 42000,
    lowWindBaselineDelta: -121000,
    taskCount: 23,
    turbineCount: 11,
    windowCount: 3,
    primaryReason: "优先处理 4 项高严重度故障，最早提前 41 小时",
    status: "recommended",
  },
  weighted: {
    id: "PLAN-20261023-WEIGHTED",
    strategy: "weighted",
    title: "综合平衡安排",
    dateRange: "10月25日 06:30 — 10月28日 16:30",
    departureLabel: "4 天 · 2 个可作业窗口",
    totalEconomicCost: 2956000,
    severityBaselineDelta: 112000,
    lowWindBaselineDelta: 36000,
    taskCount: 25,
    turbineCount: 12,
    windowCount: 2,
    primaryReason: "兼顾严重度、成本和任务覆盖率",
    status: "recommended",
  },
};

export const alternativePlans: MaintenancePlan[] = [
  {
    ...prototypePlans.economic,
    id: "PLAN-20261023-ECO-B",
    title: "备选安排 B",
    dateRange: "10月29日 05:30 — 10月31日 17:30",
    departureLabel: "3 天 · 2 个可作业窗口",
    totalEconomicCost: 3012000,
    severityBaselineDelta: 95000,
    lowWindBaselineDelta: 17000,
    taskCount: 24,
    turbineCount: 11,
    primaryReason: "海况更稳，但一项高风险任务需延后 3 天",
    status: "alternative",
  },
];

export const attentionItems = [
  { id: "ATT-1", level: "critical", label: "新增 1 项高风险告警", detail: "F-18 主轴温升异常，建议今天确认是否纳入计划", action: "查看告警" },
  { id: "ATT-2", level: "warning", label: "明日出海物资尚未备齐", detail: "WO-20261024-03 仍缺 2 套变桨轴承包", action: "处理备料" },
  { id: "ATT-3", level: "info", label: "2 张检修单待审核", detail: "最晚需在今天 17:00 前完成内部审核", action: "去审核" },
] as const;

export function getStrategyLabel(strategy: StrategyId) {
  return strategyLabels[strategy];
}

export function formatCurrency(value: number) {
  return `¥${(value / 10000).toFixed(1)}万`;
}

export function formatBaselineDelta(value: number) {
  const prefix = value >= 0 ? "少支出" : "多支出";
  return `${prefix} ${formatCurrency(Math.abs(value))}`;
}
