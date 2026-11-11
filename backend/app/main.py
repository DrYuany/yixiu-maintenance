from __future__ import annotations

import gzip
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import agent as agent_api
from app.api import conversations as conversations_api
from app.api import dashboard as dashboard_api
from app.api import versions as versions_api
from app.core.agent_runtime import AgentRuntime
from app.core.agent_memory import (
    ConversationSummaryRepository,
    DecisionMemoryRepository,
    UserProfileRepository,
)
from app.core.agent_trace import AgentTraceRepository
from app.core.demo_access import (
    USER_KEY_HEADER,
    VISITOR_COOKIE,
    VISITOR_HEADER,
    TurnQuota,
    normalize_key,
)
from app.core.memory_manager import MemoryManager
from app.core.version_manager import VersionManager
from app.core.solve_jobs import SolveJobRepository
from app.db.database import Database, resolve_db_path
from app.db.models import (
    CandidateRepository,
    ConversationRepository,
    MessageRepository,
    ModelArtifactRepository,
    VersionRepository,
    WorkingMemoryRepository,
)


def load_local_env() -> None:
    if os.environ.get("SKIP_LOCAL_ENV") == "1":
        return
    candidate_paths = [
        Path(__file__).resolve().parents[2] / ".env.local",
        Path(__file__).resolve().parents[3] / ".env.local",
    ]
    for path in candidate_paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


logger = logging.getLogger(__name__)

DEMO_PASS_COOKIE = "mwa_demo_pass"


def _demo_access_code() -> str:
    return os.environ.get("DEMO_ACCESS_CODE", "").strip()


def _attach_demo_guard(app: FastAPI) -> None:
    """演示模式：一组口令换一张七天有效的通行证。

    没设 DEMO_ACCESS_CODE 时完全不生效，本地开发和自动化测试不受影响。
    """
    code = _demo_access_code()
    if not code:
        return

    gate = """<!doctype html><meta charset="utf-8">
<title>需要访问口令</title>
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;display:grid;
place-items:center;height:100vh;margin:0;background:#fafafa;color:#1a1a1a">
<div style="text-align:center;max-width:420px;padding:0 24px">
<div style="width:40px;height:40px;border-radius:10px;background:#155fc5;margin:0 auto 20px"></div>
<h1 style="font-size:18px;font-weight:500;margin:0 0 10px">这是一个受口令保护的演示</h1>
<p style="font-size:14px;line-height:1.6;color:#666;margin:0">
请使用收到的邀请链接打开。<br>链接失效时，在网址后加上
<code style="background:#eee;padding:2px 5px;border-radius:4px">?k=口令</code> 即可。</p>
</div></body>"""

    @app.middleware("http")
    async def demo_guard(request: Request, call_next):
        path = request.url.path
        if path == "/api/health":
            return await call_next(request)
        if request.cookies.get(DEMO_PASS_COOKIE) == code:
            return await call_next(request)
        if request.query_params.get("k") == code:
            response = await call_next(request)
            response.set_cookie(DEMO_PASS_COOKIE, code, max_age=7 * 24 * 3600,
                                httponly=True, samesite="lax")
            return response
        if path.startswith("/api/"):
            return JSONResponse({"detail": "演示口令无效，请使用邀请链接打开"}, status_code=403)
        return HTMLResponse(gate, status_code=403)


# 只有这两条接口会真的发生一轮对话，其余读写不占体验额度。
QUOTA_PATHS = {"/api/agent/message", "/api/agent/message/stream"}


def _visitor_id(request: Request) -> str:
    """访客标识：前端 localStorage 的 id 优先，退 cookie，再退 IP。

    只用于计数，不做鉴权。换浏览器就能拿一份新额度，这是有意的——
    真正兜底的是全局每日上限（见 DEMO_DAILY_TURNS）。
    """
    for raw in (
        request.headers.get(VISITOR_HEADER),
        request.cookies.get(VISITOR_COOKIE),
    ):
        value = (raw or "").strip()
        if value:
            return value[:64]
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _own_key(request: Request) -> bool:
    return bool((request.headers.get(USER_KEY_HEADER) or "").strip())


def _with_visitor_cookie(response, request: Request, visitor: str):
    if not request.cookies.get(VISITOR_COOKIE):
        response.set_cookie(
            VISITOR_COOKIE, visitor, max_age=180 * 24 * 3600,
            httponly=True, samesite="lax",
        )
    return response


def _quota_message(verdict: dict[str, object]) -> str:
    if verdict.get("reason") == "daily":
        return (
            "今天的免费体验额度已经用完。可以填入你自己的 DeepSeek API Key 继续，"
            "或明天再来。"
        )
    limit = verdict.get("limit") or 0
    return (
        f"免费体验的 {limit} 轮对话已用完。"
        "填入你自己的 DeepSeek API Key 即可继续，额度不受限。"
    )


def _attach_demo_quota(app: FastAPI, quota: TurnQuota) -> None:
    """演示模式：免费轮次用完就要求访客自带密钥。

    DEMO_FREE_TURNS 没设（=0）时整段逻辑不生效，本地开发与测试完全不受影响。
    访客只要带了密钥就不扣额度，也不参与拦截。
    """

    @app.middleware("http")
    async def quota_guard(request: Request, call_next):
        visitor = _visitor_id(request)
        request.state.visitor_id = visitor

        if request.method == "POST" and request.url.path in QUOTA_PATHS:
            if not _own_key(request) and quota.enabled:
                verdict = quota.consume(visitor)
                if not verdict["allowed"]:
                    response = JSONResponse(
                        status_code=402,
                        content={
                            "code": "quota_exhausted",
                            "detail": _quota_message(verdict),
                            "limit": verdict.get("limit"),
                            "used": verdict.get("used"),
                            "reason": verdict.get("reason"),
                        },
                    )
                    return _with_visitor_cookie(response, request, visitor)

        response = await call_next(request)
        return _with_visitor_cookie(response, request, visitor)


def _resolve_site_dir() -> Path | None:
    """找前端产物目录。

    本地开发是 Vite 的 `build/`；云端发布包里叫 `site/`——因为 `build/`
    在 .gitignore 里，上传时会被整目录跳过，页面就变成 404。
    """
    root = Path(__file__).resolve().parents[2]
    for name in ("build", "site", "web"):
        candidate = root / name
        if (candidate / "index.html").exists():
            return candidate
    return None


def _attach_static_site(app: FastAPI) -> None:
    """演示模式：同源托管前端构建产物，一个端口同时出页面和接口。

    必须挂在所有 API 路由之后，否则 mount("/") 会吞掉未匹配的接口请求。
    """
    build_dir = _resolve_site_dir()
    if build_dir is None:
        logger.warning("找不到前端构建产物（build/ 或 site/），本次只提供 API")
        return
    index = build_dir / "index.html"

    # 单文件前端 2.5MB，免费隧道只有 1Mbps，不压缩首屏要等二十秒。
    # 预压一份放在旁边：命中就直发，零压缩开销，也不需要每次都算。
    gz = build_dir / "index.html.gz"
    try:
        if not gz.exists() or gz.stat().st_mtime < index.stat().st_mtime:
            gz.write_bytes(gzip.compress(index.read_bytes(), 9))
    except OSError:
        logger.warning("预压缩 index.html 失败，回退为未压缩传输")

    @app.get("/", include_in_schema=False)
    async def index_page(request: Request) -> FileResponse:
        if gz.exists() and "gzip" in request.headers.get("accept-encoding", ""):
            return FileResponse(
                gz,
                media_type="text/html; charset=utf-8",
                headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
            )
        return FileResponse(index, media_type="text/html; charset=utf-8")

    app.mount("/", StaticFiles(directory=str(build_dir), html=True), name="site")


def _probe_api_key(api_key: str) -> tuple[bool, str]:
    """拿这把密钥发一次最小请求，确认它真的能用。

    只在用户填密钥时发生一次，成本 1 个 token。宁可这时失败一次，
    也好过用户聊了三轮才发现每条回答都挂着"模型不可用"的降级提示。
    """
    try:
        from anthropic import Anthropic
        import httpx
    except ImportError:  # pragma: no cover - 依赖缺失只在裁剪包里发生
        return False, "服务端缺少 SDK 依赖，暂时无法校验密钥。"

    base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.deepseek.com/anthropic")
    model = os.environ.get("MINIMAX_MODEL", "deepseek-flash")
    try:
        client = Anthropic(
            api_key=api_key,
            auth_token="",
            base_url=base_url,
            timeout=20.0,
            http_client=httpx.Client(trust_env=False, timeout=20.0),
        )
        client.auth_token = None
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 校验失败不该把异常抛给前端
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return False, "这把密钥无效或没有权限，请重新复制完整密钥。"
        if status in (402, 429):
            return False, "这把密钥余额不足或触发限流，请检查账户状态。"
        if status is None:
            return False, f"连不上模型服务（{type(exc).__name__}），请稍后重试。"
        return False, f"密钥校验失败（HTTP {status}）。"


def create_app() -> FastAPI:
    load_local_env()
    app = FastAPI(title="Maintenance Window Agent API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    db = Database()
    quota = TurnQuota(resolve_db_path())
    conversation_repo = ConversationRepository(db)
    message_repo = MessageRepository(db)
    memory_repo = WorkingMemoryRepository(db)
    version_repo = VersionRepository(db)
    model_artifact_repo = ModelArtifactRepository(db)
    candidate_repo = CandidateRepository(db)
    memory_manager = MemoryManager(memory_repo)
    solve_jobs = SolveJobRepository(db)
    solve_jobs.recover_interrupted_jobs()
    trace_repo = AgentTraceRepository(db)
    summary_repo = ConversationSummaryRepository(db)
    profile_repo = UserProfileRepository(db)
    decision_repo = DecisionMemoryRepository(db)
    runtime = AgentRuntime(
        conversations=conversation_repo,
        messages=message_repo,
        memories=memory_manager,
        versions=version_repo,
        model_artifacts=model_artifact_repo,
        candidates=candidate_repo,
        version_manager=VersionManager(),
        jobs=solve_jobs,
        traces=trace_repo,
        summary_repo=summary_repo,
        profile_repo=profile_repo,
        decision_repo=decision_repo,
    )

    app.dependency_overrides[conversations_api.get_conversation_repo] = lambda: conversation_repo
    app.dependency_overrides[conversations_api.get_memory_manager] = lambda: memory_manager
    app.dependency_overrides[conversations_api.get_message_repo] = lambda: message_repo
    app.dependency_overrides[agent_api.get_runtime] = lambda: runtime
    app.dependency_overrides[dashboard_api.get_runtime] = lambda: runtime
    app.dependency_overrides[versions_api.get_runtime] = lambda: runtime
    app.dependency_overrides[versions_api.get_version_repo] = lambda: version_repo

    app.include_router(conversations_api.router)
    app.include_router(agent_api.router)
    app.include_router(dashboard_api.router)
    app.include_router(versions_api.router)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "demo": bool(_demo_access_code())}

    @app.get("/api/access/quota")
    def access_quota(request: Request) -> dict[str, object]:
        """前端用它显示"还剩几轮"，也用来确认自己带的密钥已被接受。"""
        visitor = getattr(request.state, "visitor_id", None) or _visitor_id(request)
        return quota.snapshot(visitor, own_key=_own_key(request))

    @app.post("/api/access/verify-key")
    def verify_key(request: Request):
        """先花 1 个 token 试一次，别让用户填完密钥才发现是错的。

        校验用的模型与正式对话一致，所以"这里过了、对话却失败"的情况只剩
        网络抖动，不会再有"密钥本身不对"这种让人摸不着头脑的降级。
        """
        key = normalize_key(request.headers.get(USER_KEY_HEADER))
        if not key:
            return JSONResponse(
                status_code=400,
                content={"code": "missing_key", "detail": "没有收到密钥"},
            )
        ok, detail = _probe_api_key(key)
        if not ok:
            return JSONResponse(
                status_code=400,
                content={"code": "invalid_key", "detail": detail},
            )
        return {"ok": True}

    # 顺序有讲究：后挂的在外面。口令门必须最外层，没口令的请求连额度都不该碰。
    _attach_demo_quota(app, quota)
    _attach_demo_guard(app)
    _attach_static_site(app)
    return app


app = create_app()
