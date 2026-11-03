import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { AgentPanel } from "../AgentPanel";

const createConversation = vi.fn().mockResolvedValue(undefined);
const selectConversation = vi.fn().mockResolvedValue(undefined);
const sendMessage = vi.fn().mockResolvedValue(undefined);

vi.mock("../../store/agent-store", () => ({
  useAgentStore: () => ({
    conversations: [
      {
        id: "conv_1",
        title: "新会话",
        createdAt: "2026-04-03T19:00:00+08:00",
        updatedAt: "2026-04-03T19:00:00+08:00",
        status: "active",
        lastMessagePreview: "你好",
        activeView: "P01",
        summary: "",
      },
    ],
    currentConversationId: "conv_1",
    messages: [
      {
        id: "user_1",
        conversationId: "conv_1",
        role: "user",
        content: "你好",
        createdAt: "2026-04-03T19:00:00+08:00",
      },
      {
        id: "assistant_1",
        conversationId: "conv_1",
        role: "assistant",
        content: "你好！有什么我可以帮你的吗？",
        createdAt: "2026-04-03T19:00:10+08:00",
      },
    ],
    loading: false,
    sending: false,
    createConversation,
    selectConversation,
    sendMessage,
  }),
}));

describe("AgentPanel", () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    createConversation.mockClear();
    selectConversation.mockClear();
    sendMessage.mockClear();
  });

  it("renders the figma-inspired assistant layout with compact icon actions", () => {
    render(<AgentPanel />);

    expect(screen.getByText("AI 窗口期分析助手")).toBeInTheDocument();
    expect(screen.getByText("智能窗口查询与分析")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "打开历史会话" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建会话" })).toBeInTheDocument();
    expect(screen.getByText("快捷指令:")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入您的问题...")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查询登塔窗口" })).toBeInTheDocument();
  });

  it("renders user messages with the figma primary bubble styling", () => {
    render(<AgentPanel />);

    const userBubble = screen
      .getByText("你好")
      .closest('[data-message-role="user"]');

    expect(userBubble).toBeInTheDocument();
    expect(userBubble).toHaveStyle({ backgroundColor: "rgb(21, 93, 252)" });
    expect(userBubble).toHaveStyle({ color: "rgb(255, 255, 255)" });
  });

  it("opens history as a side sheet overlay", () => {
    render(<AgentPanel />);

    fireEvent.click(screen.getByRole("button", { name: "打开历史会话" }));

    expect(screen.getByText("历史会话")).toBeInTheDocument();
    expect(screen.queryByText("查看并切换已有对话")).not.toBeInTheDocument();
    expect(screen.getByText("新会话")).toBeInTheDocument();
    expect(selectConversation).not.toHaveBeenCalled();
  });
});
