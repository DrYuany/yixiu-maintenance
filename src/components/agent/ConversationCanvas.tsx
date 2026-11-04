import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  ChevronDown,
  Database,
  KeyRound,
  Layers3,
  Paperclip,
  Play,
  Send,
  ShieldCheck,
  Sparkles,
  User,
  X,
} from "lucide-react";
import type {
  ClarificationOption,
  Message,
  PendingModelChange,
  QuotaInfo,
  SolveJob,
  StageEvent,
  WindowVersion,
} from "../../store/types";
import { ScheduleResultCard } from "./ScheduleResultCard";
import { MessageBody } from "./MessageBody";
import { sortVersionsChronologically } from "./plan-format";
import { PRODUCT_NAME, SITE_NAME } from "../../lib/product";

interface Props {
  messages: Message[];
  versions: WindowVersion[];
  sending: boolean;
  streamStatus?: string | null;
  streamingMessageId?: string | null;
  activeJob?: SolveJob | null;
  pendingChange?: PendingModelChange | null;
  stages?: StageEvent[];
  clarificationOptions?: ClarificationOption[] | null;
  contextVersion?: WindowVersion | null;
  onSend: (message: string) => Promise<void>;
  onOpenVersion: (versionId: string) => void;
  onClearContext?: () => void;
  onControlJob?: (action: "wait" | "background" | "use_incumbent" | "stop") => Promise<void>;
  engine?: "llm" | "rule";
  onEngineChange?: (engine: "llm" | "rule") => void;
  /** 免费体验额度；null 表示服务端没开限额或还没读到 */
  quota?: QuotaInfo | null;
  onRequestKey?: () => void;
}

const suggestions = [
  "按高风险优先重新排程",
  "船租上涨 20%，比较成本变化",
  "每天最多维修 8 小时",
];

function buildMessageVersionLinks(messages: Message[], versions: WindowVersion[]) {
  const links = new Map<string, WindowVersion[]>();
  const claimed = new Set<string>();
  const byId = new Map(versions.map((version) => [version.id, version]));

  for (const message of messages) {
    if (message.role !== "assistant" || !message.versionIds?.length) continue;
    const attached = message.versionIds
      .map((versionId) => byId.get(versionId))
      .filter((version): version is WindowVersion => Boolean(version));
    if (!attached.length) continue;
    links.set(message.id, attached);
    attached.forEach((version) => claimed.add(version.id));
  }

  const assistantMessages = messages.filter((message) => message.role === "assistant");
  const orphans: WindowVersion[] = [];

  for (const version of versions) {
    if (claimed.has(version.id)) continue;
    const generatedAt = Date.parse(version.generatedAt);
    let closest: Message | null = null;
    let closestDistance = Number.POSITIVE_INFINITY;

    if (Number.isFinite(generatedAt)) {
      for (const message of assistantMessages) {
        const messageTime = Date.parse(message.createdAt);
        if (!Number.isFinite(messageTime)) continue;
        const distance = Math.abs(messageTime - generatedAt);
        if (distance < closestDistance) {
          closest = message;
          closestDistance = distance;
        }
      }
    }

    if (closest && closestDistance <= 2 * 60 * 1000) {
      links.set(closest.id, [...(links.get(closest.id) ?? []), version]);
      claimed.add(version.id);
    } else {
      orphans.push(version);
    }
  }

  return { links, orphans };
}

function describeDslConstraint(constraint: Record<string, unknown>): string {
  if (Array.isArray(constraint.tasks)) return constraint.tasks.map(String).join(" / ");
  if (typeof constraint.task === "string") return constraint.task;
  if (typeof constraint.before === "string" || typeof constraint.after === "string") {
    return [constraint.before, constraint.after].filter(Boolean).map(String).join(" → ");
  }
  if (constraint.line !== undefined) return `作业线 ${String(constraint.line)}`;
  if (typeof constraint.time === "string") return constraint.time.slice(0, 16).replace("T", " ");
  if (constraint.value !== undefined) return String(constraint.value);
  return "变更";
}

function dslChangeItems(dsl: Record<string, unknown> | undefined) {
  if (!dsl) return [];
  const items: Array<{ label: string; value: string }> = [];
  for (const constraint of Array.isArray(dsl.constraints) ? dsl.constraints : []) {
    const spec = constraint as Record<string, unknown>;
    items.push({ label: `约束 · ${String(spec.type ?? "dsl")}`, value: describeDslConstraint(spec) });
  }
  for (const objective of Array.isArray(dsl.objectives) ? dsl.objectives : []) {
    const spec = objective as Record<string, unknown>;
    items.push({ label: "目标", value: String(spec.type ?? "objective") });
  }
  if (dsl.parameters && typeof dsl.parameters === "object") {
    for (const [key, value] of Object.entries(dsl.parameters as Record<string, unknown>)) {
      items.push({ label: `参数 · ${key}`, value: String(value) });
    }
  }
  if (dsl.data && typeof dsl.data === "object") {
    for (const [key, value] of Object.entries(dsl.data as Record<string, unknown>)) {
      items.push({ label: `数据 · ${key}`, value: Array.isArray(value) ? `${value.length} 项` : String(value) });
    }
  }
  return items;
}

export function ConversationCanvas({
  messages,
  versions = [],
  sending,
  streamStatus = null,
  streamingMessageId = null,
  activeJob,
  pendingChange,
  stages = [],
  clarificationOptions = null,
  contextVersion,
  onSend,
  onOpenVersion,
  onClearContext = () => undefined,
  onControlJob,
  engine = "llm",
  onEngineChange = () => undefined,
  quota = null,
  onRequestKey = () => undefined,
}: Props) {
  const [value, setValue] = useState("");
  const [showProcess, setShowProcess] = useState(true);
  const submitting = useRef(false);
  const threadEnd = useRef<HTMLDivElement>(null);
  const solving = activeJob?.status === "QUEUED" || activeJob?.status === "RUNNING";
  const backgrounded = Boolean(activeJob?.backgrounded);
  const chronologicalVersions = useMemo(
    () => sortVersionsChronologically(versions),
    [versions],
  );
  const planNumbers = useMemo(
    () => new Map(chronologicalVersions.map((version, index) => [version.id, index + 1])),
    [chronologicalVersions],
  );
  const { links, orphans } = useMemo(
    () => buildMessageVersionLinks(messages, chronologicalVersions),
    [messages, chronologicalVersions],
  );
  const latestVersionId = chronologicalVersions.at(-1)?.id;
  const lastMessageLength = messages.at(-1)?.content.length ?? 0;

  useEffect(() => {
    if (typeof threadEnd.current?.scrollIntoView === "function") {
      threadEnd.current.scrollIntoView({
        behavior: streamingMessageId ? "auto" : "smooth",
        block: "end",
        inline: "nearest",
      });
    }
  }, [messages.length, solving, latestVersionId, lastMessageLength, streamingMessageId]);

  const submit = async (next = value) => {
    const text = next.trim();
    if (!text || sending || submitting.current) return;
    submitting.current = true;
    setValue("");
    try {
      await onSend(text);
    } finally {
      submitting.current = false;
    }
  };

  return (
    <main className="agent-canvas" aria-label="Agent 对话">
      <header className="agent-topbar">
        <div>
          <strong>{PRODUCT_NAME}</strong>
          <span>{SITE_NAME}</span>
        </div>
        <div className="agent-topbar-side">
          {quota && (quota.enabled || quota.ownKey) ? (
            <button
              type="button"
              className={`quota-pill${quota.ownKey ? " is-own" : ""}${
                !quota.ownKey && quota.remaining <= 0 ? " is-empty" : ""
              }`}
              onClick={onRequestKey}
              title={
                quota.ownKey
                  ? "当前使用你自己填的 DeepSeek 密钥，点击可更换或移除"
                  : `免费体验共 ${quota.limit} 轮，已用 ${quota.used} 轮，点击填入自己的密钥`
              }
            >
              <KeyRound size={12} />
              {quota.ownKey ? "我的密钥" : `免费剩 ${quota.remaining} 轮`}
            </button>
          ) : null}
          <div className="engine-toggle" role="group" aria-label="理解引擎">
            <button
              type="button"
              className={`engine-option${engine === "llm" ? " is-active" : ""}`}
              onClick={() => onEngineChange("llm")}
              title="LLM 主导：模型先读全句，护栏只做否决"
            >
              LLM 优先
            </button>
            <button
              type="button"
              className={`engine-option${engine === "rule" ? " is-active" : ""}`}
              onClick={() => onEngineChange("rule")}
              title="规则主导：正则先拦，拦不住再问模型"
            >
              规则优先
            </button>
          </div>
        </div>
      </header>

      <div className="agent-thread">
        {messages.length === 0 ? (
          <div className="agent-empty">
            <div className="empty-agent-mark"><Bot size={25} /></div>
            <h2>从一句检修目标开始</h2>
            <p>
              告诉我优先目标、预算或时间约束。我会先锁定模型契约，再调用真实
              CP-SAT 求解，并把每次方案保留在对话里。
            </p>
            <div className="empty-capabilities">
              <span><Database size={14} />数据快照可追溯</span>
              <span><ShieldCheck size={14} />可行性单独复核</span>
              <span><Sparkles size={14} />每轮方案不覆盖</span>
            </div>
          </div>
        ) : null}

        {messages.map((message) => {
          const attachedVersions = links.get(message.id) ?? [];
          return (
            <article key={message.id} className={`thread-message ${message.role}`}>
              <div className="message-avatar">
                {message.role === "assistant" ? <Bot size={17} /> : <User size={17} />}
              </div>
              <div className="message-stack">
                <div className="message-author">
                  {message.role === "assistant" ? PRODUCT_NAME : "你"}
                </div>
                <div className="message-content">
                  <MessageBody
                    content={message.content.replace(
                      "详细任务和审计证据已写入右侧结果区。",
                      "点击下方方案卡查看完整排程和审计证据。",
                    )}
                    streaming={message.id === streamingMessageId}
                  />
                </div>
                {attachedVersions.length ? (
                  <div className="message-result-stack">
                    {attachedVersions.map((version) => (
                      <ScheduleResultCard
                        key={version.id}
                        version={version}
                        planNumber={planNumbers.get(version.id) ?? 1}
                        onOpen={onOpenVersion}
                      />
                    ))}
                  </div>
                ) : null}
              </div>
            </article>
          );
        })}

        {orphans.length ? (
          <article className="thread-message assistant">
            <div className="message-avatar"><Bot size={17} /></div>
            <div className="message-stack">
              <div className="message-author">{PRODUCT_NAME}</div>
              <div className="message-content">
                以下方案已保存在本次会话中，但原对话记录没有留下关联标记。
              </div>
              <div className="message-result-stack">
                {orphans.map((version) => (
                  <ScheduleResultCard
                    key={version.id}
                    version={version}
                    planNumber={planNumbers.get(version.id) ?? 1}
                    onOpen={onOpenVersion}
                  />
                ))}
              </div>
            </div>
          </article>
        ) : null}

        {clarificationOptions && clarificationOptions.length > 0 && !sending ? (
          <div className="clarify-options" aria-live="polite">
            <div className="clarify-options-label">你可以点选一个：</div>
            {clarificationOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                className="clarify-option"
                onClick={() => void onSend(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        ) : null}

        {sending || (solving && !backgrounded) ? (
          <div className="agent-thinking" aria-live="polite">
            {stages.length === 0 ? (
              <div className="thinking-line">
                <span /><span /><span />
                {sending
                  ? streamStatus || "正在读取事实并组织回答"
                  : (activeJob?.statusMessage || `真实求解进行中 · 已运行 ${activeJob?.elapsedSeconds ?? 0} 秒`)}
              </div>
            ) : null}
            {stages.length > 0 ? (
              <div className="process-panel">
                <button
                  type="button"
                  className="process-toggle"
                  onClick={() => setShowProcess((previous) => !previous)}
                >
                  <ChevronDown size={12} className={showProcess ? "rotated" : ""} />
                  {showProcess ? "收起过程" : `展开过程（${stages.length} 步）`}
                </button>
                {showProcess ? (
                  <ol className="process-steps">
                    {stages.map((stage) => (
                      <li key={stage.id} className={`process-step is-${stage.status}`}>
                        <span className="process-icon" aria-hidden="true">
                          {stage.status === "running"
                            ? <i className="spin-dot" />
                            : stage.status === "done" ? "✓" : "✗"}
                        </span>
                        <span className="process-label">{stage.label}</span>
                        {stage.detail ? <small className="process-detail">{stage.detail}</small> : null}
                      </li>
                    ))}
                  </ol>
                ) : null}
              </div>
            ) : null}
            {solving ? (
              <div className="solve-progress">
                <i style={{ width: `${activeJob?.progressPercent ?? 15}%` }} />
                <small>{activeJob?.progressPercent ?? 15}% · {activeJob?.elapsedSeconds ?? 0}s</small>
                {onControlJob ? (
                  <span className="solve-actions">
                    <button type="button" onClick={() => void onControlJob("background")}>后台运行</button>
                    <button type="button" className="danger" onClick={() => void onControlJob("stop")}>停止</button>
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
        {solving && backgrounded ? (
          <div className="agent-thinking slim" aria-live="polite">
            <span>后台求解中 · {activeJob?.progressPercent ?? 15}%</span>
            {onControlJob ? (
              <button type="button" className="danger" onClick={() => void onControlJob("stop")}>停止</button>
            ) : null}
          </div>
        ) : null}
        <div ref={threadEnd} />
      </div>

      <footer className="agent-composer-wrap">
        {contextVersion ? (
          <div className="agent-context-bar">
            <span><Layers3 size={13} />当前模型上下文</span>
            <strong>
              方案 {String(planNumbers.get(contextVersion.id) ?? 1).padStart(2, "0")}
            </strong>
            <small>
              {contextVersion.windows[0]
                ? `${contextVersion.windows[0].windowId} · ${contextVersion.windows[0].metrics.startTime.slice(0, 10)}`
                : contextVersion.focusTag}
            </small>
            <button type="button" onClick={onClearContext}>跟随最新</button>
          </div>
        ) : null}
        {pendingChange ? (
          (() => {
            const isDsl = pendingChange.kind === "dsl" || Array.isArray(pendingChange.dsl?.constraints);
            const dslItems = isDsl ? dslChangeItems(pendingChange.dsl) : [];
            const operationItems = (pendingChange.operations ?? []).map((operation) => ({
              label: operation.key,
              value: String(operation.after ?? "移除"),
            }));
            const items = isDsl ? dslItems : operationItems;
            return (
              <div className="pending-change-card" aria-live="polite">
                <div className="pending-change-head">
                  <span><Sparkles size={14} />待确认模型变更</span>
                  <strong>{isDsl ? `${items.length} 条` : `${items.length} 项`}</strong>
                </div>
                <div className="pending-change-list">
                  {items.slice(0, 4).map((item, index) => (
                    <span key={`${item.label}-${index}`}>
                      <small>{item.label}</small>
                      <strong>{item.value}</strong>
                    </span>
                  ))}
                </div>
                <div className="pending-change-actions">
                  <button
                    type="button"
                    className="confirm"
                    disabled={sending}
                    onClick={() => void submit("确认生成沙盘")}
                  >
                    <Play size={13} />确认生成沙盘
                  </button>
                  <button
                    type="button"
                    disabled={sending}
                    onClick={() => void submit("取消变更")}
                  >
                    <X size={13} />取消
                  </button>
                </div>
              </div>
            );
          })()
        ) : null}
        {quota && quota.enabled && !quota.ownKey && quota.remaining <= 0 ? (
          <div className="quota-banner" role="status">
            <div>
              <strong>免费体验的 {quota.limit} 轮已经用完</strong>
              <small>填入你自己的 DeepSeek API Key 就能继续，额度不限。</small>
            </div>
            <button type="button" onClick={onRequestKey}>填入密钥</button>
          </div>
        ) : null}
        <div className="suggestion-row">
          {suggestions.map((item) => (
            <button
              type="button"
              key={item}
              disabled={sending}
              onClick={() => void submit(item)}
            >
              {item}
            </button>
          ))}
        </div>
        <div className="agent-composer">
          <textarea
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder="描述目标、约束或要调整的方案…"
          />
          <div className="composer-actions">
            <button type="button" aria-label="添加附件"><Paperclip size={17} /></button>
            <span>Enter 发送 · Shift+Enter 换行</span>
            <button
              type="button"
              className="send-agent-message"
              aria-label="发送"
              disabled={!value.trim() || sending}
              onClick={() => void submit()}
            >
              <Send size={16} />
            </button>
          </div>
        </div>
      </footer>
    </main>
  );
}
