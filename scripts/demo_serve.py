"""演示模式启动器：一个端口同时出前端页面和 API，口令保护。

    python scripts/demo_serve.py
    python scripts/demo_serve.py --code demo2026 --port 8010

没设口令时后端行为和平时完全一样，所以这个脚本也能当普通开发服务器用。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BUILD_INDEX = ROOT / "build" / "index.html"
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def fail(message: str) -> None:
    print(f"[x] {message}")
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动受口令保护的演示站点")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1", help="默认只听本机，走隧道即可对外")
    parser.add_argument("--code", default="", help="访问口令；省略则读环境变量或自动生成")
    parser.add_argument("--open", action="store_true", help="本机演示：不设口令，直接开")
    parser.add_argument("--workers", type=int, default=0,
                        help="求解线程数。默认 min(8,CPU)；设 1 回到可复现模式")
    parser.add_argument("--seconds", type=int, default=60,
                        help="单次求解时限。方案通常 1~3 秒就有，后续时间用于打磨次要目标")
    parser.add_argument("--free-turns", type=int, default=50,
                        help="每个访客的免费对话轮数，用完要求自带 DeepSeek 密钥；0 = 不限")
    args = parser.parse_args()

    if not BUILD_INDEX.exists():
        fail("找不到 build/index.html，先执行：npm run build")

    try:
        import ortools  # noqa: F401
    except ImportError:
        fail("当前解释器没有 ortools，请用装了 OR-Tools 的 Python 3.12 运行")

    code = "" if args.open else (args.code.strip() or os.environ.get("DEMO_ACCESS_CODE", "").strip())
    if not code and not args.open:
        code = "".join(secrets.choice(ALPHABET) for _ in range(6))
    if code:
        os.environ["DEMO_ACCESS_CODE"] = code
    else:
        os.environ.pop("DEMO_ACCESS_CODE", None)

    # 演示默认多线程：单线程是为逐位可复现，12 项任务要 5 分钟才出解，
    # 演示时没人愿意等。8 线程实测同一目标值 1.1 秒收敛。
    cpu = os.cpu_count() or 1
    workers = args.workers or min(8, cpu)
    os.environ["MWA_SOLVER_WORKERS"] = str(workers)
    os.environ["MWA_SOLVE_SECONDS"] = str(max(5, args.seconds))
    # 体验额度：显式打开，否则后端默认不限额（本地开发/测试的默认值）。
    os.environ["DEMO_FREE_TURNS"] = str(max(0, args.free_turns))

    # 后端包在 backend/，而 dsl_core 这类共享模块在项目根，
    # 两个都要在搜索路径里，否则换个目录启动就 ModuleNotFoundError。
    for extra in (ROOT, BACKEND):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))

    import uvicorn

    base = f"http://127.0.0.1:{args.port}"
    print()
    print("  演示站点已就绪")
    print(f"  求解线程   {workers}" + ("（可复现模式，会慢很多）" if workers == 1 else "（演示模式，最快）"))
    print(f"  求解时限   {max(5, args.seconds)} 秒")
    print(
        "  体验额度   "
        + (f"每人 {args.free_turns} 轮，用完需自带 DeepSeek 密钥" if args.free_turns else "不限轮次")
    )
    if code:
        print(f"  本机打开   {base}/?k={code}")
        print(f"  访问口令   {code}")
        print(f"  健康检查   {base}/api/health")
        print()
        print("  要给别人看，另开一个终端跑：")
        print(f"      cpolar http {args.port}")
        print("  把 cpolar 给出的公网地址接上 /?k=口令，就是邀请链接。")
        print("  或者直接用 python scripts/demo_public.py，服务和隧道一起起、还自带公网自检。")
    else:
        print(f"  本机打开   {base}")
        print(f"  健康检查   {base}/api/health")
        print()
        print("  未设口令，同网络的人知道地址就能直接进。要对外演示请去掉 --open。")
    print()
    print("  换下一批人体验之前，先停服务，再跑：python scripts/demo_reset.py")
    print()

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
