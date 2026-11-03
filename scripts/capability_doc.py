"""能力边界文档 ↔ 注册表一致性校验。

用途：让 `docs/能力边界.md` 不会随着代码改动悄悄过期。
文档里有一个机器可读块 `<!-- capability-index ... -->`，记录每层能力的条目名；
本脚本从注册表读出实际条目，与文档块逐项比对。

用法（项目根目录下）：
    py -3.12 scripts/capability_doc.py --check    # 不一致则退出码 1，CI 可接
    py -3.12 scripts/capability_doc.py --write    # 用注册表重写文档里的索引块

设计取舍：
    能自动推出的键（参数落点、数据字段、约束/目标原语）一律从注册表读；
    推不出的键（哪些数据字段没有补丁入口）保留人工维护，只校验它是数据字段的子集
    —— 因为代码里不存在"字段 → 补丁类型"的映射，凭 kind 名猜会造假。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dsl_core.registry import data_fields, parameters, primitives  # noqa: E402

DOC = ROOT / "docs" / "能力边界.md"
BLOCK_RE = re.compile(r"<!-- capability-index\n(.*?)\n-->", re.S)

# 文档里索引块的键顺序（也是 --write 的输出顺序）
KEYS = (
    "parameters",
    "parameters_redirect",
    "parameters_unsupported",
    "data_changeable",
    "data_without_entry",
    "constraints_compiled",
    "constraints_unimplemented",
    "objectives_compiled",
)

# 人工维护：登记为可改、但 DSL 没有对应补丁类型的数据字段。
# 代码里没有"字段 → 补丁"的映射，只能人工判定（判定依据见文档 §7 第 2 条）。
DATA_WITHOUT_ENTRY = ("维修包成本", "其他费用", "严重等级", "出库")


def _param_names_by_route() -> dict[str, set[str]]:
    all_names = set(parameters.GENERATION_LAYER) | set(parameters.SELECTION_LAYER)
    out: dict[str, set[str]] = {
        "parameters": set(),
        "parameters_redirect": set(),
        "parameters_unsupported": set(),
    }
    for name in all_names:
        route = parameters.route_of(name)
        if route == "parameters":
            out["parameters"].add(name)
        elif route == "unsupported":
            out["parameters_unsupported"].add(name)
        else:
            out["parameters_redirect"].add(name)
    return out


def actual() -> dict[str, set[str]]:
    """注册表当前事实。"""
    by_route = _param_names_by_route()
    data = {
        name
        for fields in data_fields.DATA_FIELDS.values()
        for name, spec in fields.items()
        if spec.get("changeable")
    }
    return {
        **by_route,
        "data_changeable": data,
        "data_without_entry": set(DATA_WITHOUT_ENTRY),
        "constraints_compiled": set(primitives.COMPILED_CONSTRAINTS),
        "constraints_unimplemented": set(primitives.CONSTRAINT_PRIMITIVES)
        - set(primitives.COMPILED_CONSTRAINTS),
        "objectives_compiled": set(primitives.COMPILED_OBJECTIVES),
    }


def parse_block(text: str) -> dict[str, set[str]]:
    match = BLOCK_RE.search(text)
    if not match:
        raise SystemExit("文档里找不到 <!-- capability-index ... --> 块，请先运行 --write 生成")
    out: dict[str, set[str]] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = {item.strip() for item in value.split(",") if item.strip()}
    return out


def render_block(facts: dict[str, set[str]]) -> str:
    lines = ["<!-- capability-index"]
    for key in KEYS:
        lines.append(f"{key}: " + ", ".join(sorted(facts[key])))
    lines.append("-->")
    return "\n".join(lines)


def check() -> int:
    text = DOC.read_text(encoding="utf-8")
    doc_facts = parse_block(text)
    reg_facts = actual()

    problems: list[str] = []
    for key in KEYS:
        if key not in doc_facts:
            problems.append(f"[{key}] 文档缺这一行")
            continue
        if key == "data_without_entry":
            # 人工项：只要求它是可改字段的子集，且确实不属于任何补丁
            extra = doc_facts[key] - reg_facts["data_changeable"]
            if extra:
                problems.append(f"[{key}] 不在可改数据字段里：{sorted(extra)}")
            continue
        missing = reg_facts[key] - doc_facts[key]
        stale = doc_facts[key] - reg_facts[key]
        if missing:
            problems.append(f"[{key}] 注册表有、文档没写：{sorted(missing)}")
        if stale:
            problems.append(f"[{key}] 文档写了、注册表没有：{sorted(stale)}")

    # 参数总量自洽
    total = len(reg_facts["parameters"]) + len(reg_facts["parameters_redirect"]) + len(
        reg_facts["parameters_unsupported"]
    )
    if total != len(parameters.APPLY_ROUTE):
        problems.append(
            f"[参数] 落点分类合计 {total} 项，APPLY_ROUTE 有 {len(parameters.APPLY_ROUTE)} 项"
        )

    if problems:
        print("能力边界文档与注册表不一致：")
        for item in problems:
            print("  -", item)
        print("\n修好代码或文档后，运行：py -3.12 scripts/capability_doc.py --write")
        return 1

    print(
        f"一致。参数 {total} 项（可用 {len(reg_facts['parameters'])} / 换节 "
        f"{len(reg_facts['parameters_redirect'])} / 无落点 {len(reg_facts['parameters_unsupported'])}），"
        f"数据字段 {len(reg_facts['data_changeable'])} 个，"
        f"约束 {len(reg_facts['constraints_compiled'])} 已实现 / "
        f"{len(reg_facts['constraints_unimplemented'])} 未实现，"
        f"目标 {len(reg_facts['objectives_compiled'])} 个"
    )
    return 0


def write() -> int:
    text = DOC.read_text(encoding="utf-8")
    block = render_block(actual())
    if BLOCK_RE.search(text):
        text = BLOCK_RE.sub(block, text)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    DOC.write_text(text, encoding="utf-8")
    print(f"已重写索引块：{DOC.relative_to(ROOT)}")
    return 0


def main(argv: list[str]) -> int:
    if "--write" in argv:
        return write()
    if "--check" in argv or not argv:
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
