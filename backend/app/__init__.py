"""Backend application package for the maintenance window agent."""

import sys
from pathlib import Path

# 中立共享执行侧 dsl_core 在仓库根，而 uvicorn 用 `--app-dir backend` 启动时
# sys.path 只有 backend/，够不着仓库根。这里补挂，让 `import dsl_core` 可用。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# LLM 主导理解层（llmfirst 包）在 llm-first/src，进程内 import 引擎要用到。
_LLMFIRST_SRC = _REPO_ROOT / "llm-first" / "src"
if str(_LLMFIRST_SRC) not in sys.path:
    sys.path.insert(0, str(_LLMFIRST_SRC))
