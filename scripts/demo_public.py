"""对外演示：本机跑服务，国内隧道出公网地址，一条命令拿到邀请链接。

    python scripts/demo_public.py
    python scripts/demo_public.py --code demo2026 --port 8010

前提：装好 cpolar 并登录过一次（cpolar authtoken 你的令牌）。
没装的话脚本会把安装包位置和步骤打出来。

它做的事：起后端 → 等健康检查通过 → 起 cpolar 隧道 → 抓公网地址 →
拼上口令 → 自己从公网回访问一次，确认别人能打开。
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_INDEX = ROOT / "build" / "index.html"
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
# cpolar 免费版给的是 xxx.r6.cpolar.top / xxx.cpolar.cn / xxx.vip.cpolar.cn 这类
URL_RE = re.compile(r"https://[a-zA-Z0-9][a-zA-Z0-9.-]*cpolar[a-zA-Z0-9.-]*")
CPOLAR_CANDIDATES = (
    "cpolar.exe",
    r"C:\Program Files\cpolar\cpolar.exe",
    r"C:\Program Files (x86)\cpolar\cpolar.exe",
)
SETUP_ZIP = ROOT / "scripts" / "bin" / "cpolar-setup.zip"


def fail(msg: str, hint: str = "") -> None:
    print(f"\n  [x] {msg}")
    if hint:
        print(f"      {hint}")
    print()
    raise SystemExit(1)


def find_cpolar() -> str | None:
    there = shutil.which("cpolar")
    if there:
        return there
    for raw in CPOLAR_CANDIDATES:
        p = Path(raw)
        if p.is_absolute() and p.exists():
            return str(p)
    home = Path.home()
    for p in (home / ".cpolar" / "cpolar.exe", home / "cpolar" / "cpolar.exe"):
        if p.exists():
            return str(p)
    return None


def token_configured() -> bool:
    for p in (Path.home() / ".cpolar" / "cpolar.yml", Path.home() / ".cpolar.yml"):
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "authtoken" in line and ":" in line:
                value = line.split(":", 1)[1].strip().strip("'\"")
                if value:
                    return True
    return False


def http_get(url: str, timeout: float = 8.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "demo-public-check"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def wait_ready(base: str, seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, body = http_get(f"{base}/api/health", timeout=3)
        if code == 200 and b'"ok"' in body:
            return True
        time.sleep(0.6)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="起服务 + 国内隧道，生成对外邀请链接")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--code", default="", help="访问口令；省略则随机生成 6 位")
    parser.add_argument("--seconds", type=int, default=60, help="单次求解时限")
    args = parser.parse_args()

    if not BUILD_INDEX.exists():
        fail("找不到 build/index.html，先执行：npm run build")

    cpolar = find_cpolar()
    if not cpolar:
        fail(
            "没找到 cpolar。先装一次：",
            f"解压并双击安装 {SETUP_ZIP}，然后注册账号并执行：cpolar authtoken 你的令牌",
        )
    if not token_configured():
        fail(
            "cpolar 还没登录（找不到 authtoken）。",
            "去 https://www.cpolar.com 注册，控制台复制 authtoken，然后执行：cpolar authtoken 你的令牌",
        )

    code = args.code.strip() or "".join(secrets.choice(ALPHABET) for _ in range(6))
    base = f"http://127.0.0.1:{args.port}"

    print()
    print("  正在起服务……")
    env = os.environ.copy()
    env["DEMO_ACCESS_CODE"] = code
    server = subprocess.Popen(
        [
            sys.executable, "-u",
            str(ROOT / "scripts" / "demo_serve.py"),
            "--port", str(args.port),
            "--code", code,
            "--seconds", str(args.seconds),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        if not wait_ready(base):
            err = ""
            if server.poll() is not None and server.stderr:
                err = (server.stderr.read() or "")[-600:]
            fail(f"服务没起来（端口 {args.port} 可能被占用）。", err)
        print(f"  服务就绪   {base}")

        print("  正在起隧道……")
        tunnel = subprocess.Popen(
            [cpolar, "http", str(args.port), "-log=stdout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        public_url = ""
        lines: list[str] = []

        def pump() -> None:
            nonlocal public_url
            assert tunnel.stdout is not None
            for line in tunnel.stdout:
                lines.append(line.rstrip())
                if not public_url:
                    m = URL_RE.search(line)
                    if m:
                        public_url = m.group(0)

        threading.Thread(target=pump, daemon=True).start()

        deadline = time.time() + 45
        while time.time() < deadline and not public_url and tunnel.poll() is None:
            time.sleep(0.5)

        if not public_url:
            tail = "\n".join(lines[-8:])
            tunnel.terminate()
            fail("隧道没给出公网地址。", tail or "（cpolar 没有任何输出，检查是否已登录）")

        invite = f"{public_url}/?k={code}"
        print(f"  隧道就绪   {public_url}")

        print("  正在从公网回访……")
        code_health, _ = http_get(f"{public_url}/api/health", timeout=20)
        code_page, _ = http_get(invite, timeout=45)
        if code_health == 200 and code_page == 200:
            print("  公网自检   通过")
        else:
            print(f"  公网自检   异常（health={code_health} page={code_page}），先把链接发出去试试")

        print()
        print("  ┌──────────────────────────────────────────────")
        print("  │ 邀请链接（发这个给人）")
        print(f"  │ {invite}")
        print("  ├──────────────────────────────────────────────")
        print(f"  │ 访问口令   {code}")
        print(f"  │ 本机打开   {base}/?k={code}")
        print(f"  │ 健康检查   {base}/api/health")
        print("  └──────────────────────────────────────────────")
        print()
        print("  换下一批人之前：Ctrl+C 停掉，再跑 python scripts/demo_reset.py")
        print("  这个窗口别关，一关别人就打不开了。")
        print()

        while tunnel.poll() is None and server.poll() is None:
            time.sleep(1)
        print("\n  隧道或服务已退出。")

    except KeyboardInterrupt:
        print("\n  正在收尾……")
    finally:
        for proc in ("tunnel", "server"):
            p = locals().get(proc)
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("  已停止。")


if __name__ == "__main__":
    main()
