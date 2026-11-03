"""提问库评测：门控准确率 + DSL 类型准确率 + 抽样求解验证。

用法（在 dsl-prototype/ 目录下）：
    py -m eval.run_question_bank                # 全模式（自动读项目 .env.local，走真 LLM）
    py -m eval.run_question_bank --no-llm       # 只测规则层 + 意图门控（离线）
    py -m eval.run_question_bank --solve 8      # 另外对前 8 条 dsl 用例做真求解验证

判分口径：
  - 门控：解析出的 route 与期望一致（clarify 类接受"任何非 dsl 路由"）
  - 类型：dsl 用例的约束类型必须覆盖期望类型（允许额外类型，不允许缺失）
  - 求解：对抽样 dsl 用例真求解，断言约束在排程里真的成立
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from app.core.llm_parser import ParseUnavailable, load_library, parse  # noqa: E402


def _load_env() -> None:
    """把项目 .env.local 的 API key 注入环境（真 LLM 评测用）。"""
    import os

    env = ROOT.parent / ".env.local"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _route_ok(expected: str, actual: str) -> bool:
    if expected == "clarify":
        return actual != "dsl"  # 模糊诉求只要不自信产出 DSL 即可
    if expected == "dsl_or_clarify":
        return actual in ("dsl", "clarify")  # LLM 层模糊话术：两种解读都合理
    if expected == "accept_any":
        return True  # 已知硬边界：仅观察记录
    return actual == expected


def _types_ok(expected_types: list[str], result) -> bool:
    if not expected_types:
        return True
    if result.dsl is None:
        return False
    actual = {c.get("type") for c in result.dsl.get("constraints", [])}
    return set(expected_types) <= actual


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    no_llm = "--no-llm" in args
    solve_n = 0
    for i, arg in enumerate(args):
        if arg == "--solve" and i + 1 < len(args):
            solve_n = int(args[i + 1])

    if not no_llm:
        _load_env()

    library = load_library()
    rows = [
        json.loads(line)
        for line in (ROOT / "eval_cases" / "dsl_questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    gate_ok = 0
    type_ok = 0
    dsl_total = 0
    unavailable = 0
    auth_failed = False
    failures: list[str] = []

    for row in rows:
        try:
            result = parse(row["utterance"], library=library)
        except ParseUnavailable:
            unavailable += 1
            continue
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "401" in text or "Authentication" in text or "invalid" in text:
                # key 失效：摘掉环境变量，后续 LLM 用例即刻降级为 ParseUnavailable，
                # 规则层用例不受影响照常跑。
                import os

                os.environ.pop("MINIMAX_API_KEY", None)
                auth_failed = True
                unavailable += 1
                failures.append(f"LLM API key 失效（{text[:50]}…），LLM 层用例降级跳过")
                continue
            failures.append(f"{row['id']} 解析异常: {exc}")
            continue

        expected = row["route"]
        ok_gate = _route_ok(expected, result.route)
        if ok_gate:
            gate_ok += 1
        else:
            failures.append(f"{row['id']} 门控错: 期望 {expected} 得到 {result.route}（{row['utterance']}）")

        if expected == "dsl":
            dsl_total += 1
            if _types_ok(row.get("constraint_types", []), result):
                type_ok += 1
            else:
                failures.append(
                    f"{row['id']} 类型错: 期望 {row.get('constraint_types')} 得到 "
                    f"{[c.get('type') for c in (result.dsl or {}).get('constraints', [])]}（{row['utterance']}）"
                )

    total = len(rows)
    answered = total - unavailable
    print("=" * 72)
    print(f"提问库评测：{total} 条（dsl {dsl_total} 条）")
    if unavailable:
        print(f"LLM 不可用/未配置跳过 {unavailable} 条（--no-llm 或未配 key）")
    print(f"门控准确率（已解答 {answered} 条）：{gate_ok}/{answered} = {gate_ok / max(1, answered):.1%}")
    print(f"DSL 类型准确率：{type_ok}/{dsl_total} = {type_ok / max(1, dsl_total):.1%}")
    if failures:
        print(f"\n失败明细（前 30 条）：")
        for line in failures[:30]:
            print(f"  {line}")
    else:
        print("全部通过")

    if solve_n > 0:
        print("\n" + "=" * 72)
        print(f"抽样求解验证（前 {solve_n} 条 dsl 用例）：")
        solve_failures = _solve_verify(rows, library, solve_n)
        if solve_failures:
            for line in solve_failures:
                print(f"  {line}")
        else:
            print("求解验证全部通过")
        return 1 if failures or solve_failures else 0

    return 1 if failures else 0


def _solve_verify(rows: list[dict], library: list[dict], n: int) -> list[str]:
    """对抽样 dsl 用例：解析 → 真求解 → 断言约束生效。

    求解用 max_tasks=10 子集（完整模型短时限证不了可行性）；引用的任务编号
    重映射进子集（如 任务022 → 任务002），约束语义不变，只换具体任务。
    """
    from dsl_core.runner import run

    failures: list[str] = []
    count = 0
    for row in rows:
        if row.get("route") != "dsl" or not row.get("constraint_types"):
            continue
        count += 1
        if count > n:
            break
        try:
            result = parse(row["utterance"], library=library)
            if result.dsl is None:
                failures.append(f"{row['id']} 未产出 DSL")
                continue
            dsl = _remap_tasks(result.dsl)
            report = run(dsl, seconds=60, max_tasks=10)
            selected = report["排程"]
            if selected is None:
                failures.append(f"{row['id']} 求解未产生排程")
                continue
            for ctype in row.get("constraint_types", []):
                problem = _check_held(ctype, selected, dsl)
                if problem:
                    failures.append(f"{row['id']} {problem}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{row['id']} 求解异常: {exc}")
    return failures


def _remap_tasks(dsl: dict) -> dict:
    """把超出 max_tasks=10 子集的任务引用重映射进子集。"""
    import copy

    out = copy.deepcopy(dsl)

    def remap(ref: str) -> str:
        match = __import__("re").search(r"任务(\d{1,3})", str(ref))
        if not match:
            return ref
        num = int(match.group(1))
        if 1 <= num <= 10:
            return ref
        return f"任务{((num - 1) % 10) + 1:03d}"

    for spec in out.get("constraints", []):
        for key in ("task", "before", "after"):
            if key in spec:
                spec[key] = remap(spec[key])
        if "tasks" in spec:
            spec["tasks"] = [remap(t) for t in spec["tasks"]]
    return out


def _check_held(ctype: str, selected: list[dict], dsl: dict) -> str | None:
    """按类型断言约束在排程里成立（与 golden eval 同口径）。"""
    spec = next(c for c in dsl["constraints"] if c["type"] == ctype)
    if ctype == "co_occur":
        tasks = spec["tasks"]
        days = {t: next(c for c in selected if c["任务编号"] == t)["开始"] // 1440 for t in tasks}
        if days[tasks[0]] == days[tasks[1]]:
            return "co_occur 失效：两任务同一天"
    elif ctype == "precedence":
        before = next(c for c in selected if c["任务编号"] == spec["before"])
        after = next(c for c in selected if c["任务编号"] == spec["after"])
        if before["完成"] > after["开始"]:
            return "precedence 失效：前任务未先完成"
    elif ctype == "deadline":
        row = next(c for c in selected if c["任务编号"] == spec["task"])
        if row["完成"] > _minute(spec["time"]):
            return "deadline 失效：完工晚于期限"
    elif ctype == "earliest":
        row = next(c for c in selected if c["任务编号"] == spec["task"])
        if row["开始"] < _minute(spec["time"]):
            return "earliest 失效：开工早于最早时间"
    elif ctype == "fix":
        row = next(c for c in selected if c["任务编号"] == spec["task"])
        if row["作业线"] != spec["line"]:
            return "fix 失效：作业线不符"
    elif ctype == "forbidden_dates":
        days = {_minute(d) // 1440 for d in spec["dates"]}
        if any(c["开始"] // 1440 in days for c in selected):
            return "forbidden_dates 失效：禁开工日有开工"
    return None


_MINUTE_CACHE: dict[str, int] = {}


def _minute(time_str: str) -> int:
    if time_str not in _MINUTE_CACHE:
        from dsl_core.runner import _solver_modules

        load_project = _solver_modules()[0]
        data = load_project(ROOT.parent, base_id="基座01", as_of="2026-10-23T18:00:00+08:00")
        _MINUTE_CACHE[time_str] = data.minute(time_str)
    return _MINUTE_CACHE[time_str]


if __name__ == "__main__":
    raise SystemExit(main())
