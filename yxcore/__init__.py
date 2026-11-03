"""yxcore：小一修的核心包。

与大一修（dsl_core）并列，共用同一套求解内核（maintenance-milp-modeler/scripts），
但不引入大一修的编译器、原语表、量生成器——那是被降级砍掉的部分。

复用方式：靠 sys.path 挂载内核目录后 import，不 fork、不复制内核文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 槽位引擎在 fork 根下（与 dsl_core 平级）：yxcore/ 上一级就是项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = PROJECT_ROOT / "maintenance-milp-modeler" / "scripts"

# dsl_core（部件目录、实体解析）在项目根下；不挂上，脱离项目根跑（如 eval 脚本）就 import 不到。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def solver_modules():
    """挂载并返回求解内核的公开入口。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    from product_data import load_project  # noqa: PLC0415
    from product_planner import account, candidates, heuristic, solve  # noqa: PLC0415
    return {
        "load_project": load_project,
        "candidates": candidates,
        "heuristic": heuristic,
        "solve": solve,
        "account": account,
    }


__all__ = ["PROJECT_ROOT", "solver_modules"]
