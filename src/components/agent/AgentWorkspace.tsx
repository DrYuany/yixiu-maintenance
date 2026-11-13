import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import { useAgentStore } from "../../store/agent-store";
import { ApiKeyDialog } from "./ApiKeyDialog";
import { ConversationCanvas } from "./ConversationCanvas";
import { ConversationRail } from "./ConversationRail";
import { PlanDetailDrawer } from "./PlanDetailDrawer";
import { PlanHistoryPanel } from "./PlanHistoryPanel";
import { SolveWaitDialog } from "./SolveWaitDialog";
import { sortVersionsChronologically } from "./plan-format";

export function AgentWorkspace() {
  const {
    conversations,
    currentConversationId,
    workingMemory,
    messages,
    loading,
    sending,
    streamStatus,
    streamingMessageId,
    error,
    decisionArtifact,
    activeJob,
    pendingChange,
    stages,
    clarificationOptions,
    versions,
    createConversation,
    selectConversation,
    sendMessage,
    controlActiveJob,
    quota,
    quotaBlocked,
    saveApiKey,
    clearApiKey,
    dismissClarification,
  } = useAgentStore();
  const [contextVersionId, setContextVersionId] = useState<string | null>(null);
  const [detailVersionId, setDetailVersionId] = useState<string | null>(null);
  const [keyDialogOpen, setKeyDialogOpen] = useState(false);
  // 窄屏把两条侧栏收成呼出抽屉。桌面完全不渲染这些按钮，也不加这些 class。
  const [mobilePanel, setMobilePanel] = useState<"rail" | "plans" | null>(null);
  const chronologicalVersions = sortVersionsChronologically(versions);
  const contextVersion =
    versions.find((item) => item.id === contextVersionId) ?? versions[0] ?? null;
  const detailVersion = versions.find((item) => item.id === detailVersionId) ?? null;
  const detailPlanNumber = detailVersion
    ? chronologicalVersions.findIndex((item) => item.id === detailVersion.id) + 1
    : null;
  const openVersion = useCallback((versionId: string) => {
    setContextVersionId(versionId);
    setDetailVersionId(versionId);
    setMobilePanel(null);
  }, []);
  const closeVersion = useCallback(() => {
    setDetailVersionId(null);
  }, []);

  useEffect(() => {
    setContextVersionId(null);
    setDetailVersionId(null);
  }, [currentConversationId]);

  const persistedContextVersionId = workingMemory?.selectedVersionIds?.[0] ?? null;
  useEffect(() => {
    if (
      persistedContextVersionId
      && versions.some((item) => item.id === persistedContextVersionId)
    ) {
      setContextVersionId(persistedContextVersionId);
    }
  }, [persistedContextVersionId, versions]);

  useEffect(() => {
    if (quotaBlocked) setKeyDialogOpen(true);
  }, [quotaBlocked]);

  const workspaceClass = [
    "agent-workspace",
    mobilePanel === "rail" ? "mobile-rail-open" : "",
    mobilePanel === "plans" ? "mobile-plans-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={workspaceClass}>
      <ConversationRail
        conversations={conversations}
        activeId={currentConversationId}
        onCreate={() => void createConversation()}
        onSelect={(id) => {
          setMobilePanel(null);
          void selectConversation(id);
        }}
      />
      {loading ? (
        <main className="agent-loading" aria-label="Agent 对话">正在恢复会话…</main>
      ) : (
        <ConversationCanvas
          messages={messages}
          versions={versions}
          sending={sending}
          streamStatus={streamStatus}
          streamingMessageId={streamingMessageId}
          activeJob={activeJob}
          pendingChange={pendingChange}
          stages={stages}
          clarificationOptions={clarificationOptions}
          onDismissClarify={dismissClarification}
          contextVersion={contextVersion}
          onClearContext={() => setContextVersionId(null)}
          onSend={(message) =>
            sendMessage(
              message,
              message === "确认生成沙盘" && pendingChange?.baseVersionId
                ? pendingChange.baseVersionId
                : contextVersion?.id ?? null,
            )
          }
          onOpenVersion={openVersion}
          onControlJob={(action) => controlActiveJob(action)}
          quota={quota}
          onRequestKey={() => setKeyDialogOpen(true)}
          onOpenRail={() => setMobilePanel((panel) => panel === "rail" ? null : "rail")}
          onOpenPlans={() => setMobilePanel((panel) => panel === "plans" ? null : "plans")}
        />
      )}
      {mobilePanel ? (
        <>
          <button
            type="button"
            className="mobile-panel-backdrop"
            aria-label="关闭侧栏"
            onClick={() => setMobilePanel(null)}
          />
          <button
            type="button"
            className={`mobile-panel-close is-${mobilePanel}`}
            aria-label="关闭侧栏"
            onClick={() => setMobilePanel(null)}
          >
            <X size={16} />
          </button>
        </>
      ) : null}
      {error ? <div className="agent-error">{error}</div> : null}
      <PlanHistoryPanel
        versions={versions}
        activeJob={activeJob}
        selectedVersionId={contextVersion?.id ?? null}
        onOpenVersion={openVersion}
      />
      <PlanDetailDrawer
        version={detailVersion}
        planNumber={detailPlanNumber}
        decisionArtifact={decisionArtifact}
        job={activeJob}
        onClose={closeVersion}
      />
      <SolveWaitDialog
        open={Boolean(activeJob?.needsUserDecision)}
        elapsedSeconds={activeJob?.elapsedSeconds ?? 0}
        hasIncumbent={activeJob?.hasIncumbent ?? false}
        onAction={(action) => void controlActiveJob(action)}
      />
      <ApiKeyDialog
        open={keyDialogOpen}
        hasKey={Boolean(quota?.ownKey)}
        limit={quota?.limit ?? 50}
        onClose={() => setKeyDialogOpen(false)}
        onSubmit={async (key) => {
          await saveApiKey(key);
          setKeyDialogOpen(false);
        }}
        onClear={async () => {
          await clearApiKey();
        }}
      />
    </div>
  );
}
