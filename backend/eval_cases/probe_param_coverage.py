"""参数落地能力探针。

目的：把"注册表声称可改"和"改完真能进模型"这两件事对齐。

注册表里 `is_changeable()` 为真的参数，是提示词 L0 目录对外宣称的"你
能改的东西"。但真正能不能改，取决于三处：
  validator  → 字段在注册表且可改
  bridge     → 能不能映射进 PlanningRequest（parameters/objectives/constraints）
  load_project → 参数键在 OVERRIDES 白名单里

三处只要有一处不认，用户说"改这个"就会拿到报错。本脚本逐项跑一遍，
把断点钉在第几步，输出一张可直接贴进文档的表。

用法：
    python backend/eval_cases/probe_param_coverage.py
    python backend/eval_cases/probe_param_coverage.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
for path in (BACKEND_ROOT, PROJECT_ROOT / "maintenance-milp-modeler" / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from dsl_core.planning_bridge import dsl_to_request  # noqa: E402
from dsl_core.registry import parameters as param_reg  # noqa: E402
from dsl_core.validator import validate_dsl  # noqa: E402
from product_data import OVERRIDES, load_project  # noqa: E402

# 每项的探针取值：只求"格式合法"，不追求业务含义。
_PROBE_VALUES: dict[str, object] = {
    "候选风机": ["风机001"],
    "候选部件": ["部件01"],
    "强制维修任务": ["任务001"],
    "禁止新开工风机": ["风机001"],
    "禁止新增开工日期": ["2026-11-01"],
    "指定任务截止时间": ["2026-11-10T08:00:00+08:00"],
    "固定使用的批准窗口": ["窗口01"],
    "成本敏感性倍率列表": [1.0],
    "标准目标策略": "成本最低优先",
    "正式目标": "成本最低优先",
    "预算覆盖口径": "总经济成本",
    "船租是否包含燃油": "是",
    "每天开始维修": "08:00",
    "每天结束维修": "18:00",
}
_YES_NO = {
    "允许夜间转移", "允许跨天暂停", "自动允许超期", "未知期限任务是否可提前修",
    "发电损失计入总经济成本", "船租计入总经济成本", "人工计入总经济成本",
    "维修包计入总经济成本", "预算限制启用",
}


def _probe_value(name: str) -> object:
    if name in _PROBE_VALUES:
        return _PROBE_VALUES[name]
    if name in _YES_NO:
        return "是"
    return 1


_REDIRECT_DSL: dict[str, dict] = {
    "标准目标策略": {"objectives": [{"type": "strategy", "name": "检修最多器件优先"}]},
    "费用预算上限": {"constraints": [{"type": "budget", "quantity": "总经济成本",
                                     "op": "<=", "value": 5000000}]},
    "共同最低完成任务数": {"constraints": [{"type": "cardinality", "filter": {}, "op": ">=",
                                           "value": 5}]},
    "强制维修任务": {"constraints": [{"type": "must", "task": "任务001"}]},
}


def _classify(name: str) -> dict[str, object]:
    route = param_reg.route_of(name)
    record: dict[str, object] = {"参数": name, "落点": route}
    if not param_reg.is_changeable(name):
        record.update(断点="注册表", 结论="未接入（不应出现在提示词里）",
                      提示=param_reg.unsupported_reason(name))
        return record

    if route == "parameters":
        value = _probe_value(name)
        record["探针值"] = value
        dsl = {"parameters": [{"field": name, "op": "=", "value": value}]}
    else:
        dsl = dict(_REDIRECT_DSL[name])
        record["探针值"] = "见 _REDIRECT_DSL"

    errors = validate_dsl(dsl)
    if errors:
        record.update(断点="validator", 结论="注册表自相矛盾", 提示=errors[0])
        return record

    base = load_project(PROJECT_ROOT, "基座01")
    request, residual, _ = dsl_to_request(dsl, base=None)
    if residual:
        record.update(断点="bridge", 结论="写了但没人接", 提示=str(residual[0])[:120])
        return record

    if route != "parameters":
        reached = (
            (route == "objectives.strategy" and request.strategy == "检修最多器件优先")
            or (route.startswith("constraints.") and bool(request.limits))
        )
        record.update(断点="无", 结论="生效（换节写）" if reached else "换节写也没接住",
                      落点值=dict(request.limits) or request.strategy)
        return record

    try:
        overrides = dict(request.overrides)
        for key, multiplier in request.relative_overrides.items():
            overrides[key] = float(base.params[key]) * multiplier
        patched = load_project(PROJECT_ROOT, "基座01", overrides=overrides,
                              data_patches=request.data_patches)
    except Exception as exc:  # noqa: BLE001 - 探针要把错误原文记下来
        message = str(exc)
        if "未支持参数" in message:
            record.update(断点="load_project.OVERRIDES", 结论="填了就报错", 提示=message[:120])
        else:
            record.update(断点="load_project 取值", 结论="键能落地但值不合规", 提示=message[:120])
        return record

    changed = any(
        key in base.params and key in patched.params and base.params[key] != patched.params[key]
        for key in overrides
    )
    record.update(断点="无",
                  结论="生效" if changed else "接受（探针用同值，未验证语义）")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    names = [row["参数名称"] for row in param_reg._rows() if param_reg.is_changeable(row["参数名称"])]
    rows = [_classify(name) for name in names]

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(str(row["结论"]), []).append(row)

    print(f"注册表可改参数：{len(names)} 项\n")
    for verdict, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"== {verdict}（{len(items)} 项）==")
        for item in items:
            print(f"  {item['参数']:12s} 断点 {item['断点']:24s} {item.get('提示','')[:80]}")
        print()

    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
