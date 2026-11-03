import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import { AgentWorkspace } from "../AgentWorkspace";
import { ConversationCanvas } from "../ConversationCanvas";
import { SolveWaitDialog } from "../SolveWaitDialog";

const sendMessage = vi.fn().mockResolvedValue(undefined);

vi.mock("../../../store/agent-store", () => ({
  useAgentStore: () => ({
    conversations: [{ id: "conv-1", title: "五周检修计划", updatedAt: "2026-10-25T10:00:00+08:00", lastMessagePreview: "优先修高风险任务" }],
    currentConversationId: "conv-1",
    messages: [
      { id: "m1", conversationId: "conv-1", role: "user", content: "优先修高风险任务", createdAt: "2026-10-25T10:00:00+08:00" },
      { id: "m2", conversationId: "conv-1", role: "assistant", content: "排程完成，点击下方方案卡查看详情。", createdAt: "2026-10-25T10:00:10+08:00", versionIds: ["ver-1"] },
    ],
    loading: false,
    sending: false,
    error: null,
    versions: [{
      id: "ver-1", conversationId: "conv-1", sourceType: "user", generatedAt: "2026-10-25T10:00:00+08:00",
      userPrompt: "优先修高风险任务", focusTag: "高风险优先抢修", intentType: "generate_version",
      parameterSnapshot: {}, resultSummary: {
        totalCost: 3340938.5,
        scheduledTaskCount: 25,
        totalTaskCount: 25,
        vesselDays: 37,
        latestCompletion: "2026-11-11T18:00:00+08:00",
        baselineSavings: { "严重性优先": 125000, "低风速优先": null },
        solver: { "有解": true, "分层全部最优": true },
      },
      repairSchedule: [{
        taskId: "任务001", turbineId: 1, componentName: "叶片",
        startHour: 8, endHour: 16, durationHours: 8,
        startTime: "2026-11-02 08:00", endTime: "2026-11-02 16:00",
      }],
      windows: [{
        id: "cand-1", windowId: "窗口02", versionId: "ver-1", conversationId: "conv-1",
        generatedAt: "2026-10-25T10:00:00+08:00", focusTag: "高风险优先抢修",
        generatedFromPrompt: "优先修高风险任务", displayOrder: 1, isPinned: false, isDeleted: false,
        metrics: {
          duration: 240, opportunityCost: 100, directCost: 80, riskLevel: "high",
          startTime: "2026-11-02 00:00", endTime: "2026-11-12 00:00",
        },
      }],
      includedInCandidatePool: true,
      solveJobId: "job-1",
      modelFingerprint: "model-1234567890",
    }],
    activeJob: {
      id: "job-1", conversationId: "conv-1", status: "COMPLETED", solverStatus: "FEASIBLE",
      submittedAt: "2026-10-25T10:00:00+08:00", elapsedSeconds: 42, needsUserDecision: false,
      backgrounded: false, hasIncumbent: true, modelFingerprint: "model-1234567890",
      request: { strategy: "高风险优先抢修", mode: "分层", seconds: 300 }, result: { versionId: "ver-1" },
    },
    decisionArtifact: {
      contextFingerprint: "context-1234567890", decisionFingerprint: "decision-1234567890",
      modelFingerprint: "model-1234567890", canonicalContext: {}, canonicalDecision: {}, canonicalModel: {},
    },
    solveEvidence: {
      status: "FEASIBLE", hasIncumbent: true, proof: false, elapsedSeconds: 42,
      phase: "completed", running: false, modelFingerprint: "model-1234567890",
    },
    createConversation: vi.fn(), selectConversation: vi.fn(), sendMessage, controlActiveJob: vi.fn(),
  }),
}));

describe("AgentWorkspace", () => {
  it("renders sessions, conversation, and decision evidence as three product regions", () => {
    render(<AgentWorkspace />);
    expect(screen.getByRole("complementary", { name: "会话列表" })).toBeInTheDocument();
    expect(screen.getByRole("main", { name: "Agent 对话" })).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "方案记录" })).toBeInTheDocument();
    expect(screen.getAllByText("高风险优先抢修").length).toBeGreaterThan(0);
    const conversation = screen.getByRole("main", { name: "Agent 对话" });
    expect(within(conversation).getByText("已证明最优")).toBeInTheDocument();
    expect(within(conversation).getByText("334.09 万元")).toBeInTheDocument();
    expect(within(conversation).getByRole("article", { name: "检修排程结果，方案 1" })).toBeInTheDocument();
    expect(within(conversation).getByText("建议检修窗口")).toBeInTheDocument();
    expect(within(conversation).getByText(/存在任务集合不一致的基准，不作节省结论/)).toBeInTheDocument();
  });

  it("opens a saved plan detail from the history panel", () => {
    render(<AgentWorkspace />);
    const history = screen.getByRole("complementary", { name: "方案记录" });
    fireEvent.click(within(history).getByRole("button", { name: "查看方案 1 详情" }));

    expect(screen.getByRole("dialog", { name: "方案 1 详情" })).toBeInTheDocument();
    expect(screen.getByText("这段时间做哪些、花多少钱")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "检修排程" }));
    expect(screen.getByText("任务001")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭方案详情" }));
    expect(screen.queryByRole("dialog", { name: "方案 1 详情" })).not.toBeInTheDocument();
  });

  it("submits a natural-language planning request", () => {
    render(<AgentWorkspace />);
    fireEvent.change(screen.getByPlaceholderText("描述目标、约束或要调整的方案…"), { target: { value: "每天最多维修 8 小时" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(sendMessage).toHaveBeenCalledWith("每天最多维修 8 小时", "ver-1");
  });
});

describe("ConversationCanvas submission guard", () => {
  it("does not submit twice before the first request settles", () => {
    const pendingSend = vi.fn(() => new Promise<void>(() => undefined));
    render(<ConversationCanvas messages={[]} sending={false} onSend={pendingSend} />);
    const input = screen.getByPlaceholderText("描述目标、约束或要调整的方案…");
    fireEvent.change(input, { target: { value: "按高风险优先重新排程" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(pendingSend).toHaveBeenCalledTimes(1);
  });

  it("keeps every result card in message order instead of replacing the latest one", () => {
    const firstVersion = {
      id: "ver-1", conversationId: "conv-1", sourceType: "user" as const, generatedAt: "2026-10-25T10:00:10+08:00",
      userPrompt: "第一次排程", focusTag: "高风险优先", intentType: "generate_version", parameterSnapshot: {},
      resultSummary: { totalCost: 1000000, scheduledTaskCount: 1, totalTaskCount: 2, windowCount: 1 },
      repairSchedule: [], windows: [], includedInCandidatePool: true,
    };
    const secondVersion = {
      ...firstVersion,
      id: "ver-2", generatedAt: "2026-10-25T10:02:10+08:00", userPrompt: "船租上涨 20%",
      focusTag: "成本优化", resultSummary: { totalCost: 1200000, scheduledTaskCount: 1, totalTaskCount: 2, windowCount: 1 },
    };
    render(
      <ConversationCanvas
        messages={[
          { id: "m1", conversationId: "conv-1", role: "assistant", content: "第一版", createdAt: firstVersion.generatedAt, versionIds: ["ver-1"] },
          { id: "m2", conversationId: "conv-1", role: "user", content: "船租上涨 20%", createdAt: "2026-10-25T10:02:00+08:00" },
          { id: "m3", conversationId: "conv-1", role: "assistant", content: "第二版", createdAt: secondVersion.generatedAt, versionIds: ["ver-2"] },
        ]}
        versions={[firstVersion, secondVersion]}
        sending={false}
        onSend={vi.fn()}
        onOpenVersion={vi.fn()}
      />,
    );

    expect(screen.getByRole("article", { name: "检修排程结果，方案 1" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "检修排程结果，方案 2" })).toBeInTheDocument();
    expect(screen.getByText("第一版")).toBeInTheDocument();
    expect(screen.getByText("第二版")).toBeInTheDocument();
  });

  it("shows the bound model context and confirms a pending change", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const version = {
      id: "ver-1", conversationId: "conv-1", sourceType: "user" as const, generatedAt: "2026-10-25T10:00:10+08:00",
      userPrompt: "第一次排程", focusTag: "高风险优先", intentType: "generate_version", parameterSnapshot: {},
      resultSummary: { totalCost: 1000000, scheduledTaskCount: 1, totalTaskCount: 2, windowCount: 1 },
      repairSchedule: [], windows: [], includedInCandidatePool: true,
    };
    render(
      <ConversationCanvas
        messages={[]}
        versions={[version]}
        sending={false}
        contextVersion={version}
        pendingChange={{
          baseVersionId: "ver-1",
          request: {},
          operations: [{ kind: "relative_parameter", key: "船租", before: null, after: 1.2 }],
          preview: "变更预览",
          sourcePrompt: "船租上涨20%",
        }}
        onSend={send}
        onOpenVersion={vi.fn()}
      />,
    );

    expect(screen.getByText("当前模型上下文")).toBeInTheDocument();
    expect(screen.getByText("待确认模型变更")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认生成沙盘" }));
    expect(send).toHaveBeenCalledWith("确认生成沙盘");
  });
});

describe("SolveWaitDialog", () => {
  it("offers long-running choices without claiming infeasibility", () => {
    render(<SolveWaitDialog open elapsedSeconds={181} hasIncumbent objectiveValue={3048435.77} bestBound={2920000} onAction={vi.fn()} />);
    expect(screen.getByText("求解仍在进行")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续等待" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "后台运行" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "使用当前方案" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止求解" })).toBeInTheDocument();
    expect(screen.queryByText(/^无解$/)).not.toBeInTheDocument();
  });
});
