"""Deterministic, auditable summary of the CP-SAT maintenance model."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


PARAMETER_META = {
    "船租（元每艘每天）": {"label": "船租", "unit": "元/艘/天"},
    "人工单价（元每人时）": {"label": "人工单价", "unit": "元/人时"},
    "最多作业线数": {"label": "最多作业线数", "unit": "条"},
    "人员总上限": {"label": "人员总上限", "unit": "人"},
    "每天最多维修小时数": {"label": "每天最多维修小时数", "unit": "小时/天"},
    "风机间转移小时数": {"label": "风机间转移时间", "unit": "小时"},
    "每天维修开始时间": {"label": "每日维修开始时间", "unit": "时刻"},
    "每天维修结束时间": {"label": "每日维修结束时间", "unit": "时刻"},
}


METRIC_FORMULAS = {
    "总经济成本": (
        "总经济成本 = 固定停机损失常数 "
        "+ Σ(任务直接成本 + 停机损失变化) × x(i,k,l) "
        "+ Σ 船租 × 租船日变量 y(line,day)"
    ),
    "现金支出": (
        "现金支出 = Σ 任务直接成本 × x(i,k,l) "
        "+ Σ 船租 × y(line,day)"
    ),
    "完成任务数": "完成任务数 = Σ x(i,k,l)",
    "最后完工时间": "最后完工时间 = max(完工分钟(i,k) × x(i,k,l))",
    "完工时间和": "完工时间和 = Σ 完工分钟(i,k) × x(i,k,l)",
    "高严重完成数": "高严重完成数 = Σ_{任务严重等级=高} x(i,k,l)",
    "中严重完成数": "中严重完成数 = Σ_{任务严重等级=中} x(i,k,l)",
    "低严重完成数": "低严重完成数 = Σ_{任务严重等级=低} x(i,k,l)",
    "高严重完工时间和": "高严重完工时间和 = Σ_{严重等级=高} 完工分钟(i,k) × x(i,k,l)",
    "中严重完工时间和": "中严重完工时间和 = Σ_{严重等级=中} 完工分钟(i,k) × x(i,k,l)",
    "低严重完工时间和": "低严重完工时间和 = Σ_{严重等级=低} 完工分钟(i,k) × x(i,k,l)",
}


CONSTRAINT_MEANINGS = {
    "interval": "候选维修作业的可选时间区间，由任务选择变量启用",
    "linear": "任务选择、库存累计、船租启用和业务上下界等线性约束",
    "no_overlap": "同一作业线或同一风机上的作业时间不得重叠",
    "lin_max": "最大完工、租船日启用和停机损失线性化辅助约束",
    "at_most_one": "互斥候选最多选择一个",
    "exactly_one": "候选集合恰好选择一个",
    "bool_or": "布尔或逻辑约束",
    "bool_and": "布尔与逻辑约束",
}


def _constraint_kind(constraint: Any) -> str:
    for kind in (
        "interval",
        "linear",
        "no_overlap",
        "lin_max",
        "at_most_one",
        "exactly_one",
        "bool_or",
        "bool_and",
        "all_diff",
        "circuit",
        "cumulative",
        "dummy_constraint",
    ):
        checker = getattr(constraint, f"has_{kind}", None)
        if callable(checker) and checker():
            return kind
    return "other"


def _variable_group(name: str) -> str:
    if name.startswith("x_"):
        return "task_choice"
    if name.startswith("prefix_"):
        return "rental_prefix"
    if name.startswith("suffix_"):
        return "rental_suffix"
    if name.startswith("rent_"):
        return "rental_day"
    if name.startswith("loss_"):
        return "downtime_loss"
    if name == "最后完工时间":
        return "makespan"
    return "other"


def _domain(variable: Any) -> list[int]:
    values = list(variable.domain)
    if not values:
        return []
    if len(values) == 2:
        return [int(values[0]), int(values[1])]
    return [int(min(values)), int(max(values))]


def _variable_meaning(group: str) -> str:
    return {
        "task_choice": "任务 i 在候选 k、作业线 l 上执行时为 1，否则为 0",
        "rental_prefix": "某作业线在某日已有作业开始时为 1",
        "rental_suffix": "某作业线在某日仍有作业未结束时为 1",
        "rental_day": "某作业线在某日需要租船时为 1",
        "downtime_loss": "某风机在时间分段内用于线性化停机损失的整数辅助变量",
        "makespan": "所有已选维修任务的最晚完工分钟",
        "other": "CP-SAT 辅助变量",
    }[group]


def _parameter_records(data: Any) -> list[dict[str, Any]]:
    overrides = dict(data.meta.get("参数覆盖") or {})
    records = []
    for key, value in sorted(data.params.items()):
        meta = PARAMETER_META.get(key, {"label": key, "unit": "按基础数据定义"})
        records.append(
            {
                "id": key,
                "label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                "source": "用户本次覆盖" if key in overrides else "基座参数快照",
                "baseSource": data.meta.get("来源"),
                "dataNature": data.meta.get("数据性质"),
                "asOf": data.meta.get("规划时点"),
                "override": key in overrides,
            }
        )
    return records


def _expression_preview(expression: Any, max_terms: int = 6, max_chars: int = 900) -> dict[str, Any]:
    text = str(expression)
    terms = text.split(" + ")
    preview = " + ".join(terms[:max_terms])
    if len(terms) > max_terms:
        preview += f" + …（共 {len(terms)} 项）"
    if len(preview) > max_chars:
        preview = preview[:max_chars] + "…"
    return {
        "preview": preview,
        "termCount": len(terms),
        "expressionCharacters": len(text),
    }


def _linear_constraint_sample(
    constraint: Any,
    variable_names: list[str],
    index: int,
) -> dict[str, Any] | None:
    if not constraint.has_linear():
        return None
    variables = list(constraint.linear.vars)
    coefficients = list(constraint.linear.coeffs)
    domain = list(constraint.linear.domain)
    terms = []
    for variable_index, coefficient in zip(variables, coefficients):
        if variable_index < 0 or variable_index >= len(variable_names):
            continue
        variable_name = variable_names[variable_index]
        terms.append(
            variable_name if coefficient == 1 else f"{coefficient}*{variable_name}"
        )
    preview = " + ".join(terms[:8])
    if len(terms) > 8:
        preview += f" + …（共 {len(terms)} 项）"
    if len(domain) == 2 and domain[0] == domain[1]:
        bound = f"= {domain[0]}"
    elif len(domain) >= 2:
        bound = f"∈ [{domain[0]}, {domain[1]}]"
    else:
        bound = f"domain={domain}"
    return {
        "id": constraint.name or f"linear_{index + 1}",
        "expression": f"{preview} {bound}",
        "variableCount": len(variables),
        "lowerBound": domain[0] if domain else None,
        "upperBound": domain[1] if len(domain) > 1 else None,
    }


def build_model_artifact(
    *,
    data: Any,
    options: dict[str, list[dict[str, Any]]],
    model: Any,
    choices: list[tuple[dict[str, Any], Any]],
    stages: list[tuple[str, str, Any]],
    strategy: str,
    mode: str,
    weights: dict[str, float] | None,
    limits: dict[str, Any] | None,
    partial: bool,
    solver_parameters: dict[str, Any],
    model_fingerprint: str,
) -> dict[str, Any]:
    proto = model.Proto()
    variable_names = [variable.name for variable in proto.variables]
    variable_groups = Counter(_variable_group(variable.name) for variable in proto.variables)
    constraint_groups = Counter(_constraint_kind(constraint) for constraint in proto.constraints)
    variables = []
    for group, count in variable_groups.items():
        members = [
            variable
            for variable in proto.variables
            if _variable_group(variable.name) == group
        ]
        domains = [_domain(variable) for variable in members if _domain(variable)]
        variables.append(
            {
                "id": group,
                "name": group,
                "type": "binary" if all(item == [0, 1] for item in domains) else "integer",
                "count": count,
                "lowerBound": min((item[0] for item in domains), default=0),
                "upperBound": max((item[1] for item in domains), default=0),
                "meaning": _variable_meaning(group),
                "samples": [variable.name for variable in members[:5]],
            }
        )
    variables.sort(key=lambda item: item["count"], reverse=True)
    constraints = [
        {
            "kind": kind,
            "count": count,
            "meaning": CONSTRAINT_MEANINGS.get(kind, "CP-SAT 内部辅助约束"),
            "names": [
                constraint.name
                for constraint in proto.constraints
                if _constraint_kind(constraint) == kind and constraint.name
            ][:5],
        }
        for kind, count in constraint_groups.items()
    ]
    constraints.sort(key=lambda item: item["count"], reverse=True)
    objective_stages = []
    for index, (name, sense, expression) in enumerate(stages, start=1):
        expression_preview = _expression_preview(expression)
        objective_stages.append(
            {
                "priority": index,
                "name": name,
                "sense": "maximize" if sense == "maximize" else "minimize",
                "formula": METRIC_FORMULAS.get(name, f"目标表达式：{name}"),
                "expressionCharacters": expression_preview["expressionCharacters"],
                "expressionTermCount": expression_preview["termCount"],
                "expressionPreview": expression_preview["preview"],
            }
        )
    constraint_samples = []
    for index, constraint in enumerate(proto.constraints):
        sample = _linear_constraint_sample(constraint, variable_names, index)
        if sample:
            constraint_samples.append(sample)
        if len(constraint_samples) >= 12:
            break
    task_count = len(data.tasks)
    choice_count = len(choices)
    line_count = min(int(data.params["最多作业线数"]), int(data.params["人员总上限"]))
    window_count = len(data.windows)
    return {
        "schemaVersion": "maintenance-model-artifact-v1",
        "modelType": "CP-SAT bounded integer scheduling with linear cost",
        "problemStatement": (
            "在已批准海事窗口、安全海况、库存、船线、人员、每日工时和任务期限内，"
            "选择可执行维修任务及其开工时间，并按声明的分层或加权目标优化。"
        ),
        "strategy": strategy,
        "mode": mode,
        "weights": weights,
        "limits": limits or {},
        "partialDiagnostic": partial,
        "modelFingerprint": model_fingerprint,
        "dataSnapshot": {
            "inputHash": data.meta.get("输入哈希"),
            "runHash": data.meta.get("运行哈希"),
            "asOf": data.meta.get("规划时点"),
            "dataNature": data.meta.get("数据性质"),
            "source": data.meta.get("来源"),
            "planningBoundary": data.meta.get("规划边界"),
            "targetWindow": data.meta.get("目标窗口编号"),
            "businessVersion": data.meta.get("业务版本"),
        },
        "dimensions": {
            "tasks": task_count,
            "windows": window_count,
            "workLines": line_count,
            "candidateChoices": choice_count,
        },
        "variables": {
            "total": len(proto.variables),
            "groups": variables,
        },
        "objectives": {
            "mode": mode,
            "stages": objective_stages,
            "weights": weights,
        },
        "constraints": {
            "total": len(proto.constraints),
            "groups": constraints,
            "samples": constraint_samples,
            "expressionNote": (
                "样本来自本次 CP-SAT 线性约束的真实变量名、系数和上下界；"
                "超长行只截取前 8 项并标注总项数。"
            ),
        },
        "parameters": _parameter_records(data),
        "solverParameters": solver_parameters,
        "provenance": {
            "artifactGenerator": "product_planner.build_model_artifact/v1",
            "sourceFile": data.meta.get("来源"),
            "generatedFrom": "actual CP-SAT model built for this solve",
            "note": "变量和约束数量来自本次 CP-SAT Proto；目标公式为生成模型时的实际结构摘要。",
        },
    }


def write_model_artifact(
    artifact: dict[str, Any],
    output: Path,
    raw_model: Any,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / ("diagnostic_solver_model.pbtxt" if artifact.get("partialDiagnostic") else "solver_model.pbtxt")
    export_model_file(raw_model, raw_path)
    artifact["rawModel"] = {
        "file": raw_path.name,
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "sizeBytes": raw_path.stat().st_size,
    }
    artifact_path = output / ("diagnostic_model_artifact.json" if artifact.get("partialDiagnostic") else "model_artifact.json")
    artifact["artifactFile"] = artifact_path.name
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return artifact


def export_model_file(model: Any, target: Path) -> None:
    """OR-Tools cannot export to Unicode Windows paths; export via an ASCII temp file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maintenance-model-") as temporary:
        temp_path = Path(temporary) / "solver_model.pbtxt"
        model.ExportToFile(str(temp_path))
        shutil.copyfile(temp_path, target)
