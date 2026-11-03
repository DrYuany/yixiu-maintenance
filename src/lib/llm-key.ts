/**
 * 体验额度相关的前端本地状态：访客标识 + 访客自带的模型密钥。
 *
 * 两样东西都只存在浏览器 localStorage 里：
 * - 访客 ID 只用来让服务端数轮次，不包含任何个人信息；
 * - 密钥只随对话请求发给你自己部署的那台服务，服务端不落库、不写日志，
 *   仅在这一轮请求里转给模型。
 */

const KEY_STORAGE = "mwa.llm.apiKey";
const VISITOR_STORAGE = "mwa.visitor.id";

function safeStorage(): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage;
  } catch {
    return null;
  }
}

export function getStoredApiKey(): string {
  return (safeStorage()?.getItem(KEY_STORAGE) ?? "").trim();
}

export function setStoredApiKey(key: string | null): void {
  const storage = safeStorage();
  if (!storage) return;
  const next = (key ?? "").trim();
  if (next) storage.setItem(KEY_STORAGE, next);
  else storage.removeItem(KEY_STORAGE);
}

function randomId(): string {
  const cryptoObj = typeof crypto !== "undefined" ? crypto : undefined;
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
  return `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function getVisitorId(): string {
  const storage = safeStorage();
  if (!storage) return "anon";
  let id = storage.getItem(VISITOR_STORAGE);
  if (!id) {
    id = randomId();
    storage.setItem(VISITOR_STORAGE, id);
  }
  return id;
}

export const LLM_KEY_HEADER = "X-LLM-Api-Key";
export const VISITOR_HEADER = "X-Visitor-Id";
