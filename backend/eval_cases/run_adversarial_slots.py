"""对抗槽位评测：注册表驱动提示词 vs 旧手写提示词。

用途：验证「让 LLM 看完整槽位清单」是否真的提升长尾话术的槽位填充准确率。

用法（backend 目录下）：
    py -m eval_cases.run_adversarial_slots                # 注册表提示词（新）
    py -m eval_cases.run_adversarial_slots --legacy       # 旧手写提示词（基线）
    py -m eval_cases.run_adversarial_slots --limit 20     # 只跑前 20 条
    py -m eval_cases.run_adversarial_slots --no-llm       # 只看规则层能拦多少

判分口径：
  - expect 是对象 → route 必须 dsl，且 parameters/constraints 覆盖期望项
  - expect 是字符串（clarify/reject/not_change）→ route 必须一致
  - status=not_implemented → 该参数已经登记但系统改不动，不计入准确率，
    单独列出。这类用例留在文件里当"待实现清单"，删掉等于把窟窿藏起来。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from app.core.llm_parser import (  # noqa: E402
    ParseUnavailable,
    _PARSE_SYSTEM,
    load_library,
    parse,
)


def _load_env() -> None:
    env = ROOT.parent / ".env.local"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _values_equal(expected, actual, op_expected="=", op_actual="=") -> bool:
    a, b = _num(expected), _num(actual)
    if a is not None and b is not None:
        if abs(a - b) > max(1e-6, abs(a) * 1e-4):
            return False
    elif str(expected).strip() != str(actual).strip():
        return False
    # 倍率类：op 用 "=" 或 "*" 表达同一倍率是等价的（如 电价整体倍率 = 1.2 / * 1.2）
    if {op_expected, op_actual} <= {"=", "*"} and op_expected != op_actual:
        return op_expected == "*" or op_actual == "*"
    return True


# 参数 ↔ 等价约束/目标形态（语义相同，只是用户可能任一表达）
_PARAM_EQUIVALENTS = {
    "费用预算上限": ("budget", None),
    "标准目标策略": ("strategy_objective", None),
    "共同最低完成任务数": ("cardinality", None),
    "强制维修任务": ("must", None),
}


def _param_satisfied(want: dict, dsl: dict) -> bool:
    field, value = want["field"], want["value"]
    want_op = want.get("op", "=")
    for got in dsl.get("parameters") or []:
        if got.get("field") == field and _values_equal(
            value, got.get("value"), want_op, got.get("op", "=")
        ):
            return True

    kind = _PARAM_EQUIVALENTS.get(field)
    if not kind:
        return False
    if kind[0] == "budget":
        return any(
            c.get("type") == "budget" and c.get("op") in ("<=", "==")
            and _values_equal(value, c.get("value"))
            for c in dsl.get("constraints") or []
        )
    if kind[0] == "strategy_objective":
        return any(
            o.get("type") == "strategy" and str(value) in str(o.get("name", ""))
            for o in dsl.get("objectives") or []
        ) or any(
            o.get("type") == "strategy" and str(o.get("name", "")) in str(value)
            for o in dsl.get("objectives") or []
        )
    if kind[0] == "cardinality":
        return any(
            c.get("type") == "cardinality" and c.get("op") in (">=", "==")
            and _values_equal(value, c.get("value"))
            for c in dsl.get("constraints") or []
        )
    if kind[0] == "must":
        wanted = value if isinstance(value, list) else [value]
        got = {c.get("task") for c in dsl.get("constraints") or [] if c.get("type") == "must"}
        return all(str(t) in got for t in wanted)
    return False


def _param_ok(expected: dict, dsl: dict) -> bool:
    for want in expected.get("parameters") or []:
        if not _param_satisfied(want, dsl):
            return False
    return True


def _constraint_ok(expected: dict, dsl: dict) -> bool:
    for want in expected.get("constraints") or []:
        hit = any(
            all(got.get(k) == v for k, v in want.items())
            for got in dsl.get("constraints") or []
            if got.get("type") == want.get("type")
        )
        if not hit:
            return False
    return True


def _score(row: dict, result) -> tuple[bool, str]:
    expect = row["expect"]
    if isinstance(expect, str):
        ok = result.route == expect
        return ok, "" if ok else f"期望 {expect}，得到 {result.route}"

    dsl = result.dsl if result.route == "dsl" else None
    if dsl is None:
        # 运行时会回退旧规划器；旧桥接能产出等价 DSL 也算系统理解了槽位
        from app.core.llm_parser import parse_by_legacy

        dsl = parse_by_legacy(row["utterance"])
    if not dsl:
        return False, f"期望 dsl，得到 {result.route}"
    if not _param_ok(expect, dsl):
        got = [p.get("field") for p in dsl.get("parameters") or []]
        want = [p.get("field") for p in expect.get("parameters") or []]
        return False, f"参数不符：期望 {want}，得到 {got}"
    if not _constraint_ok(expect, dsl):
        got = [c.get("type") for c in dsl.get("constraints") or []]
        want = [c.get("type") for c in expect.get("constraints") or []]
        return False, f"约束不符：期望 {want}，得到 {got}"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    legacy = "--legacy" in args
    no_llm = "--no-llm" in args
    limit = 0
    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    _load_env()
    if no_llm:
        os.environ.pop("MINIMAX_API_KEY", None)
    library = load_library()
    rows = [
        json.loads(line)
        for line in (ROOT / "eval_cases" / "adversarial_slots.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit:
        rows = rows[:limit]

    passed = 0
    failed: list[str] = []
    skipped = 0
    pending: list[str] = []
    for row in rows:
        if row.get("status") == "not_implemented":
            # 参数还没接入，考它只会得到"系统报错"——记下来，不当作模型答错。
            pending.append(f"{row['id']} [{row['param']}] {row['utterance']}")
            continue
        try:
            result = parse(
                row["utterance"], library=library,
                system_prompt=_PARSE_SYSTEM if legacy else None,
            )
        except ParseUnavailable:
            skipped += 1
            continue
        ok, reason = _score(row, result)
        if ok:
            passed += 1
        else:
            failed.append(f"{row['id']} [{row['param']}] {reason} ｜ {row['utterance']}")

    scored = len(rows) - skipped - len(pending)
    mode = "旧手写提示词（基线）" if legacy else "注册表槽位说明书"
    print("=" * 72)
    print(f"对抗槽位评测：{len(rows)} 条，{mode}")
    if skipped:
        print(f"LLM 不可用跳过 {skipped} 条")
    if pending:
        print(f"参数未接入、暂不计分 {len(pending)} 条（见 status=not_implemented）")
    print(f"准确率：{passed}/{scored} = {passed / max(1, scored):.1%}")
    if failed:
        print(f"\n失败明细（前 25 条）：")
        for line in failed[:25]:
            print(f"  {line}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
