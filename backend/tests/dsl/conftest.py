from __future__ import annotations

import sys
from pathlib import Path

# backend/tests/dsl/ → 项目根 = parents[3]；backend 根路径由父级 conftest 注入。
PROJECT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT / "maintenance-milp-modeler" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
