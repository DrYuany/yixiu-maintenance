import { useMemo, useState } from "react";
import { AlertCircle, ArrowRight, CalendarDays, CheckCircle2, ChevronRight, Clock3, Coins, Info, SlidersHorizontal, Sparkles, TrendingDown, X } from "lucide-react";
import {
  alternativePlans,
  attentionItems,
  formatBaselineDelta,
  formatCurrency,
  getStrategyLabel,
  prototypePlans,
  type MaintenancePlan,
  type StrategyId,
} from "../product/prototype-data";

interface P01DashboardProps {
  onSelectPlan: (plan: MaintenancePlan) => void;
  onOpenAttention: (attentionId: string) => void;
}

const strategies: Array<{ id: StrategyId; description: string }> = [
  { id: "economic", description: "总经济成本最低" },
  { id: "risk", description: "高严重度故障尽早处理" },
  { id: "weighted", description: "成本、风险与覆盖率平衡" },
];

export function P01Dashboard({ onSelectPlan, onOpenAttention }: P01DashboardProps) {
  const [strategy, setStrategy] = useState<StrategyId>("economic");
  const [conditionOpen, setConditionOpen] = useState(false);
  const currentPlan = prototypePlans[strategy];
  const plans = useMemo(() => strategy === "economic" ? [currentPlan, ...alternativePlans] : [currentPlan], [currentPlan, strategy]);

  return (
    <div className="workbench-page">
      <div className="page-heading">
        <div><div className="title-line"><h1>检修工作台</h1><span className="demo-badge">演示数据</span></div><p>基于当前告警、海况、库存和人员约束生成，更新时间 2026-10-23 14:32</p></div>
        <button className="secondary-button" onClick={() => setConditionOpen(true)}><SlidersHorizontal size={16} />调整条件</button>
      </div>

      <section className="strategy-section" aria-label="推荐策略">
        <div className="section-caption"><span>选择推荐策略</span><small>切换策略将展示对应的最新计算结果</small></div>
        <div className="strategy-tabs">
          {strategies.map((item) => <button key={item.id} className={strategy === item.id ? "active" : ""} onClick={() => setStrategy(item.id)}><strong>{getStrategyLabel(item.id)}</strong><span>{item.description}</span></button>)}
        </div>
      </section>

      <section className="recommendation-section">
        <div className="section-title-row"><div><h2>推荐检修安排</h2><p>优先看出海时间和总经济成本，具体任务在详情中核查</p></div><span className="result-state"><CheckCircle2 size={15} />已完成约束复核</span></div>
        <div className="plan-grid">
          {plans.map((plan, index) => <PlanCard key={plan.id} plan={plan} rank={index + 1} onOpen={() => onSelectPlan(plan)} />)}
        </div>
        <div className="cost-note"><Info size={15} /><span>总经济成本包含船租、人工、备件及停机/降额发电损失；两项基准差异分别计算。</span></div>
      </section>

      <section className="attention-section">
        <div className="section-title-row"><div><h2>需要我关注</h2><p>只展示需要今天确认或即将影响检修安排的事项</p></div><button className="text-button">查看全部 <ArrowRight size={14} /></button></div>
        <div className="attention-list">
          {attentionItems.map((item) => <button key={item.id} className={`attention-row ${item.level}`} onClick={() => onOpenAttention(item.id)}><span className="attention-icon">{item.level === "critical" ? <AlertCircle size={18} /> : item.level === "warning" ? <Clock3 size={18} /> : <CalendarDays size={18} />}</span><span className="attention-copy"><strong>{item.label}</strong><small>{item.detail}</small></span><span className="attention-action">{item.action}<ChevronRight size={15} /></span></button>)}
        </div>
      </section>

      {conditionOpen ? <ConditionDrawer onClose={() => setConditionOpen(false)} /> : null}
    </div>
  );
}

function PlanCard({ plan, rank, onOpen }: { plan: MaintenancePlan; rank: number; onOpen: () => void }) {
  return (
    <article className={`plan-card ${rank === 1 ? "primary" : ""}`}>
      <div className="plan-card-head"><span className="rank-badge">{rank === 1 ? <><Sparkles size={14} />首选</> : `备选 ${rank}`}</span><span className="plan-id">{plan.id}</span></div>
      <div className="plan-time"><CalendarDays size={19} /><div><small>建议出海时间</small><strong>{plan.dateRange}</strong><span>{plan.departureLabel}</span></div></div>
      <div className="cost-hero"><small>预计总经济成本</small><strong>{formatCurrency(plan.totalEconomicCost)}</strong></div>
      <div className="baseline-grid">
        <div><span>较严重性优先基准</span><strong className={plan.severityBaselineDelta >= 0 ? "positive" : "negative"}><TrendingDown size={15} />{formatBaselineDelta(plan.severityBaselineDelta)}</strong></div>
        <div><span>较低风速优先基准</span><strong className={plan.lowWindBaselineDelta >= 0 ? "positive" : "negative"}><TrendingDown size={15} />{formatBaselineDelta(plan.lowWindBaselineDelta)}</strong></div>
      </div>
      <div className="plan-reason"><Coins size={16} /><span>{plan.primaryReason}</span></div>
      <button className="primary-button full" onClick={onOpen}>查看方案详情 <ChevronRight size={16} /></button>
    </article>
  );
}

function ConditionDrawer({ onClose }: { onClose: () => void }) {
  return <div className="drawer-layer"><button className="drawer-backdrop" onClick={onClose} aria-label="关闭条件抽屉" /><aside className="condition-drawer"><div className="drawer-title"><span>调整计算条件</span><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></div><div className="drawer-body"><div className="form-group"><label>规划周期</label><select defaultValue="5"><option value="5">未来 5 周</option><option value="3">未来 3 周</option></select></div><div className="form-group"><label>最大可用作业线</label><input defaultValue="3" /></div><div className="form-group"><label>现金预算上限</label><input defaultValue="3,500,000" /><small>不含发电机会损失</small></div><div className="form-group"><label>加权目标</label><div className="weight-row"><span>经济成本</span><strong>50%</strong></div><input type="range" defaultValue="50" /></div><div className="condition-tip"><Info size={16} />安全海况、人员资质与正式截止期属于硬约束，不能在此关闭。</div></div><div className="drawer-footer"><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" onClick={onClose}>应用并重新计算</button></div></aside></div>;
}
