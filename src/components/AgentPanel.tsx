import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  ChevronRight,
  History,
  Loader2,
  Plus,
  Send,
  User,
  X,
} from "lucide-react";
import { useAgentStore } from "../store/agent-store";

const QUICK_COMMANDS = [
  "查询登塔窗口",
  "推荐窗口",
  "窗口复盘",
  "使用帮助",
];

const WELCOME_MESSAGE =
  "您好！我是窗口期分析助手。我可以帮您查询公共窗口、分析航次约束、校验单机窗口、推荐配置参数。请问有什么可以帮您？";

function formatTime(value: string) {
  try {
    return new Date(value).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return value;
  }
}

export function AgentPanel() {
  const {
    conversations,
    currentConversationId,
    messages,
    loading,
    sending,
    degraded,
    createConversation,
    selectConversation,
    sendMessage,
  } = useAgentStore();

  const [input, setInput] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sending]);

  const visibleConversations = useMemo(
    () => conversations.slice(0, 8),
    [conversations],
  );

  const renderedMessages = useMemo(() => {
    if (messages.length > 0) return messages;

    return [
      {
        id: "assistant_welcome",
        conversationId: currentConversationId ?? "local",
        role: "assistant" as const,
        content: WELCOME_MESSAGE,
        createdAt: new Date().toISOString(),
      },
    ];
  }, [currentConversationId, messages]);

  const handleSend = async (value = input) => {
    const nextValue = value.trim();
    if (!nextValue || sending) return;
    setInput("");
    await sendMessage(nextValue);
  };

  return (
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-white text-[#101828]">
      <div className="relative flex h-[69px] items-center border-b border-[#e5e7eb] px-4">
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center text-[#155dfc]">
            <Bot className="h-4 w-4" strokeWidth={2.25} />
          </div>
          <div className="min-w-0">
            <div className="truncate text-[14px] leading-5 font-medium text-[#101828]">
              AI 窗口期分析助手
            </div>
            <div className="truncate text-[12px] leading-4 text-[#6a7282]">
              智能窗口查询与分析
            </div>
          </div>
        </div>

        <div className="absolute right-4 top-1/2 flex -translate-y-1/2 items-center gap-1.5">
          <button
            type="button"
            onClick={() => setShowHistory(true)}
            aria-label="打开历史会话"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-[#e5e7eb] bg-white text-[#6a7282] shadow-[0_1px_2px_rgba(16,24,40,0.06)] transition-all hover:border-[#cbd5e1] hover:bg-[#f8fafc] hover:text-[#101828]"
          >
            <History className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => void createConversation()}
            aria-label="新建会话"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-[#e5e7eb] bg-white text-[#6a7282] shadow-[0_1px_2px_rgba(16,24,40,0.06)] transition-all hover:border-[#cbd5e1] hover:bg-[#f8fafc] hover:text-[#101828]"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto bg-white">
        {loading ? (
          <div className="flex h-full items-center justify-center px-4 text-sm text-[#6a7282]">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在加载聊天...
          </div>
        ) : (
          <div className="flex min-h-full flex-col gap-4 px-4 py-4">
            {renderedMessages.map((message) => {
              const isUser = message.role === "user";

              return (
                <div
                  key={message.id}
                  className={`flex items-start gap-3 ${isUser ? "justify-end" : ""}`}
                >
                  {!isUser ? (
                    <div className="mt-[2px] flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-[#e5e7eb] text-[#4b5563]">
                      <Bot className="h-4 w-4" />
                    </div>
                  ) : null}

                  <div
                    data-message-role={message.role}
                    className={`agent-message-bubble rounded-[10px] px-4 py-2 ${
                      isUser ? "min-h-[56px] bg-[#155dfc]" : "bg-[#f3f4f6]"
                    }`}
                    style={
                      isUser
                        ? {
                            backgroundColor: "#155dfc",
                            color: "#ffffff",
                          }
                        : undefined
                    }
                  >
                    <div
                      className={`whitespace-pre-wrap text-[14px] leading-5 ${
                        isUser ? "text-white" : "text-[#101828]"
                      }`}
                    >
                      {message.content}
                    </div>
                    <div
                      className={`mt-1 text-[12px] leading-4 ${
                        isUser ? "text-[#dbeafe]" : "text-[#6a7282]"
                      }`}
                    >
                      {formatTime(message.createdAt)}
                    </div>
                    {!isUser && degraded?.messageId === message.id ? (
                      <div
                        data-testid="degraded-note"
                        className="mt-2 flex items-start gap-2 rounded-[6px] border border-[#fde68a] bg-[#fffbeb] px-2 py-1.5 text-[12px] leading-4 text-[#92400e]"
                      >
                        <AlertTriangle className="mt-[1px] h-3.5 w-3.5 flex-shrink-0" />
                        <span>{degraded.message}</span>
                      </div>
                    ) : null}
                  </div>

                  {isUser ? (
                    <div className="mt-[2px] flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-[#155dfc] text-white">
                      <User className="h-4 w-4" />
                    </div>
                  ) : null}
                </div>
              );
            })}

            {sending ? (
              <div className="flex items-start gap-3">
                <div className="mt-[2px] flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-[#e5e7eb] text-[#4b5563]">
                  <Bot className="h-4 w-4" />
                </div>
                <div className="rounded-[10px] bg-[#f3f4f6] px-4 py-2 text-[14px] leading-5 text-[#6a7282]">
                  <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
                  助手正在思考...
                </div>
              </div>
            ) : null}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      <div className="border-t border-[#e5e7eb] bg-[#f9fafb] px-4 py-[9px]">
        <div className="mb-2 text-[12px] leading-4 text-[#4a5565]">快捷指令:</div>
        <div className="flex flex-wrap gap-2">
          {QUICK_COMMANDS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => void handleSend(item)}
              className="rounded-[4px] border border-[#d1d5dc] bg-white px-[9px] py-[5px] text-[12px] leading-4 font-medium text-[#0a0a0a] transition-colors hover:border-[#bfd3ff] hover:bg-[#eff6ff]"
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <div className="border-t border-[#e5e7eb] bg-white px-4 py-[17px]">
        <div className="flex items-start gap-2">
          <div className="flex h-[38px] flex-1 items-center rounded-[10px] border border-[#d1d5dc] bg-white px-3">
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSend();
                }
              }}
              placeholder="输入您的问题..."
              className="h-[22px] w-full resize-none bg-transparent text-[14px] leading-5 text-[#101828] outline-none placeholder:text-[rgba(10,10,10,0.5)]"
            />
          </div>

          <button
            type="button"
            onClick={() => void handleSend()}
            disabled={!input.trim() || sending}
            aria-label="send message"
            className="flex h-[38px] w-12 items-center justify-center rounded-[10px] bg-[#155dfc] text-white transition-all hover:bg-[#0f4de1] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {sending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>

      {showHistory ? (
        <div className="absolute inset-0 z-20 flex bg-white/72 backdrop-blur-[1px]">
          <div className="ml-auto flex h-full w-[268px] flex-col border-l border-[#e5e7eb] bg-white shadow-[0_20px_40px_rgba(15,23,42,0.1)]">
            <div className="flex h-[69px] items-center justify-between border-b border-[#e5e7eb] px-4">
              <div className="text-[14px] leading-5 font-medium text-[#101828]">
                历史会话
              </div>
              <button
                type="button"
                onClick={() => setShowHistory(false)}
                aria-label="关闭历史会话"
                className="flex h-7 w-7 items-center justify-center rounded-md border border-[#e5e7eb] bg-white text-[#6a7282] transition-all hover:border-[#cbd5e1] hover:bg-[#f8fafc] hover:text-[#101828]"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-3 py-3">
              <div className="space-y-2">
                {visibleConversations.map((conversation) => {
                  const active = conversation.id === currentConversationId;

                  return (
                    <button
                      key={conversation.id}
                      type="button"
                      onClick={() => {
                        void selectConversation(conversation.id);
                        setShowHistory(false);
                      }}
                      className={`w-full rounded-[12px] border px-3 py-3 text-left transition-all ${
                        active
                          ? "border-[#bfd3ff] bg-[#eff6ff]"
                          : "border-[#e5e7eb] bg-white hover:border-[#cbd5e1] hover:bg-[#f8fafc]"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="truncate text-[13px] leading-5 font-medium text-[#101828]">
                          {conversation.title}
                        </div>
                        <ChevronRight className="h-4 w-4 flex-shrink-0 text-[#9aa4b2]" />
                      </div>
                      <div className="mt-1 truncate text-[12px] leading-4 text-[#6a7282]">
                        {conversation.lastMessagePreview || "新会话"}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
