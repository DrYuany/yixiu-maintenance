import type {
  AgentResponse,
  Conversation,
  ConversationDetail,
  DashboardResponse,
  ModelArtifact,
  QuotaInfo,
  SortMode,
  SolveJob,
  StageEvent,
} from "@/store/types";
import {
  LLM_KEY_HEADER,
  VISITOR_HEADER,
  getStoredApiKey,
  getVisitorId,
} from "@/lib/llm-key";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "";

export interface StreamHandlers {
  onToken?: (text: string) => void;
  onReplace?: (text: string) => void;
  onStatus?: (label: string) => void;
  onStage?: (stage: StageEvent) => void;
}

/** 带上访客标识；只有填过密钥才带密钥头。 */
function identityHeaders(): Record<string, string> {
  const headers: Record<string, string> = { [VISITOR_HEADER]: getVisitorId() };
  const key = getStoredApiKey();
  if (key) headers[LLM_KEY_HEADER] = key;
  return headers;
}

/** 带状态码与业务错误码的异常，前端据此区分"额度用完"和普通失败。 */
export class ApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function isQuotaError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "quota_exhausted";
}

async function toApiError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let message = text || `Request failed: ${response.status}`;
  let code: string | undefined;
  try {
    const parsed = JSON.parse(text) as { detail?: unknown; code?: unknown };
    if (typeof parsed.code === "string") code = parsed.code;
    const detail = parsed.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const nested = detail as { detail?: unknown; message?: unknown; code?: unknown };
      if (typeof nested.code === "string") code = code ?? nested.code;
      if (typeof nested.message === "string") message = nested.message;
      else if (typeof nested.detail === "string") message = nested.detail;
      else message = JSON.stringify(detail);
    }
  } catch {
    // 不是 JSON（隧道错误页之类），保留原始文本
  }
  return new ApiError(message, response.status, code);
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...identityHeaders(),
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const apiClient = {
  getQuota: () => request<QuotaInfo>("/api/access/quota"),
  verifyApiKey: (key: string) =>
    request<{ ok: boolean }>("/api/access/verify-key", {
      method: "POST",
      headers: { [LLM_KEY_HEADER]: key },
    }),
  listConversations: () => request<Conversation[]>("/api/conversations"),
  createConversation: (title: string) =>
    request<Conversation>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  getConversation: (conversationId: string) =>
    request<ConversationDetail>(`/api/conversations/${conversationId}`),
  sendMessage: (
    conversationId: string,
    message: string,
    selectedVersionId?: string | null,
    selectedWindowId?: string | null,
    engine: "llm" | "rule" = "llm",
  ) =>
    request<AgentResponse>("/api/agent/message", {
      method: "POST",
      body: JSON.stringify({
        conversationId,
        message,
        selectedVersionId: selectedVersionId ?? null,
        selectedWindowId: selectedWindowId ?? null,
        engine,
      }),
    }),
  sendMessageStream: async (
    conversationId: string,
    message: string,
    selectedVersionId: string | null | undefined,
    selectedWindowId: string | null | undefined,
    handlers: StreamHandlers,
    engine: "llm" | "rule" = "llm",
  ): Promise<AgentResponse> => {
    const response = await fetch(`${API_BASE_URL}/api/agent/message/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...identityHeaders() },
      body: JSON.stringify({
        conversationId,
        message,
        selectedVersionId: selectedVersionId ?? null,
        selectedWindowId: selectedWindowId ?? null,
        engine,
      }),
    });
    if (!response.ok) {
      throw await toApiError(response);
    }
    if (!response.body) {
      throw new ApiError("浏览器不支持流式响应", response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const outcome: { response: AgentResponse | null; error: string | null } = {
      response: null,
      error: null,
    };

    const dispatch = (frame: string) => {
      let event = "message";
      const dataLines: string[] = [];
      for (const rawLine of frame.split(/\r?\n/)) {
        if (rawLine.startsWith("event:")) {
          event = rawLine.slice(6).trim();
        } else if (rawLine.startsWith("data:")) {
          dataLines.push(rawLine.slice(5).trimStart());
        }
      }
      if (!dataLines.length) return;
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
      } catch {
        return;
      }
      if (event === "token" && typeof data.text === "string") {
        handlers.onToken?.(data.text);
      } else if (event === "replace" && typeof data.text === "string") {
        handlers.onReplace?.(data.text);
      } else if (event === "status" && typeof data.label === "string") {
        handlers.onStatus?.(data.label);
      } else if (event === "stage" && typeof data.id === "string" && typeof data.label === "string") {
        handlers.onStage?.(data as unknown as StageEvent);
      } else if (event === "done") {
        outcome.response = data as unknown as AgentResponse;
      } else if (event === "error") {
        outcome.error = typeof data.message === "string" ? data.message : "流式请求失败";
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        dispatch(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) dispatch(buffer);
    if (outcome.error) throw new Error(outcome.error);
    if (!outcome.response) throw new Error("流式响应未返回最终结果");
    return outcome.response;
  },
  getModelArtifact: (versionId: string) =>
    request<ModelArtifact>(`/api/versions/${versionId}/model`),
  getSolveJob: (jobId: string) =>
    request<SolveJob>(`/api/agent/jobs/${jobId}`),
  getLatestSolveJob: (conversationId: string) =>
    request<SolveJob | null>(`/api/agent/conversations/${conversationId}/jobs/latest`),
  getSolveJobs: (conversationId: string) =>
    request<SolveJob[]>(`/api/agent/conversations/${conversationId}/jobs`),
  controlSolveJob: (jobId: string, action: "wait" | "background" | "use_incumbent" | "stop") =>
    request<SolveJob>(`/api/agent/jobs/${jobId}/actions`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  getDashboard: (conversationId: string, sortMode: SortMode) =>
    request<DashboardResponse>(
      `/api/dashboard/${conversationId}?sortMode=${sortMode}`,
    ),
  deleteVersion: (versionId: string) =>
    request<void>(`/api/versions/${versionId}`, { method: "DELETE" }),
  deleteCandidate: (candidateId: string) =>
    request<void>(`/api/candidates/${candidateId}`, { method: "DELETE" }),
  reorderCandidates: (
    items: Array<{ id: string; displayOrder: number }>,
  ) =>
    request<void>("/api/candidates/reorder", {
      method: "PATCH",
      body: JSON.stringify({ items }),
    }),
  includeVersion: (versionId: string, includedInCandidatePool: boolean) =>
    request<void>(`/api/versions/${versionId}/include`, {
      method: "POST",
      body: JSON.stringify({ includedInCandidatePool }),
    }),
  excludeVersion: (versionId: string, includedInCandidatePool: boolean) =>
    request<void>(`/api/versions/${versionId}/exclude`, {
      method: "POST",
      body: JSON.stringify({ includedInCandidatePool }),
    }),
};
