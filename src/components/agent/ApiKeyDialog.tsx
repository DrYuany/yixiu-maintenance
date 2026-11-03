import { useEffect, useState } from "react";
import { ExternalLink, KeyRound, X } from "lucide-react";

interface Props {
  open: boolean;
  /** 当前浏览器里是否已经存着密钥 */
  hasKey: boolean;
  /** 免费额度上限，用于文案 */
  limit: number;
  onClose: () => void;
  onSubmit: (key: string) => Promise<void>;
  onClear: () => Promise<void>;
}

/**
 * 免费轮次用完后的出路：填自己的 DeepSeek 密钥。
 *
 * 密钥只存在本机 localStorage，随对话请求发给我们自己部署的这台服务，
 * 服务端只在当前这一轮转给模型，不落库、不写日志。文案里要把这句说清楚，
 * 否则没人敢往里贴 key。
 */
export function ApiKeyDialog({
  open,
  hasKey,
  limit,
  onClose,
  onSubmit,
  onClear,
}: Props) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setValue("");
      setError(null);
      setSubmitting(false);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    const key = value.trim();
    if (key.length < 12) {
      setError("密钥看起来不完整，通常以 sk- 开头，长度 30 位以上。");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(key);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "保存失败，请重试。");
      setSubmitting(false);
    }
  };

  return (
    <div
      className="api-key-overlay"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="api-key-dialog" role="dialog" aria-modal="true" aria-label="填入 API 密钥">
        <header>
          <div className="api-key-title">
            <KeyRound size={16} />
            <strong>{hasKey ? "更换模型密钥" : "用自己的密钥继续"}</strong>
          </div>
          <button type="button" aria-label="关闭" onClick={onClose}>
            <X size={15} />
          </button>
        </header>

        <p className="api-key-desc">
          {hasKey
            ? "当前对话已经用你自己的 DeepSeek 密钥，额度不受限。"
            : `免费体验的 ${limit} 轮对话已经用完。填入你自己的 DeepSeek API Key，可以继续使用，轮次不限。`}
        </p>

        <label className="api-key-field">
          <span>DeepSeek API Key</span>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="sk-..."
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void submit();
              }
            }}
          />
        </label>

        {error ? <div className="api-key-error">{error}</div> : null}

        <div className="api-key-hint">
          密钥只保存在你这台设备的浏览器里，不会存到服务器，也只用于你自己的对话。
          <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">
            去 DeepSeek 控制台创建 <ExternalLink size={11} />
          </a>
        </div>

        <footer>
          {hasKey ? (
            <button
              type="button"
              className="api-key-remove"
              disabled={submitting}
              onClick={() => void onClear()}
            >
              移除已保存的密钥
            </button>
          ) : (
            <span />
          )}
          <div className="api-key-actions">
            <button type="button" onClick={onClose} disabled={submitting}>取消</button>
            <button
              type="button"
              className="primary"
              onClick={() => void submit()}
              disabled={submitting || !value.trim()}
            >
              {submitting ? "验证中…" : "保存并继续"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
