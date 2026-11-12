"""重录结果级基线（人工复核后才能跑，不要当成日常脚本）。

用法（仓库根目录）：
    MWA_SOLVER_WORKERS=1 py -3.12 maintenance-milp-modeler/tests/record_result_baseline.py

什么时候才该重录
----------------
基线红了先问「为什么」：目标函数、归一化分母、候选生成、数据台账里任何一处
有意改动，都会让排程结果变化，这时**人工确认新结果满足用户要求**，再重录。
不要为了「让测试变绿」直接刷基线——那等于把回归防线擦掉。

重录后请在提交说明里写清：改了哪处、旧结果是什么、新结果为什么更对。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# 单线程必须在导入求解器之前定下来：并行搜索在超时截断时可能取到不同中间解。
os.environ["MWA_SOLVER_WORKERS"] = "1"

ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from result_contract import snapshot  # noqa: E402
from yxcore.pipeline import run as yx_run  # noqa: E402

# 两个断点：一个快且覆盖「全修」，一个是线上演示钉住的时点、覆盖「少修」。
BREAKPOINTS = [
    {
        "名称": "覆盖期起点（全修）",
        "规划时点": "2026-10-23T18:00:00+08:00",
        "时限秒": 8,
        "选它的理由": "台账首批 11 条，求解快，能挡住目标函数/归一化分母的改动",
    },
    {
        "名称": "演示钉住时点（少修）",
        "规划时点": "2026-11-08T18:00:00+08:00",
        "时限秒": 30,
        "选它的理由": "线上演示钉在这一天，15 条告警按代价权衡排 13 条，覆盖不修分支",
    },
]


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    entries = []
    for bp in BREAKPOINTS:
        print(f"录制 {bp['名称']} @ {bp['规划时点']} ({bp['时限秒']}s) ...", flush=True)
        with tempfile.TemporaryDirectory() as out:
            result = yx_run([], as_of=bp["规划时点"], seconds=bp["时限秒"], output=Path(out))
        snap = snapshot(result)
        entries.append({**bp, "快照": snap})
        print(f"  完成 {snap['汇总']['完成任务数']}/{snap['汇总']['待修总数']}，"
              f"解状态={snap['求解']['解状态']}，最优性={snap['求解']['最优性']}", flush=True)

    payload = {
        "基线版本": "1.0",
        "说明": (
            "结果级基线：固定台账快照 + 固定参数，冻结每个任务排在哪个窗口、哪条作业线、"
            "几点开工，以及没排的任务和费用汇总。用于证明后续改动有没有改变排程结果。"
            "锁的是「算出来的这份方案」，不是「已证明最优」——两个断点当前都是 FEASIBLE / 未证明最优。"
        ),
        "重录命令": "MWA_SOLVER_WORKERS=1 py -3.12 maintenance-milp-modeler/tests/record_result_baseline.py",
        "重录纪律": "基线变红先查原因；人工确认新结果满足用户要求后重录，并在提交说明里交代差异。",
        "代码提交": git_head(),
        "求解配置": {"策略": "成本最低优先", "求解线程": 1, "随机种子": 17},
        "基线": entries,
    }
    target = TESTS / "result_baseline.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
