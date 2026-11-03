"""八句话验收脚本。

用法：
    python backend/eval_cases/verify_eight_sentences.py            # 只验证编译
    python backend/eval_cases/verify_eight_sentences.py --solve    # 连真实求解一起跑

判分口径：
  1. 每句话都要进入求解通道（is_planning / is_scenario_batch）；
  2. 每句话都要编译出至少一项改动，否则算没认出来；
  3. --solve 时，改动必须让求解结果相对基线发生变化；
     结果一模一样就说明这句改动是空转的（沉默失败）。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_coresys.stdout.reconfigure(encoding="utf-8")

from app.core.maintenance_product_executor import MaintenanceProductExecutor  # noqa: E402
from app.core.model_change import (  # noqa: E402
    build_change_plan,
    build_scenario_batch,
    is_scenario_batch,
)
from app.core.planning_intent import PlanningIntentCompiler, PlanningRequest  # noqa: E402

# 第 1、2 句原始说法是 7 月，但当前数据的规划窗口是 11/02–11/12，
# 7 月落在窗口外——那种情况系统会明确拒绝（正确行为，不是缺陷）。
# 所以验收用窗口内的日期，才能真正验证"能解决"。
EIGHT = [
    "叶片备件11月5日到货，11月5日之前不能开工",
    "严重故障11月8日前必须修完",
    "齿轮箱维修工时上涨20%",
    "船租改成50万",
    "最多两条作业线",
    "现金支出不超过500万",
    "T01必须修",
    "你先按默认方法算一遍，你再按每天只能工作8小时算一遍，然后对比这两版的方案",
]

# 第 8 句拆出来的场景，单独拿去跑求解
EIGHT_SCENARIOS = ["每天只能工作8小时"]


def compile_all():
    compiler = PlanningIntentCompiler()
    rows = []
    for index, text in enumerate(EIGHT, start=1):
        kind = "batch" if is_scenario_batch(text) else "single"
        if kind == "batch":
            batch = build_scenario_batch(text, PlanningRequest(), None, compiler)
            preview = batch.preview
            ok = not batch.blocked
            count = len(batch.scenarios)
        else:
            plan = build_change_plan(text, PlanningRequest(), None, compiler)
            preview = plan.preview
            ok = bool(plan.operations) and not plan.errors
            count = len(plan.operations)
        rows.append((index, text, kind, ok, count, preview))
    return rows


def solve_all(seconds: int = 40):
    executor = MaintenanceProductExecutor()
    compiler = PlanningIntentCompiler()
    cases: list[tuple[str, PlanningRequest]] = [("基线", PlanningRequest(seconds=seconds))]
    for index, text in enumerate(EIGHT, start=1):
        if is_scenario_batch(text):
            continue
        cases.append((f"{index}. {text[:20]}", replace(compiler.compile(text), seconds=seconds)))
    for label in EIGHT_SCENARIOS:
        cases.append((f"8. {label}", replace(compiler.compile(label), seconds=seconds)))
    results = {}
    for label, request in cases:
        try:
            report = executor.execute(request, f"job_verify_{abs(hash(label)) % 10**8}", f"verify-{label}")
            solving = report.get("求解", {})
            summary = report.get("推荐", {})
            results[label] = {
                "status": solving.get("解状态"),
                "cost": summary.get("总成本") or solving.get("模型成本"),
                "scheduled": summary.get("已安排任务数"),
                "tasks": summary.get("任务总数"),
                "vesselDays": summary.get("船天"),
            }
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            # 改动说不通被挡住，是正确行为，不是失败
            refused = ("不会影响结果" in text) or ("不在本次规划范围" in text)
            results[label] = (
                {"refused": text} if refused else {"error": text}
            )
    return results


def main() -> int:
    rows = compile_all()
    print("=" * 78)
    print("一、八句话的编译结果")
    print("=" * 78)
    failed = 0
    for index, text, kind, ok, count, preview in rows:
        flag = "通过" if ok else "没认出来"
        if not ok:
            failed += 1
        print(f"\n[{index}] {text}")
        print(f"    类型={kind}  {flag}  改动项={count}")
        for line in preview.splitlines():
            print(f"    {line}")
    print()
    print("=" * 78)
    print(f"编译：{len(rows) - failed}/{len(rows)} 句通过")
    print("=" * 78)

    if "--solve" not in sys.argv:
        print("\n（加 --solve 会再跑真实求解，验证改动确实改变了结果）")
        return 1 if failed else 0

    print("\n二、真实求解（每项 " + "40" + " 秒）")
    print("=" * 78)
    results = solve_all()
    baseline = results.get("基线", {})
    for label, item in results.items():
        if "refused" in item:
            print(f"{label:34s} 明确拒绝（正确）：{item['refused']}")
            continue
        if "error" in item:
            print(f"{label:34s} 执行失败：{item['error']}")
            continue
        print(
            f"{label:34s} 状态={item['status']} 成本={item['cost']} "
            f"任务={item['scheduled']}/{item['tasks']} 船天={item['vesselDays']}"
        )
    print()
    base_key = (baseline.get("cost"), baseline.get("scheduled"), baseline.get("vesselDays"))
    unchanged = []
    for label, item in results.items():
        if label == "基线" or "error" in item or "refused" in item:
            continue
        key = (item.get("cost"), item.get("scheduled"), item.get("vesselDays"))
        if key == base_key:
            unchanged.append(label)
    if unchanged:
        print("【可疑】以下改动跑出来的结果和基线完全一样，可能是空转：")
        for label in unchanged:
            print(f"    {label}")
    else:
        print("所有改动都让结果相对基线发生了变化，没有空转。")
    return 1 if (failed or unchanged) else 0


if __name__ == "__main__":
    raise SystemExit(main())
