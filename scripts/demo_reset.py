"""把演示状态恢复到出厂：换下一批人体验之前跑一次。

    python scripts/demo_reset.py

只做两件事：把 data/phase1-demo 恢复到 git 里的版本、删掉会话数据库。
求解产物 product-output 不动，只报体积，删不删你自己决定。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_REL = "data/phase1-demo"
DEMO_DIR = ROOT / "data" / "phase1-demo"
DB = ROOT / "data" / "agent.db"
OUTPUT = ROOT / "product-output"


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def restore_demo_data() -> str:
    if not DEMO_DIR.exists():
        return f"跳过：{DEMO_REL} 不存在"
    result = subprocess.run(
        ["git", "checkout", "--", DEMO_REL],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return f"恢复演示数据失败：{result.stderr.strip() or result.stdout.strip()}"
    return "演示数据已恢复（检修单/出海清单/执行反馈回到空表，任务回到待安排）"


def clear_database() -> str:
    removed = []
    blocked = []
    for suffix in ("", "-wal", "-shm"):
        path = DB.with_name(DB.name + suffix)
        if not path.exists():
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            blocked.append(path.name)
    if blocked:
        return f"删不掉 {'、'.join(blocked)}：后端可能还在跑，先停掉服务再重试"
    if not removed:
        return "会话数据库本来就是空的"
    return f"已删除 {'、'.join(removed)}（对话历史、方案版本、检修单记录一并清空）"


def main() -> int:
    print()
    print("  正在恢复演示状态……")
    print(f"  · {restore_demo_data()}")
    print(f"  · {clear_database()}")

    output_size = dir_size(OUTPUT)
    if output_size:
        count = len([d for d in OUTPUT.iterdir() if d.is_dir()]) if OUTPUT.exists() else 0
        print(f"  · 求解产物保留 {human(output_size)}（{count} 次运行），未删除")
        print("    要清理就手动删掉 product-output 目录，下次求解会自动重建")
    else:
        print("  · 没有求解产物")

    print()
    print("  数据已回到出厂。重新启动服务即可接待下一批人。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
