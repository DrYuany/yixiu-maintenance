from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# dsl_core（中立共享执行侧）在仓库根，pytest 从 backend/ 起跑够不着，补挂。
REPO_ROOT = BACKEND_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent.db"


# 生产环境里规划时点跟着"今天"走，测试必须钉死一天才有确定答案。
# 钉住的这一天要和断言里的数字同源：2026-10-23 18:00 收到 11 项告警。
PINNED_AS_OF = "2026-10-23T18:00:00+08:00"


@pytest.fixture(autouse=True)
def clear_env() -> None:
    os.environ.pop("MWA_DB_PATH", None)
    os.environ["SKIP_LOCAL_ENV"] = "1"
    os.environ["PLANNING_AS_OF"] = PINNED_AS_OF
    os.environ.pop("MINIMAX_API_KEY", None)
    os.environ.pop("MINIMAX_MODEL", None)
    os.environ.pop("MINIMAX_BASE_URL", None)
    # 演示口令会让所有接口多一道拦截，测试里必须关掉，否则全部 403。
    os.environ.pop("DEMO_ACCESS_CODE", None)
