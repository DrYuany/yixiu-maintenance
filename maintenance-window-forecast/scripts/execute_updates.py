#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI wrapper for the portable maintenance-window-forecast skill runtime."""

from __future__ import annotations

import json
import sys

from maintenance_window_runtime import execute_named_strategy_comparison, execute_parameter_updates


def main() -> None:
    if len(sys.argv) < 2:
        demo_result = execute_parameter_updates({"weights": {"loss": 1.1}}, solver_name="ortools")
        print(
            "No arguments provided. Ran demo scenario.\n\n"
            + json.dumps(demo_result, ensure_ascii=False, indent=2)
        )
        return

    mode = sys.argv[1]
    if len(sys.argv) >= 3 and sys.argv[2] != "-":
        raw_payload = sys.argv[2]
        solver = sys.argv[3] if len(sys.argv) > 3 else "ortools"
    else:
        raw_payload = sys.stdin.read()
        solver = sys.argv[3] if len(sys.argv) > 3 else (sys.argv[2] if len(sys.argv) > 2 else "ortools")

    payload = json.loads(raw_payload)

    if mode == "updates":
        result = execute_parameter_updates(payload, solver_name=solver)
    elif mode == "compare":
        result = execute_named_strategy_comparison(payload, solver_name=solver)
    else:
        raise SystemExit(f"Unknown mode: {mode}")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
