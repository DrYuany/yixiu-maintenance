#!/usr/bin/env python3
"""Validate the restricted linear ModelSpec contract used by this skill."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _finite_number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def validate_model_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["ModelSpec must be a JSON object."]

    required = ("schema_version", "status", "problem_statement", "data", "variables", "objectives", "constraints", "unresolved_data", "assumptions", "provenance")
    for field in required:
        if field not in spec:
            errors.append(f"Missing required field: {field}.")

    if spec.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'.")
    if not isinstance(spec.get("problem_statement"), str) or not spec.get("problem_statement", "").strip():
        errors.append("problem_statement must be a non-empty string.")

    status = spec.get("status")
    if not isinstance(status, str) or status not in {"ready", "clarification_required", "extension_proposal", "rejected"}:
        errors.append("status must be ready, clarification_required, extension_proposal, or rejected.")
    if status != "ready":
        errors.append("Solver dispatch requires status 'ready'.")
    unresolved = spec.get("unresolved_data")
    if not isinstance(unresolved, list):
        errors.append("unresolved_data must be a list.")
    elif unresolved:
        errors.append("Solver dispatch is blocked while unresolved_data is non-empty.")

    data = spec.get("data")
    if not isinstance(data, dict):
        errors.append("data must be an object mapping parameter IDs to finite numbers.")
        data = {}
    else:
        for key, value in data.items():
            if not isinstance(key, str) or not key:
                errors.append("Data parameter IDs must be non-empty strings.")
            if not _finite_number(value):
                errors.append(f"Data parameter '{key}' must be a finite number.")

    variables = spec.get("variables")
    variable_ids: set[str] = set()
    if not isinstance(variables, list) or not variables:
        errors.append("variables must be a non-empty list.")
        variables = []
    for index, variable in enumerate(variables):
        prefix = f"variables[{index}]"
        if not isinstance(variable, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        vid = variable.get("id")
        if not isinstance(vid, str) or not vid:
            errors.append(f"{prefix}.id must be a non-empty string.")
        elif vid in variable_ids:
            errors.append(f"Duplicate variable ID '{vid}'.")
        else:
            variable_ids.add(vid)
        vtype = variable.get("type")
        if not isinstance(vtype, str) or vtype not in {"binary", "integer", "continuous"}:
            errors.append(f"{prefix}.type must be binary, integer, or continuous.")
        lower, upper = variable.get("lower_bound"), variable.get("upper_bound")
        if not _finite_number(lower) or not _finite_number(upper):
            errors.append(f"{prefix} requires finite numeric lower_bound and upper_bound.")
        elif lower > upper:
            errors.append(f"{prefix} lower_bound cannot exceed upper_bound.")
        elif vtype == "binary" and (lower != 0 or upper != 1):
            errors.append(f"{prefix} binary bounds must be exactly 0 and 1.")
        elif vtype == "integer" and (not float(lower).is_integer() or not float(upper).is_integer()):
            errors.append(f"{prefix} integer bounds must be integral.")
        if not isinstance(variable.get("unit"), str) or not variable.get("unit", "").strip():
            errors.append(f"{prefix}.unit must be stated.")

    def check_number_or_data(value: Any, label: str) -> None:
        if _finite_number(value):
            return
        if isinstance(value, dict) and set(value) == {"data"}:
            ref = value["data"]
            if not isinstance(ref, str) or ref not in data:
                errors.append(f"{label} references unknown data parameter '{ref}'.")
            elif not _finite_number(data[ref]):
                errors.append(f"{label} references non-finite data parameter '{ref}'.")
            return
        errors.append(f"{label} must be a finite number or a data reference; expressions and variable coefficients are not linear coefficients.")

    def check_expression(expression: Any, label: str) -> None:
        if not isinstance(expression, dict) or set(expression) != {"constant", "terms"}:
            errors.append(f"{label} must use canonical linear form with only constant and terms; nonlinear/operator expressions are rejected.")
            return
        check_number_or_data(expression["constant"], f"{label}.constant")
        terms = expression["terms"]
        if not isinstance(terms, list):
            errors.append(f"{label}.terms must be a list.")
            return
        for ti, term in enumerate(terms):
            tlabel = f"{label}.terms[{ti}]"
            if not isinstance(term, dict) or set(term) != {"variable", "coefficient"}:
                errors.append(f"{tlabel} must contain exactly variable and coefficient.")
                continue
            variable_id = term["variable"]
            if not isinstance(variable_id, str) or variable_id not in variable_ids:
                errors.append(f"{tlabel} references undeclared variable '{variable_id}'.")
            check_number_or_data(term["coefficient"], f"{tlabel}.coefficient")

    objectives = spec.get("objectives")
    objective_ids: set[str] = set()
    priorities: set[int] = set()
    if not isinstance(objectives, list) or not objectives:
        errors.append("objectives must be a non-empty list.")
        objectives = []
    for index, objective in enumerate(objectives):
        prefix = f"objectives[{index}]"
        if not isinstance(objective, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        oid, priority = objective.get("id"), objective.get("priority")
        if not isinstance(oid, str) or not oid:
            errors.append(f"{prefix}.id must be a non-empty string.")
        elif oid in objective_ids:
            errors.append(f"Duplicate objective ID '{oid}'.")
        else:
            objective_ids.add(oid)
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            errors.append(f"{prefix}.priority must be a positive integer.")
        elif priority in priorities:
            errors.append(f"Objective priorities must be unique for lexicographic ordering; duplicate {priority}.")
        else:
            priorities.add(priority)
        if not isinstance(objective.get("sense"), str) or objective.get("sense") not in {"minimize", "maximize"}:
            errors.append(f"{prefix}.sense must be minimize or maximize.")
        check_expression(objective.get("expression"), f"{prefix}.expression")

    constraints = spec.get("constraints")
    constraint_ids: set[str] = set()
    if not isinstance(constraints, list):
        errors.append("constraints must be a list.")
        constraints = []
    for index, constraint in enumerate(constraints):
        prefix = f"constraints[{index}]"
        if not isinstance(constraint, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        cid = constraint.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"{prefix}.id must be a non-empty string.")
        elif cid in constraint_ids:
            errors.append(f"Duplicate constraint ID '{cid}'.")
        else:
            constraint_ids.add(cid)
        if constraint.get("kind") != "linear_row":
            errors.append(f"{prefix}.kind must be linear_row; unregistered constraint templates cannot reach the solver.")
        if not isinstance(constraint.get("meaning"), str) or not constraint.get("meaning", "").strip():
            errors.append(f"{prefix}.meaning must explain the business meaning.")
        check_expression(constraint.get("lhs"), f"{prefix}.lhs")
        if not isinstance(constraint.get("sense"), str) or constraint.get("sense") not in {"<=", ">=", "=="}:
            errors.append(f"{prefix}.sense must be <=, >=, or ==.")
        check_number_or_data(constraint.get("rhs"), f"{prefix}.rhs")

    for field in ("assumptions", "provenance"):
        if not isinstance(spec.get(field), list):
            errors.append(f"{field} must be a list.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_spec", nargs="?", help="JSON file; defaults to stdin")
    args = parser.parse_args()
    try:
        raw = Path(args.model_spec).read_text(encoding="utf-8") if args.model_spec else sys.stdin.read()
        spec = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    errors = validate_model_spec(spec)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
