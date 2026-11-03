import { Clock3 } from "lucide-react";

export type SolveWaitAction = "wait" | "background" | "use_incumbent" | "stop";

interface Props {
  open: boolean;
  elapsedSeconds: number;
  hasIncumbent: boolean;
  objectiveValue?: number;
  bestBound?: number;
  onAction: (action: SolveWaitAction) => void;
}

export function SolveWaitDialog({ open, elapsedSeconds, hasIncumbent, objectiveValue, bestBound, onAction }: Props) {
  if (!open) return null;
  return (
    <div className="solve-dialog-backdrop" role="presentation">
      <section className="solve-dialog" role="dialog" aria-modal="true" aria-labelledby="solve-dialog-title">
        <div className="solve-dialog-icon"><Clock3 /></div>
        <h2 id="solve-dialog-title">求解仍在进行</h2>
        <p>已运行 {Math.floor(elapsedSeconds / 60)} 分钟。求解器仍在搜索，当前状态未证明不可行。</p>
        {hasIncumbent ? (
          <div className="solve-metrics">
            <span>当前可行成本<strong>¥{objectiveValue?.toLocaleString("zh-CN")}</strong></span>
            <span>当前最优界<strong>¥{bestBound?.toLocaleString("zh-CN")}</strong></span>
          </div>
        ) : <div className="solve-note">目前尚未找到可行方案，求解器仍在继续。</div>}
        <div className="solve-dialog-actions">
          <button onClick={() => onAction("wait")}>继续等待</button>
          <button onClick={() => onAction("background")}>后台运行</button>
          {hasIncumbent ? <button onClick={() => onAction("use_incumbent")}>使用当前方案</button> : null}
          <button className="danger" onClick={() => onAction("stop")}>停止求解</button>
        </div>
      </section>
    </div>
  );
}
