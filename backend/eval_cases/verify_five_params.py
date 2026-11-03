"""五个新增参数的验收：能被识别、能进求解器、并且真的改变了结果。

用法：
    python backend/eval_cases/verify_five_params.py            # 只验证编译
    python backend/eval_cases/verify_five_params.py --solve     # 真求解，验证绑定性
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # repo root: dsl_core
sys.path.insert(0, str(ROOT / "backend"))

from app.core.planning_intent import PlanningIntentCompiler, PlanningRequest  # noqa: E402

# 五句原话 -> 期望字段名
FIVE = [
    ("人工单价改成 300 元", "人工单价（元每人时）", 300.0),
    ("人员总上限设为 2 人", "人员总上限", 2),
    ("风机间转移时间改成 1 小时", "风机间转移小时数", 1),
    ("每天 12 点开工", "每天维修开始时间", "12:00"),
    ("每天 14 点收工", "每天维修结束时间", "14:00"),
]

# 已有的三个参数，确认这次没被改坏
REGRESSION = [
    ("船租改成 50 万", "船租（元每艘每天）"),
    ("最多两条作业线", "最多作业线数"),
    ("每天最多维修 8 小时", "每天最多维修小时数"),
]


def compile_check() -> int:
    compiler = PlanningIntentCompiler()
    failures = 0
    for text, field, expected in FIVE:
        request = compiler.compile(text, PlanningRequest())
        got = request.overrides.get(field)
        ok = got == expected
        routable = compiler.is_planning_request(text)
        print(f"{'[OK] ' if ok and routable else '[FAIL]'} {text:22s} -> {field}={got!r} (期望 {expected!r}) 路由={routable}")
        if not (ok and routable):
            failures += 1
    for text, field in REGRESSION:
        request = compiler.compile(text, PlanningRequest())
        ok = field in request.overrides
        print(f"{'[OK] ' if ok else '[FAIL]'} 回归 {text:20s} -> {field}={request.overrides.get(field)!r}")
        if not ok:
            failures += 1
    return failures


def solve_check(seconds: int = 45) -> int:
    from app.core.maintenance_product_executor import MaintenanceProductExecutor

    compiler = PlanningIntentCompiler()
    executor = MaintenanceProductExecutor()
    baseline = executor.execute(PlanningRequest(seconds=seconds), "job_five_base", "five-base")
    base_solve = baseline.get("求解", {})
    base_cost = base_solve.get("模型成本")
    base_count = len((baseline.get("推荐") or {}).get("排程") or [])
    as_of = (baseline.get("数据") or {}).get("规划时点", "")
    print(
        f"\n规划时点：{as_of[:16]}（数值随日期变，跨天对比需用 PLANNING_AS_OF 钉住同一天）"
        f"\n基线：状态={base_solve.get('解状态')} 成本={base_cost} 安排={base_count} 个任务\n"
    )

    failures = 0
    for index, (text, field, _expected) in enumerate(FIVE, start=1):
        request = compiler.compile(text, PlanningRequest(seconds=seconds))
        try:
            # 输出目录名不能带空格，用序号而不是原话
            report = executor.execute(request, f"job_five_{index}", f"five-{index}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {text:22s} 求解抛错：{exc}")
            failures += 1
            continue
        solve = report.get(" solving", report.get("求解", {}))
        cost = solve.get("模型成本")
        count = len((report.get("推荐") or {}).get("排程") or [])
        changed = (cost != base_cost) or (count != base_count)
        print(
            f"{'[OK] ' if changed else '[--] '} {text:22s} 状态={solve.get('解状态')} "
            f"成本={cost} 安排={count} 个任务"
            + ("" if changed else "   ← 与基线相同")
        )
        if not changed:
            failures += 1
    return failures


if __name__ == "__main__":
    bad = compile_check()
    if "--solve" in sys.argv:
        bad += solve_check()
    print("\n结果：", "全部通过" if bad == 0 else f"{bad} 项未通过")
    sys.exit(0 if bad == 0 else 1)
