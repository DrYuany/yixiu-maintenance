"""行级数据补丁的绑定性验证（行为验证）。

原理来自 ReLoop 的行为测试：把参数推到极端，看目标值动不动。
动 = 约束真的进了模型；不动 = 这条改动是空转的。

这里专测三句最容易空转的话：
  1. 备件到货      —— 只有叶片任务本来会被选中时，到货才影响结果
  2. 任务期限      —— 只有给的日期早于原期限时才会收紧
  3. 现金支出上限  —— 上限必须真的卡住方案才有意义

用法：
    python backend/eval_cases/verify_patch_binding.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_coresys.stdout.reconfigure(encoding="utf-8")

from app.core.maintenance_product_executor import MaintenanceProductExecutor  # noqa: E402
from app.core.planning_intent import PlanningIntentCompiler, PlanningRequest  # noqa: E402

SECONDS = 60

CASES = [
    ("对照组：只必修任务001", "T01必须修"),
    ("实验组：必修任务001 + 叶片11月5日到货", "T01必须修，叶片备件11月5日到货，11月5日之前不能开工"),
    ("期限：高严重任务11月8日前修完", "严重故障11月8日前必须修完"),
    ("现金：现金支出不超过400万", "现金支出不超过400万"),
]


def run() -> None:
    compiler = PlanningIntentCompiler()
    executor = MaintenanceProductExecutor()
    rows = []
    for label, text in CASES:
        compiled = compiler.compile_detailed(text)
        request = replace(compiled.request, seconds=SECONDS)
        try:
            report = executor.execute(
                request, f"job_bind_{abs(hash(label)) % 10**8}", f"bind-{label}"
            )
            solving = report.get("求解", {})
            schedule = (report.get("推荐") or {}).get("排程") or []
            first = sorted(schedule, key=lambda item: item["开始"])[:3]
            rows.append((
                label,
                solving.get("解状态"),
                solving.get("模型成本"),
                [(item["任务编号"], item["部件名称"], item["开始"], item["完成"]) for item in first],
            ))
        except Exception as exc:  # noqa: BLE001
            rows.append((label, "拒绝", None, [(str(exc), "", 0, 0)]))

    print("=" * 92)
    print(f"绑定性验证（每项最长 {SECONDS} 秒）")
    print("=" * 92)
    for label, status, cost, first in rows:
        print(f"\n{label}")
        print(f"    解状态={status}  成本={cost}")
        for row in first:
            if isinstance(row[0], str) and len(row) == 4 and row[3] == 0:
                print(f"    {row[0]}")
            else:
                print(f"    {row[0]} {row[1]} 开始={row[2]} 完成={row[3]}")
    print()
    print("=" * 92)
    status_map = {label: status for label, status, _, _ in rows}
    cost_map = {label: cost for label, _, cost, _ in rows}
    base = "对照组：只必修任务001"
    if cost_map.get(base) is not None:
        other = "实验组：必修任务001 + 叶片11月5日到货"
        if cost_map.get(other) == cost_map.get(base):
            print("【可疑】加了到货限制后成本与对照组完全相同，叶片约束没有 binding")
        else:
            print("到货约束已 binding：加了到货限制后，方案成本/排期发生了变化")
    for label, status in status_map.items():
        if status == "拒绝":
            print(f"【明确拒绝】{label}")


if __name__ == "__main__":
    run()
