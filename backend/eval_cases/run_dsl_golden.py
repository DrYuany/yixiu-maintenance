"""Golden 判分脚本：手写 DSL 用例 → 真求解 → 业务断言。

用法（在 dsl-prototype/ 目录下）：
    py -m eval.run_golden        # 跑全部用例，exit 0 = 全部通过

判分口径与 verify_eight_sentences.py 一致：不只验证"能跑通"，
而是断言求解结果真的满足约束（不同天/先后/最早开工）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from dsl_core.runner import DslError, run  # noqa: E402


def _check(case: dict, selected: list[dict], data) -> str | None:
    kind = case["check"]
    tasks = case["tasks"]
    rows = {t: next(c for c in selected if c["任务编号"] == t) for t in tasks}
    if kind == "diff_day":
        if rows[tasks[0]]["开始"] // 1440 == rows[tasks[1]]["开始"] // 1440:
            return "两个任务落在了同一天"
    elif kind == "finish_before_start":
        if rows[tasks[0]]["完成"] > rows[tasks[1]]["开始"]:
            return "前任务未先于后任务完成"
    elif kind == "start_after":
        minute = data.minute(case.get("time") or "2026-11-05T08:00:00+08:00")
        if rows[tasks[0]]["开始"] < minute:
            return "开工早于指定最早时间"
    else:
        return f"未知检查 {kind!r}"
    return None


def main() -> int:
    from dsl_core.runner import _solver_modules

    load_project = _solver_modules()[0]
    project = ROOT.parent
    data = load_project(project, base_id="基座01", as_of="2026-10-23T18:00:00+08:00")

    cases_path = ROOT / "eval_cases" / "dsl_golden_cases.jsonl"
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    failed = 0
    for case in cases:
        try:
            report = run(case["dsl"], seconds=60, max_tasks=10)
        except DslError as exc:
            print(f"[FAIL] {case['id']} ({case['name']}): DSL 校验失败: {exc.errors}")
            failed += 1
            continue
        selected = report["排程"]
        if selected is None:
            print(f"[FAIL] {case['id']} ({case['name']}): 求解未产生排程")
            failed += 1
            continue
        problem = _check(case, selected, data)
        if problem:
            print(f"[FAIL] {case['id']} ({case['name']}): {problem}")
            failed += 1
        else:
            print(f"[OK]   {case['id']} ({case['name']}): 约束生效，状态={report['求解']['解状态']}")

    print()
    print(f"golden: {len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
