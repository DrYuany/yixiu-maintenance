"""闸门分层归因计数：只统计，不判定。

一次变更请求从「用户原话」到「出方案」，中间会撞上几道门。这里把
「被哪道门拦下」归成可计数的标签，供复盘和离线测评出一张分布表。

只读现有函数的返回结构，不重复执行任何检查、不新增判定机制：

  意图违例  utterance.audit_utterance 没通过   —— 原话说了，DSL 没落点
  编译期拒  validator.validate_dsl 非空 / 数据补丁被拒 / 期限把任务挤掉
  求解未完  diagnose 结论「未算完」           —— UNKNOWN / TIME_LIMIT，不是无解
  真不可行  diagnose 结论「不可行」           —— 带已证明 / 上界证明的原因
  未能定位  diagnose 结论「未能定位」         —— 诚实单列，不硬塞进「真不可行」
  产物违例  reconcile 没通过                 —— DSL 说了，产物没做到（含独立复核失败）

分桶借 Shi et al. 的 Compiler-ER / Runtime-ER 口径，按本项目拆细：
  编译期桶：意图违例 + 编译期拒（求解之前就被挡下）
  求解期桶：求解未完 + 真不可行 + 未能定位 + 产物违例（求解时及之后）

三条纪律（和 diagnose.py 同一套）：
- 一次结果按「先撞到的那道门」归因；没被拦下写「未拦截」。
- 认不出来的写「未归类」，不猜、不硬塞。
- 原设计文档只列了五类，但 ``diagnose`` 实际有**三种**结论（不可行 / 未算完 /
  未能定位）。「未能定位」单列第六类：把归不了因的失败算进「真不可行」，
  正是状态诚实性要防的那件事。

「没通过」的口径：reconcile / utterance 都带 ``通过`` 字段，runner 也是按
``违例 or 未核对`` 拦的，两者等价。所以这里统一看 ``通过``；缺该字段时退回
看 ``违例/未核对``，兼容手工拼的结构。注意「未核对」不是「违例」，是证据空白，
强度不同——分层计数只回答落在哪道门，不混淆这两者。

命令行（从仓库根跑，读一行一个样本的 jsonl）：
    py -m dsl_core.gate_metrics backend/eval_cases/gate_breakdown.jsonl
每行可直接给 ``gate`` 标签，或给结构化信号现算：
    {"id": "c1", "compile_errors": ["未知原语"]}
    {"id": "c2", "diagnosis": {"结论": "未算完"}}
    {"id": "c3", "reconcile_audit": {"通过": false, "违例": [...]}}
    {"id": "c4", "utterance_audit": {"通过": false, "违例": [...]}}
    {"id": "c5", "passed": true}
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

INTENT_VIOLATION = "意图违例"
COMPILE_REJECT = "编译期拒"
SOLVE_INCOMPLETE = "求解未完"
PROVEN_INFEASIBLE = "真不可行"
UNLOCATED = "未能定位"
ARTIFACT_VIOLATION = "产物违例"

NOT_BLOCKED = "未拦截"
UNCLASSIFIED = "未归类"

# 归因标签（按门的实际先后顺序排列，不是按字母）
GATE_CLASSES = (
    INTENT_VIOLATION,
    COMPILE_REJECT,
    SOLVE_INCOMPLETE,
    PROVEN_INFEASIBLE,
    UNLOCATED,
    ARTIFACT_VIOLATION,
)
ALL_LABELS = (*GATE_CLASSES, NOT_BLOCKED, UNCLASSIFIED)

COMPILER_BUCKET = "编译期桶"
RUNTIME_BUCKET = "求解期桶"

_BUCKET_OF = {
    INTENT_VIOLATION: COMPILER_BUCKET,
    COMPILE_REJECT: COMPILER_BUCKET,
    SOLVE_INCOMPLETE: RUNTIME_BUCKET,
    PROVEN_INFEASIBLE: RUNTIME_BUCKET,
    UNLOCATED: RUNTIME_BUCKET,
    ARTIFACT_VIOLATION: RUNTIME_BUCKET,
}

# runner 抛 DslError 时，错误文本里能区分产物层的前缀
_ARTIFACT_PREFIXES = ("意图对账未通过", "求解结果未通过独立复核")


def _blocked(audit: dict[str, Any] | None) -> bool:
    """这份对账结果会不会被闸门拦下。"""
    if not audit:
        return False
    if "通过" in audit:
        return not bool(audit["通过"])
    return bool(audit.get("违例") or audit.get("未核对"))


def classify(
    *,
    utterance_audit: dict[str, Any] | None = None,
    compile_errors: list[str] | None = None,
    diagnosis: dict[str, Any] | None = None,
    reconcile_audit: dict[str, Any] | None = None,
    passed: bool = False,
) -> str:
    """把一次结果归到「先撞到的那道门」，一次只落一个标签。

    参数都是现成函数的返回值，调用方不必重跑任何检查。什么都不给且不
    标 ``passed`` 时返回「未归类」——没证据就别下结论。
    """
    # 1. 原话 ↔ DSL。这道门在预览/执行层，比编译期校验更早。
    if _blocked(utterance_audit):
        return INTENT_VIOLATION
    # 2. 编译期确定性校验 / 补丁落地被拒。
    if compile_errors:
        return COMPILE_REJECT
    # 3. 求解诊断。三种结论分开判，「未能定位」绝不算成不可行。
    if diagnosis:
        verdict = str(diagnosis.get("结论") or "")
        if verdict == "未算完":
            return SOLVE_INCOMPLETE
        if verdict == "不可行":
            return PROVEN_INFEASIBLE
        if verdict == "未能定位":
            return UNLOCATED
        return UNCLASSIFIED
    # 4. 产物对账（求解成功之后才会跑到）。
    if _blocked(reconcile_audit):
        return ARTIFACT_VIOLATION
    if passed:
        return NOT_BLOCKED
    return UNCLASSIFIED


def from_dsl_error(exc: Any) -> str:
    """从 ``runner.run()`` 抛出的 ``DslError`` 反推归因（便捷入口）。

    ``DslError`` 只有 ``errors`` 文本和可选的 ``diagnosis``。带 diagnosis 的
    是求解期失败，errors 只是给人看的描述，不能算编译期拒；文本带
    「意图对账未通过」或「独立复核」前缀的是产物层；其余才是编译期拒。

    意图违例（原话 ↔ DSL）不在 runner 里抛，要走
    ``classify(utterance_audit=...)``。
    """
    errors = list(getattr(exc, "errors", None) or [])
    diagnosis = getattr(exc, "diagnosis", None)
    text = "；".join(errors)

    if diagnosis:
        return classify(diagnosis=diagnosis)
    if any(prefix in text for prefix in _ARTIFACT_PREFIXES):
        return classify(reconcile_audit={"通过": False, "违例": [text]})
    if errors:
        return classify(compile_errors=errors)
    return UNCLASSIFIED


def classify_records(records: Iterable[dict[str, Any]]) -> list[str]:
    """批量归因：每行可直接带 ``gate`` 标签，也可带结构化信号现算。"""
    labels: list[str] = []
    for record in records:
        if record.get("gate"):
            labels.append(str(record["gate"]))
            continue
        labels.append(
            classify(
                utterance_audit=record.get("utterance_audit"),
                compile_errors=record.get("compile_errors"),
                diagnosis=record.get("diagnosis"),
                reconcile_audit=record.get("reconcile_audit"),
                passed=bool(record.get("passed")),
            )
        )
    return labels


def tally(labels: Iterable[str]) -> dict[str, Any]:
    """一批归因标签 → 分布表：按类计数/占比 + 按桶计数/占比。

    未知标签不静默丢掉：单列「未识别标签」，否则拼错一个字符串会被算成
    0 次还看不出来。
    """
    items = list(labels)
    total = len(items)
    counts = {label: 0 for label in ALL_LABELS}
    # 未知标签只进「未识别标签」，不混进按类计数——否则拼错的字符串会被
    # 悄悄算成一类，还看不出来。占比分母仍是总数，和小于 1 正好暴露问题。
    unknown = sorted({label for label in items if label not in counts})
    for label in items:
        if label in counts:
            counts[label] += 1

    buckets = {COMPILER_BUCKET: 0, RUNTIME_BUCKET: 0}
    for label in items:
        bucket = _BUCKET_OF.get(label)
        if bucket:
            buckets[bucket] += 1

    def share(count: int) -> float:
        # 不提前 round：占比是要给别人复算和再聚合的，舍入只在展示时做。
        return count / total if total else 0.0

    return {
        "总数": total,
        "被拦下": total - counts.get(NOT_BLOCKED, 0),
        "按类计数": counts,
        "按类占比": {label: share(count) for label, count in counts.items()},
        "按桶计数": buckets,
        "按桶占比": {bucket: share(count) for bucket, count in buckets.items()},
        "未识别标签": unknown,
    }


def format_tally(report: dict[str, Any]) -> str:
    """分布表的一行人类版（日志、复盘报告用）。"""
    total = int(report.get("总数") or 0)
    if not total:
        return "闸门分布：没有样本"
    lines = [f"闸门分布：共 {total} 次，被拦下 {report.get('被拦下', 0)} 次"]
    counts = report.get("按类计数") or {}
    shares = report.get("按类占比") or {}
    for label in ALL_LABELS:
        count = counts.get(label, 0)
        if count:
            lines.append(f"  {label}：{count} 次（{shares.get(label, 0) * 100:.1f}%）")
    buckets = report.get("按桶计数") or {}
    lines.append(
        f"  分桶：{COMPILER_BUCKET} {buckets.get(COMPILER_BUCKET, 0)}，"
        f"{RUNTIME_BUCKET} {buckets.get(RUNTIME_BUCKET, 0)}"
    )
    unknown = report.get("未识别标签") or []
    if unknown:
        lines.append("  未识别标签（请核对拼写）：" + "、".join(unknown))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="闸门失败原因分层计数（复盘/测评）")
    parser.add_argument("cases", type=Path, help="一行一个样本的 jsonl")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 分布表")
    args = parser.parse_args(argv)

    records = [
        json.loads(line)
        for line in args.cases.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = tally(classify_records(records))
    print(format_tally(report))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    unknown = report.get("未识别标签") or []
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
