import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { apiClient, isQuotaError } from "@/lib/api-client";
import { setStoredApiKey } from "@/lib/llm-key";
import type {
  AgentResponse,
  CandidateWindow,
  ClarificationOption,
  Conversation,
  ConversationDetail,
  DashboardResponse,
  Message,
  PendingModelChange,
  PageId,
  QuotaInfo,
  SortMode,
  WorkingMemory,
  DecisionArtifact,
  SolveEvidence,
  SolveJob,
  StageEvent,
  DegradedPayload,
} from "@/store/types";
import { createOfflineDemo, isStandaloneFileProtocol } from "@/store/offline-demo";

interface AgentStoreValue {
  conversations: Conversation[];
  currentConversationId: string | null;
  currentView: PageId;
  workingMemory: WorkingMemory | null;
  messages: Message[];
  versions: DashboardResponse["versions"];
  candidateWindowPool: CandidateWindow[];
  topRecommendations: CandidateWindow[];
  sortMode: SortMode;
  loading: boolean;
  sending: boolean;
  streamStatus: string | null;
  streamingMessageId: string | null;
  error: string | null;
  decisionArtifact: DecisionArtifact | null;
  solveEvidence: SolveEvidence | null;
  activeJob: SolveJob | null;
  pendingChange: PendingModelChange | null;
  stages: StageEvent[];
  /** 理解引擎：llm = LLM 主导（默认），rule = 规则主导。 */
  engine: "llm" | "rule";
  /**
   * 这一轮没走成模型时留下的说明，挂在具体那条助手消息上。
   * 不写进消息正文：正文是产品产出，这条是系统状态，混在一起会被摘抄走。
   */
  degraded: (DegradedPayload & { messageId: string }) | null;
  /** 澄清候选（意图推荐）：非空时在消息下方渲染可点击选项。 */
  clarificationOptions: ClarificationOption[] | null;
  /** 关掉澄清选项（点「都不是，我重新说」时），让用户自由输入。 */
  dismissClarification: () => void;
  /** 免费体验额度；null = 还没读到（不显示提示） */
  quota: QuotaInfo | null;
  /**
   * 额度用完时被拦下的那条话。留着是为了让用户在弹窗里填完密钥后
   * 能原地重发——否则他得自己把刚打的字再敲一遍。
   */
  quotaBlocked: { message: string } | null;
  saveApiKey: (key: string) => Promise<void>;
  clearApiKey: () => Promise<void>;
  refreshQuota: () => Promise<void>;
  dismissQuota: () => void;
  createConversation: () => Promise<void>;
  selectConversation: (conversationId: string) => Promise<void>;
  sendMessage: (
    message: string,
    selectedVersionId?: string | null,
    selectedWindowId?: string | null,
  ) => Promise<void>;
  setView: (view: PageId) => void;
  setEngine: (engine: "llm" | "rule") => void;
  setSortMode: (sortMode: SortMode) => Promise<void>;
  deleteVersion: (versionId: string) => Promise<void>;
  deleteCandidate: (candidateId: string) => Promise<void>;
  reorderCandidate: (candidateId: string, direction: "up" | "down") => Promise<void>;
  toggleVersionInPool: (
    versionId: string,
    includedInCandidatePool: boolean,
  ) => Promise<void>;
  refreshCurrentConversation: () => Promise<void>;
  controlActiveJob: (action: "wait" | "background" | "use_incumbent" | "stop") => Promise<void>;
}

const AgentStoreContext = createContext<AgentStoreValue | null>(null);

function getEmptyDashboard(
  conversationId: string | null = null,
): DashboardResponse {
  return {
    conversationId: conversationId ?? "",
    sortMode: "latest_first",
    versions: [],
    candidateWindowPool: [],
    topRecommendations: [],
  };
}

function jobToEvidence(job: SolveJob): SolveEvidence {
  return {
    status: job.solverStatus,
    hasIncumbent: job.hasIncumbent,
    proof: job.solverStatus === "OPTIMAL" || job.solverStatus === "INFEASIBLE",
    elapsedSeconds: job.elapsedSeconds,
    phase: job.status.toLowerCase(),
    reason: job.error,
    running: job.status === "QUEUED" || job.status === "RUNNING",
    modelFingerprint: job.modelFingerprint,
  };
}

export function AgentStoreProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const offline = typeof window !== "undefined" && isStandaloneFileProtocol(window.location.protocol);
  const offlineDemo = useMemo(() => createOfflineDemo(), []);
  const [conversations, setConversations] = useState<Conversation[]>(offline ? offlineDemo.conversations : []);
  const [currentConversationId, setCurrentConversationId] = useState<
    string | null
  >(offline ? offlineDemo.conversations[0].id : null);
  const [currentView, setCurrentView] = useState<PageId>("P01");
  const [workingMemory, setWorkingMemory] = useState<WorkingMemory | null>(
    null,
  );
  const [messages, setMessages] = useState<Message[]>(offline ? offlineDemo.messages : []);
  const [dashboard, setDashboard] = useState<DashboardResponse>(
    getEmptyDashboard(),
  );
  const [loading, setLoading] = useState(!offline);
  const [sending, setSending] = useState(false);
  const [streamStatus, setStreamStatus] = useState<string | null>(null);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [stages, setStages] = useState<StageEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState<(DegradedPayload & { messageId: string }) | null>(null);
  const [clarificationOptions, setClarificationOptions] = useState<ClarificationOption[] | null>(null);
  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [quotaBlocked, setQuotaBlocked] = useState<{ message: string } | null>(null);
  const [engine, setEngine] = useState<"llm" | "rule">("llm");
  const [decisionArtifact, setDecisionArtifact] = useState<DecisionArtifact | null>(offline ? offlineDemo.decisionArtifact : null);
  const [solveEvidence, setSolveEvidence] = useState<SolveEvidence | null>(offline ? offlineDemo.solveEvidence : null);
  const [activeJobs, setActiveJobs] = useState<SolveJob[]>([]);
  // 界面上的「当前任务」：优先取最新一个仍在运行的，其次取最新一个
  const activeJob = useMemo<SolveJob | null>(() => {
    const running = activeJobs.filter((job) => ["QUEUED", "RUNNING"].includes(job.status));
    return running[0] ?? activeJobs[0] ?? null;
  }, [activeJobs]);
  const sendInFlight = useRef(false);
  const hydratingRef = useRef(false);
  // 所有仍在运行的 job id（以逗号串成稳定依赖，只在运行集合变化时触发轮询重建）
  const runningJobIds = useMemo(
    () =>
      activeJobs
        .filter((job) => ["QUEUED", "RUNNING"].includes(job.status))
        .map((job) => job.id)
        .join(","),
    [activeJobs],
  );

  const refreshConversationList = useCallback(async () => {
    const next = await apiClient.listConversations();
    setConversations(next);
    return next;
  }, []);

  const refreshQuota = useCallback(async () => {
    if (offline) return;
    try {
      setQuota(await apiClient.getQuota());
    } catch {
      // 额度只是页面提示，读不到不影响对话本身
    }
  }, [offline]);

  const syncDashboard = useCallback(
    async (conversationId: string, nextSortMode: SortMode) => {
      const nextDashboard = await apiClient.getDashboard(
        conversationId,
        nextSortMode,
      );
      setDashboard(nextDashboard);
    },
    [],
  );

  const hydrateConversation = useCallback(
    async (conversationId: string, nextSortMode: SortMode) => {
      const [detail, nextDashboard, jobs] = await Promise.all([
        apiClient.getConversation(conversationId),
        apiClient.getDashboard(conversationId, nextSortMode),
        apiClient.getSolveJobs(conversationId),
      ]);

      setCurrentConversationId(conversationId);
      setMessages(detail.messages);
      setWorkingMemory(detail.workingMemory);
      setCurrentView(detail.activeView ?? detail.workingMemory.activeView);
      setDashboard(nextDashboard);
      setActiveJobs(jobs);
      const latestJob = jobs[0] ?? null;
      setDecisionArtifact(latestJob?.result?.decisionArtifact ?? null);
      setSolveEvidence(latestJob ? jobToEvidence(latestJob) : null);
    },
    [],
  );

  const selectConversation = useCallback(
    async (conversationId: string) => {
      setLoading(true);
      setError(null);
      try {
        await hydrateConversation(conversationId, dashboard.sortMode);
      } catch (nextError) {
        setError(
          nextError instanceof Error
            ? nextError.message
            : "加载会话失败，请稍后重试。",
        );
      } finally {
        setLoading(false);
      }
    },
    [dashboard.sortMode, hydrateConversation],
  );

  const createConversation = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const created = await apiClient.createConversation("新会话");
      await refreshConversationList();
      await hydrateConversation(created.id, "latest_first");
    } catch (nextError) {
      setError(
        nextError instanceof Error
          ? nextError.message
          : "创建会话失败，请检查后端服务。",
      );
      setLoading(false);
    } finally {
      setLoading(false);
    }
  }, [hydrateConversation, refreshConversationList]);

  const refreshCurrentConversation = useCallback(async () => {
    if (!currentConversationId) return;

    setLoading(true);
    setError(null);
    try {
      await hydrateConversation(currentConversationId, dashboard.sortMode);
    } catch (nextError) {
      setError(
        nextError instanceof Error
          ? nextError.message
          : "刷新当前会话失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [currentConversationId, dashboard.sortMode, hydrateConversation]);

  const sendMessage = useCallback(
    async (
      message: string,
      selectedVersionId?: string | null,
      selectedWindowId?: string | null,
    ) => {
      if (!currentConversationId || sendInFlight.current) return;

      sendInFlight.current = true;
      setSending(true);
      setError(null);
      setDegraded(null);
      setClarificationOptions(null);
      const optimisticMessage: Message = {
        id: `temp_${Date.now()}`,
        conversationId: currentConversationId,
        role: "user",
        content: message,
        createdAt: new Date().toISOString(),
      };
      const assistantPlaceholder: Message = {
        id: `temp_reply_${Date.now()}`,
        conversationId: currentConversationId,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
      };
      setMessages((previous) => [...previous, optimisticMessage, assistantPlaceholder]);
      if (offline) {
        setMessages((previous) => previous.map((item) =>
          item.id === assistantPlaceholder.id
            ? {
                ...item,
                content: "这是离线界面演示。通过 npm run dev 并启动 FastAPI 后端后，我会把这条需求编译成模型契约并调用真实求解器。",
              }
            : item,
        ));
        setSending(false);
        return;
      }
      let streamed = false;
      const applyResponse = async (response: AgentResponse) => {
        setDecisionArtifact(response.decisionArtifact ?? null);
        setSolveEvidence(response.solveEvidence ?? null);
        setDegraded(
          response.degraded
            ? { ...response.degraded, messageId: response.assistantMessage.id }
            : null,
        );
        setClarificationOptions(response.clarificationOptions ?? null);
        if (response.solveJob) {
          setActiveJobs((previous) => {
            const others = previous.filter((job) => job.id !== response.solveJob!.id);
            return [response.solveJob!, ...others];
          });
        }
        setCurrentView(response.recommendedView);
        void refreshQuota();
        await refreshConversationList();
        await hydrateConversation(currentConversationId, dashboard.sortMode);
      };
      setStreamingMessageId(assistantPlaceholder.id);
      setStreamStatus("正在读取事实…");
      setStages([]);
      try {
        const response = await apiClient.sendMessageStream(
          currentConversationId,
          message,
          selectedVersionId,
          selectedWindowId,
          {
            onStatus: (label) => setStreamStatus(label),
            onStage: (stage) =>
              setStages((previous) => {
                const index = previous.findIndex((item) => item.id === stage.id);
                if (index >= 0) {
                  const next = previous.slice();
                  next[index] = stage;
                  return next;
                }
                return [...previous, stage];
              }),
            onToken: (text) => {
              streamed = true;
              setStreamStatus(null);
              setMessages((previous) => previous.map((item) =>
                item.id === assistantPlaceholder.id
                  ? { ...item, content: item.content + text }
                  : item,
              ));
            },
            onReplace: (text) => {
              streamed = true;
              setStreamStatus(null);
              setMessages((previous) => previous.map((item) =>
                item.id === assistantPlaceholder.id
                  ? { ...item, content: text }
                  : item,
              ));
            },
          },
          engine,
        );
        await applyResponse(response);
      } catch (nextError) {
        if (isQuotaError(nextError)) {
          // 额度用完不是"失败"：把这轮打出去的消息收回来，等用户填完密钥原地重发。
          setMessages((previous) =>
            previous.filter(
              (item) =>
                item.id !== optimisticMessage.id
                && item.id !== assistantPlaceholder.id,
            ),
          );
          setQuotaBlocked({ message });
          void refreshQuota();
          return;
        }
        if (!streamed) {
          try {
            const response = await apiClient.sendMessage(
              currentConversationId,
              message,
              selectedVersionId,
              selectedWindowId,
              engine,
            );
            await applyResponse(response);
          } catch (fallbackError) {
            setMessages((previous) =>
              previous.filter(
                (item) =>
                  item.id !== optimisticMessage.id
                  && item.id !== assistantPlaceholder.id,
              ),
            );
            setError(
              fallbackError instanceof Error
                ? fallbackError.message
                : "发送消息失败，请稍后再试。",
            );
          }
        } else {
          setError(
            nextError instanceof Error
              ? nextError.message
              : "流式回答中断，已保留本次对话。",
          );
          await hydrateConversation(currentConversationId, dashboard.sortMode);
        }
      } finally {
        sendInFlight.current = false;
        setSending(false);
        setStreamingMessageId(null);
        setStreamStatus(null);
      }
    },
    [currentConversationId, dashboard.sortMode, hydrateConversation, offline, refreshConversationList, refreshQuota, engine],
  );

  const saveApiKey = useCallback(
    async (key: string) => {
      const next = key.trim();
      if (!next) return;
      // 先验证再保存：错密钥留在浏览器里，用户每轮都会看到"模型不可用"，
      // 却不知道问题出在自己贴错了。
      await apiClient.verifyApiKey(next);
      setStoredApiKey(next);
      setError(null);
      const blocked = quotaBlocked;
      setQuotaBlocked(null);
      await refreshQuota();
      // 填完密钥把刚才被拦下的那句话直接重发，不用用户再敲一遍。
      if (blocked) await sendMessage(blocked.message);
    },
    [quotaBlocked, refreshQuota, sendMessage],
  );

  const clearApiKey = useCallback(async () => {
    setStoredApiKey(null);
    await refreshQuota();
  }, [refreshQuota]);

  const dismissQuota = useCallback(() => setQuotaBlocked(null), []);
  const dismissClarification = useCallback(() => setClarificationOptions(null), []);

  const controlActiveJob = useCallback(async (action: "wait" | "background" | "use_incumbent" | "stop") => {
    if (!activeJob) return;
    try {
      const updated = await apiClient.controlSolveJob(activeJob.id, action);
      setActiveJobs((previous) => previous.map((job) => job.id === updated.id ? updated : job));
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "更新求解任务失败");
    }
  }, [activeJob]);

  useEffect(() => {
    const ids = runningJobIds.split(",").filter(Boolean);
    if (ids.length === 0) return;
    let cancelled = false;
    const pollOne = async (jobId: string) => {
      try {
        const next = await apiClient.getSolveJob(jobId);
        if (cancelled) return;
        setActiveJobs((previous) => previous.map((job) => (job.id === jobId ? next : job)));
        if (["COMPLETED", "FAILED", "CANCELLED"].includes(next.status) && currentConversationId) {
          if (next.result?.decisionArtifact) setDecisionArtifact(next.result.decisionArtifact);
          if (!hydratingRef.current) {
            hydratingRef.current = true;
            try {
              await refreshConversationList();
              await hydrateConversation(currentConversationId, dashboard.sortMode);
            } finally {
              hydratingRef.current = false;
            }
          }
        }
      } catch (nextError) {
        if (!cancelled) setError(nextError instanceof Error ? nextError.message : "读取求解进度失败");
      }
    };
    const pollAll = () => {
      for (const id of ids) void pollOne(id);
    };
    void pollAll();
    const timer = window.setInterval(pollAll, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [
    runningJobIds,
    currentConversationId,
    dashboard.sortMode,
    hydrateConversation,
    refreshConversationList,
  ]);

  const setSortMode = useCallback(
    async (nextSortMode: SortMode) => {
      if (!currentConversationId) return;

      setError(null);
      try {
        await syncDashboard(currentConversationId, nextSortMode);
      } catch (nextError) {
        setError(
          nextError instanceof Error
            ? nextError.message
            : "更新排序失败。",
        );
      }
    },
    [currentConversationId, syncDashboard],
  );

  const deleteVersion = useCallback(
    async (versionId: string) => {
      if (!currentConversationId) return;
      try {
        await apiClient.deleteVersion(versionId);
        await syncDashboard(currentConversationId, dashboard.sortMode);
      } catch (nextError) {
        setError(
          nextError instanceof Error
            ? nextError.message
            : "删除版本失败。",
        );
      }
    },
    [currentConversationId, dashboard.sortMode, syncDashboard],
  );

  const deleteCandidate = useCallback(
    async (candidateId: string) => {
      if (!currentConversationId) return;
      try {
        await apiClient.deleteCandidate(candidateId);
        await syncDashboard(currentConversationId, dashboard.sortMode);
      } catch (nextError) {
        setError(
          nextError instanceof Error
            ? nextError.message
            : "删除候选窗口失败。",
        );
      }
    },
    [currentConversationId, dashboard.sortMode, syncDashboard],
  );

  const reorderCandidate = useCallback(
    async (candidateId: string, direction: "up" | "down") => {
      if (!currentConversationId || dashboard.sortMode !== "latest_first") {
        return;
      }

      const items = [...dashboard.candidateWindowPool];
      const currentIndex = items.findIndex((item) => item.id === candidateId);
      if (currentIndex < 0) return;

      const targetIndex =
        direction === "up" ? currentIndex - 1 : currentIndex + 1;
      if (targetIndex < 0 || targetIndex >= items.length) return;

      const reordered = [...items];
      const [current] = reordered.splice(currentIndex, 1);
      reordered.splice(targetIndex, 0, current);

      await apiClient.reorderCandidates(
        reordered.map((item, index) => ({
          id: item.id,
          displayOrder: reordered.length - index,
        })),
      );
      await syncDashboard(currentConversationId, dashboard.sortMode);
    },
    [currentConversationId, dashboard.candidateWindowPool, dashboard.sortMode, syncDashboard],
  );

  const toggleVersionInPool = useCallback(
    async (versionId: string, includedInCandidatePool: boolean) => {
      if (!currentConversationId) return;

      try {
        if (includedInCandidatePool) {
          await apiClient.includeVersion(versionId, true);
        } else {
          await apiClient.excludeVersion(versionId, false);
        }
        await syncDashboard(currentConversationId, dashboard.sortMode);
      } catch (nextError) {
        setError(
          nextError instanceof Error
            ? nextError.message
            : "更新版本候选状态失败。",
        );
      }
    },
    [currentConversationId, dashboard.sortMode, syncDashboard],
  );

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      if (offline) {
        setLoading(false);
        return;
      }
      setLoading(true);
      setError(null);
      void refreshQuota();
      try {
        const nextConversations = await refreshConversationList();
        if (cancelled) return;

        if (nextConversations.length === 0) {
          const created = await apiClient.createConversation("新会话");
          if (cancelled) return;
          await refreshConversationList();
          if (cancelled) return;
          await hydrateConversation(created.id, "latest_first");
          if (cancelled) return;
        } else {
          await hydrateConversation(nextConversations[0].id, "latest_first");
        }
      } catch (nextError) {
        if (!cancelled) {
          setError(
            nextError instanceof Error
              ? nextError.message
              : "初始化失败，请确认前后端服务均已启动。",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void bootstrap();

    return () => {
      cancelled = true;
    };
  }, [hydrateConversation, offline, refreshConversationList, refreshQuota]);

  const pendingChange = useMemo<PendingModelChange | null>(() => {
    const filters = workingMemory?.activeFilters as Record<string, unknown> | undefined;
    const pending = filters?.pendingChange;
    return pending && typeof pending === "object"
      ? pending as PendingModelChange
      : null;
  }, [workingMemory]);

  const value = useMemo<AgentStoreValue>(
    () => ({
      conversations,
      currentConversationId,
      currentView,
      workingMemory,
      messages,
      versions: dashboard.versions,
      candidateWindowPool: dashboard.candidateWindowPool,
      topRecommendations: dashboard.topRecommendations,
      sortMode: dashboard.sortMode,
      loading,
      sending,
      streamStatus,
      streamingMessageId,
      error,
      decisionArtifact,
      solveEvidence,
      activeJob,
      pendingChange,
      stages,
      degraded,
      clarificationOptions,
      engine,
      quota,
      quotaBlocked,
      createConversation,
      selectConversation,
      sendMessage,
      saveApiKey,
      clearApiKey,
      refreshQuota,
      dismissQuota,
      dismissClarification,
      setView: setCurrentView,
      setEngine,
      setSortMode,
      deleteVersion,
      deleteCandidate,
      reorderCandidate,
      toggleVersionInPool,
      refreshCurrentConversation,
      controlActiveJob,
    }),
    [
      conversations,
      createConversation,
      currentConversationId,
      currentView,
      dashboard.candidateWindowPool,
      dashboard.sortMode,
      dashboard.topRecommendations,
      dashboard.versions,
      deleteCandidate,
      deleteVersion,
      error,
      decisionArtifact,
      solveEvidence,
      activeJob,
      pendingChange,
      stages,
      degraded,
      clarificationOptions,
      engine,
      quota,
      quotaBlocked,
      saveApiKey,
      clearApiKey,
      refreshQuota,
      dismissQuota,
      dismissClarification,
      loading,
      messages,
      refreshCurrentConversation,
      controlActiveJob,
      reorderCandidate,
      selectConversation,
      sending,
      streamStatus,
      setSortMode,
      streamingMessageId,
      toggleVersionInPool,
      workingMemory,
    ],
  );

  return (
    <AgentStoreContext.Provider value={value}>
      {children}
    </AgentStoreContext.Provider>
  );
}

export function useAgentStore() {
  const context = useContext(AgentStoreContext);
  if (!context) {
    throw new Error("useAgentStore must be used within AgentStoreProvider");
  }
  return context;
}
