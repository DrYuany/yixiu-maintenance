import { useState } from "react";
import { ArrowLeft, Boxes, CalendarDays, Check, ChevronDown, ChevronRight, ClipboardCheck, Coins, Download, PackageCheck, Ship, Users } from "lucide-react";
import { formatBaselineDelta, formatCurrency, type MaintenancePlan } from "../product/prototype-data";

interface Props { selectedPlan: MaintenancePlan; onBack: () => void; onCreateWorkOrder: () => void; }
type DetailTab = "tasks" | "resources" | "spares" | "cost" | "unscheduled";

const tabs: Array<{ id: DetailTab; label: string }> = [
  { id: "tasks", label: "检修任务" }, { id: "resources", label: "人员与船舶" }, { id: "spares", label: "备件准备" }, { id: "cost", label: "成本与对比" }, { id: "unscheduled", label: "未安排任务" },
];

export function P02VoyageAnalysis({ selectedPlan, onBack, onCreateWorkOrder }: Props) {
  const [tab, setTab] = useState<DetailTab>("tasks");
  const [basisOpen, setBasisOpen] = useState(false);
  return <div className="detail-page">
    <div className="breadcrumb-row"><button onClick={onBack}><ArrowLeft size={16} />返回工作台</button><span>/</span><span>推荐方案详情</span></div>
    <div className="detail-heading"><div><div className="title-line"><h1>{selectedPlan.title}</h1><span className="status-tag success">推荐</span><span className="demo-badge">演示数据</span></div><p>{selectedPlan.id} · 生成于 2026-10-23 14:32</p></div><div className="heading-actions"><button className="secondary-button"><Download size={16} />导出方案</button><button className="primary-button" onClick={onCreateWorkOrder}><ClipboardCheck size={16} />选用并生成检修单</button></div></div>

    <section className="decision-summary">
      <div className="summary-main"><span>建议出海时间</span><strong>{selectedPlan.dateRange}</strong><small>{selectedPlan.departureLabel}</small></div>
      <div className="summary-metric"><span>预计总经济成本</span><strong>{formatCurrency(selectedPlan.totalEconomicCost)}</strong></div>
      <div className="summary-metric"><span>较严重性优先</span><strong className={selectedPlan.severityBaselineDelta >= 0 ? "positive" : "negative"}>{formatBaselineDelta(selectedPlan.severityBaselineDelta)}</strong></div>
      <div className="summary-metric"><span>较低风速优先</span><strong className={selectedPlan.lowWindBaselineDelta >= 0 ? "positive" : "negative"}>{formatBaselineDelta(selectedPlan.lowWindBaselineDelta)}</strong></div>
    </section>

    <section className="content-card"><div className="card-heading"><div><h2>出海安排</h2><p>同一方案中的窗口共同完成本次检修计划</p></div><span className="result-state"><Check size={14} />25 项任务均已排程</span></div><div className="voyage-timeline"><div className="voyage-item active"><span className="timeline-dot">1</span><div><strong>窗口 01 · 10月26日 06:00—18:00</strong><small>东区作业 · 7 台风机 · 14 项任务</small></div><div className="voyage-stats"><span>船舶 2 艘</span><strong>¥156.8万</strong></div></div><div className="timeline-connector" /><div className="voyage-item"><span className="timeline-dot">2</span><div><strong>窗口 02 · 10月27日 07:00—10月28日 18:00</strong><small>中西区作业 · 5 台风机 · 11 项任务</small></div><div className="voyage-stats"><span>船舶 2 艘</span><strong>¥125.3万</strong></div></div></div></section>

    <section className="content-card detail-tabs-card"><div className="detail-tabs">{tabs.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>{item.label}{item.id === "unscheduled" ? <em>0</em> : null}</button>)}</div><div className="tab-panel">{tab === "tasks" && <TaskTable />}{tab === "resources" && <ResourcePanel />}{tab === "spares" && <SparesPanel />}{tab === "cost" && <CostPanel plan={selectedPlan} />}{tab === "unscheduled" && <EmptyPanel />}</div></section>

    <section className="collapsible-card"><button onClick={() => setBasisOpen(!basisOpen)}><span><strong>计算依据与版本</strong><small>目标、硬约束、数据快照和版本记录</small></span><ChevronDown size={18} className={basisOpen ? "rotated" : ""} /></button>{basisOpen ? <div className="basis-grid"><div><span>优化目标</span><strong>总经济成本最小</strong></div><div><span>求解状态</span><strong>主目标已证明最优</strong></div><div><span>数据快照</span><strong>2026-10-23 14:32</strong></div><div><span>方案版本</span><strong>V6 · 当前版本</strong></div></div> : null}</section>
  </div>;
}

function TaskTable() { const rows = [["F-03","齿轮箱油压异常","高","窗口01 · 08:00—11:30","已排程"],["F-18","主轴温升异常","高","窗口01 · 07:00—12:00","已排程"],["F-09","变桨系统通讯故障","中","窗口01 · 13:00—16:30","已排程"],["F-22","偏航制动器磨损","中","窗口02 · 09:00—13:00","已排程"],["F-27","机舱冷却系统告警","低","窗口02 · 14:00—16:00","已排程"]]; return <div className="table-wrap"><table className="data-table"><thead><tr><th>风机</th><th>故障任务</th><th>严重度</th><th>计划时间</th><th>状态</th><th /></tr></thead><tbody>{rows.map((row) => <tr key={row[0]}>{row.map((cell, index) => <td key={index}>{index === 2 ? <span className={`severity ${cell === "高" ? "high" : cell === "中" ? "medium" : "low"}`}>{cell}</span> : index === 4 ? <span className="status-tag success">{cell}</span> : cell}</td>)}<td><button className="icon-button"><ChevronRight size={16} /></button></td></tr>)}</tbody></table></div>; }
function ResourcePanel() { return <div className="info-grid"><InfoTile icon={<Users />} title="检修人员" value="18 人" note="3 个班组，资质均满足" /><InfoTile icon={<Ship />} title="作业船舶" value="2 艘" note="海工运维 01 / 03" /><InfoTile icon={<CalendarDays />} title="预计占用" value="3 个自然日" note="含等待与转场" /></div>; }
function SparesPanel() { return <div className="info-grid"><InfoTile icon={<PackageCheck />} title="备件需求" value="12 类 / 38 件" note="已确认可用 36 件" /><InfoTile icon={<Boxes />} title="计划预留" value="36 件" note="批准后执行预留" /><InfoTile icon={<PackageCheck />} title="待补齐" value="2 套" note="预计 10月24日到货" /></div>; }
function CostPanel({ plan }: { plan: MaintenancePlan }) { return <div className="cost-breakdown"><div><span>船舶租赁</span><strong>¥72.0万</strong><em>25.5%</em></div><div><span>维修人工</span><strong>¥18.6万</strong><em>6.6%</em></div><div><span>备件成本</span><strong>¥96.5万</strong><em>34.2%</em></div><div><span>发电机会损失</span><strong>¥95.0万</strong><em>33.7%</em></div><div className="total"><span>总经济成本</span><strong>{formatCurrency(plan.totalEconomicCost)}</strong><em>100%</em></div></div>; }
function EmptyPanel() { return <div className="empty-panel"><Check size={26} /><strong>当前没有未安排任务</strong><span>25 项已接收任务均已纳入本方案。</span></div>; }
function InfoTile({ icon, title, value, note }: { icon: React.ReactNode; title: string; value: string; note: string }) { return <div className="info-tile"><span>{icon}</span><div><small>{title}</small><strong>{value}</strong><em>{note}</em></div></div>; }
