import { MessageSquarePlus, Search, Sparkles } from "lucide-react";
import type { Conversation } from "../../store/types";
import { PRODUCT_NAME, PRODUCT_TAGLINE } from "../../lib/product";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  onCreate: () => void;
  onSelect: (id: string) => void;
}

export function ConversationRail({ conversations, activeId, onCreate, onSelect }: Props) {
  return (
    <aside className="agent-rail" aria-label="会话列表">
      <div className="agent-brand">
        <span><Sparkles size={18} /></span>
        <div><strong>{PRODUCT_NAME}</strong><small>{PRODUCT_TAGLINE}</small></div>
      </div>
      <button className="new-chat-button" onClick={onCreate}><MessageSquarePlus size={16} />新建排程会话</button>
      <label className="agent-search"><Search size={15} /><input aria-label="搜索会话" placeholder="搜索会话" /></label>
      <div className="rail-section-title">最近会话</div>
      <div className="conversation-list">
        {conversations.map((item) => (
          <button key={item.id} className={item.id === activeId ? "active" : ""} onClick={() => onSelect(item.id)}>
            <strong>{item.title}</strong><span>{item.lastMessagePreview || "尚无消息"}</span>
          </button>
        ))}
      </div>
      <div className="rail-footer"><span className="online-dot" />确定性编译 v1 · 审计模式</div>
    </aside>
  );
}
