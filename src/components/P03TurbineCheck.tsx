import { useState } from "react";
import { ArrowLeft, Check, CheckCircle2, Circle, ClipboardCheck, FileCheck2, PackageCheck, Ship, UserCheck } from "lucide-react";
import { formatCurrency, type MaintenancePlan } from "../product/prototype-data";

interface Props { selectedPlan: MaintenancePlan; onBack: () => void; }

const steps = ["草稿", "内部审核", "出海准备", "执行", "完工核查", "已归档"];

export function P03TurbineCheck({ selectedPlan, onBack }: Props) {
  const [stage, setStage] = useState(0);
  const advance = () => setStage((value) => Math.min(value + 1, steps.length - 1));
  return <div className="detail-page">
    <div className="breadcrumb-row"><button onClick={onBack}><ArrowLeft size={16} />返回方案详情</button><span>/</span><span>检修单 WO-20261023-01</span></div>
    <div className="detail-heading"><div><div className="title-line"><h1>检修单 WO-20261023-01</h1><span className={`status-tag ${stage > 1 ? "success" : "processing"}`}>{steps[stage]}</span></div><p>关联方案 {selectedPlan.id} · 负责人 张海峰</p></div><button className="primary-button" onClick={advance} disabled={stage === steps.length - 1}>{stage === 0 ? "提交内部审核" : stage === 1 ? "审核通过" : stage === 2 ? "确认准备完成" : stage === 3 ? "提交完工反馈" : stage === 4 ? "核查并归档" : "已完成"}</button></div>

    <section className="order-summary"><div><span>计划出海</span><strong>{selectedPlan.dateRange}</strong></div><div><span>任务</span><strong>{selectedPlan.taskCount} 项 / {selectedPlan.turbineCount} 台</strong></div><div><span>预计总成本</span><strong>{formatCurrency(selectedPlan.totalEconomicCost)}</strong></div><div><span>创建时间</span><strong>10月23日 14:46</strong></div></section>

    <section className="content-card"><div className="process-steps">{steps.map((item, index) => <button key={item} className={`${index === stage ? "active" : ""} ${index < stage ? "complete" : ""}`} onClick={() => index <= stage && setStage(index)}><span>{index < stage ? <Check size={15} /> : index + 1}</span><strong>{item}</strong></button>)}</div></section>

    <section className="status-overview"><StatusTile icon={<UserCheck />} label="内部审核" value={stage >= 2 ? "已通过" : stage === 1 ? "审核中" : "待提交"} tone={stage >= 2 ? "success" : "processing"} /><StatusTile icon={<FileCheck2 />} label="海事许可" value={stage >= 2 ? "申请中" : "未提交"} tone={stage >= 3 ? "success" : "neutral"} /><StatusTile icon={<PackageCheck />} label="库存预留" value={stage >= 2 ? "已预留 36 件" : "待批准后预留"} tone={stage >= 2 ? "success" : "neutral"} /><StatusTile icon={<Ship />} label="实际领料" value={stage >= 3 ? "已领用 34 件" : "尚未领料"} tone={stage >= 3 ? "success" : "neutral"} /></section>

    <section className="content-card stage-workspace"><div className="card-heading"><div><h2>{steps[stage]}工作区</h2><p>{getStageDescription(stage)}</p></div></div>{stage === 0 && <DraftWorkspace />}{stage === 1 && <ReviewWorkspace />}{stage === 2 && <PreparationWorkspace />}{stage >= 3 && <ExecutionWorkspace stage={stage} />}</section>
  </div>;
}

function getStageDescription(stage: number) { return ["核对自动带入的任务、人员、物资、时间与费用后提交审核。","审核方案合理性、风险和资源准备情况。","逐项确认人员、船舶、许可和物资状态。","记录实际执行、领料和现场异常。","核对完工反馈、实际费用与物资退回。","查看最终归档材料和计划执行偏差。"][stage]; }
function DraftWorkspace() { return <div className="checklist"><CheckRow text="25 项任务均有明确开完工时间" done /><CheckRow text="18 名人员资质与班组安排已核验" done /><CheckRow text="2 套变桨轴承包预计 10月24日到货" warning /><CheckRow text="海事许可材料待审核通过后提交" /></div>; }
function ReviewWorkspace() { return <div className="review-layout"><div className="checklist"><CheckRow text="高风险任务均在正式截止期前完成" done /><CheckRow text="人员与船舶无时间冲突" done /><CheckRow text="库存计划预留量不超过可用库存" done /></div><label className="review-note">审核意见<textarea placeholder="填写审核意见，退回时必填" /></label></div>; }
function PreparationWorkspace() { return <div className="checklist"><CheckRow text="检修人员到岗确认" done /><CheckRow text="海工运维 01 / 03 船舶确认" done /><CheckRow text="36 件备件完成计划预留" done /><CheckRow text="海事许可审批结果" warning /></div>; }
function ExecutionWorkspace({ stage }: { stage: number }) { return <div className="empty-panel"><CheckCircle2 size={28} /><strong>{stage === 5 ? "检修单已完成归档" : "等待现场反馈"}</strong><span>{stage === 5 ? "计划、审批、库存流水和完工材料已形成完整记录。" : "本原型演示后续阶段的状态流转，实际反馈接口将在现场系统接入后补齐。"}</span></div>; }
function CheckRow({ text, done, warning }: { text: string; done?: boolean; warning?: boolean }) { return <div className={`check-row ${warning ? "warning" : ""}`}>{done ? <CheckCircle2 size={19} /> : <Circle size={19} />}<span>{text}</span>{warning ? <em>需关注</em> : done ? <em>已核验</em> : <em>待处理</em>}</div>; }
function StatusTile({ icon, label, value, tone }: { icon: React.ReactNode; label: string; value: string; tone: string }) { return <div className="status-tile"><span className={tone}>{icon}</span><div><small>{label}</small><strong>{value}</strong></div></div>; }
