import type { Conversation, DecisionArtifact, Message, SolveEvidence } from "./types";

export function isStandaloneFileProtocol(protocol: string) {
  return protocol === "file:";
}

export function createOfflineDemo(): {
  conversations: Conversation[];
  messages: Message[];
  decisionArtifact: DecisionArtifact;
  solveEvidence: SolveEvidence;
} {
  const updatedAt = "2026-10-25T14:32:00+08:00";
  const conversations: Conversation[] = [
    { id: "demo-1", title: "五周检修计划", createdAt: updatedAt, updatedAt, status: "active", lastMessagePreview: "高风险优先，船租上涨 20%", activeView: "P01", summary: "审计模式演示" },
    { id: "demo-2", title: "11 月窗口对比", createdAt: updatedAt, updatedAt, status: "idle", lastMessagePreview: "比较经济与风险策略", activeView: "P01", summary: "" },
    { id: "demo-3", title: "台风后抢修方案", createdAt: updatedAt, updatedAt, status: "idle", lastMessagePreview: "最多两条作业线", activeView: "P01", summary: "" },
  ];
  const messages: Message[] = [
    { id: "demo-m1", conversationId: "demo-1", role: "user", content: "优先修高风险任务，船租上涨 20%，每天最多作业 8 小时。", createdAt: updatedAt },
    { id: "demo-m2", conversationId: "demo-1", role: "assistant", content: "已将高风险任务完成数设为第一目标，总经济成本设为第二目标；每天作业上限锁定为 8 小时。当前已找到可行方案，但尚未证明最优。", createdAt: updatedAt },
  ];
  const decisionArtifact: DecisionArtifact = {
    canonicalContext: { data: "data-v3", mode: "audit" },
    canonicalDecision: { objectives: ["high_risk_completion", "total_cost"], daily_hours: 8 },
    canonicalModel: { solver: "CP-SAT", workers: 1 },
    contextFingerprint: "ctx-8fd319ca1210",
    decisionFingerprint: "decision-b9e26e4122",
    modelFingerprint: "model-7e4079449d",
  };
  const solveEvidence: SolveEvidence = {
    status: "FEASIBLE",
    hasIncumbent: true,
    proof: false,
    elapsedSeconds: 82,
    phase: "solve",
    running: false,
    objectiveValue: 2929063.67,
    bestBound: 2790458.57,
    modelFingerprint: decisionArtifact.modelFingerprint,
  };
  return { conversations, messages, decisionArtifact, solveEvidence };
}
