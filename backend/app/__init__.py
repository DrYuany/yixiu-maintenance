"""Backend application package for the maintenance window agent."""

import sys
from pathlib import Path

# 中立共享执行侧 dsl_core 在仓库根，而 uvicorn 用 `--app-dir backend` 启动时
# sys.path 只有 backend/，够不着仓库根。这里补挂，让 `import dsl_core` 可用。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 注：此处曾挂载 llm-first/src（大一修的 LLM 主导理解层）。小一修分家后
# 仓库内已无该目录，理解层由 yxcore 承担，故移除这段无效挂载。
