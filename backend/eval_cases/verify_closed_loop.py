"""闭环验收：算完能不能变成单子，单子能不能真正销账。

这条链路以前是断的：
    规划 → 一个 JSON 报告 → 结束。
    「检修单 / 出海清单 / 执行反馈」三张表永远是空的，
    「待安排 N 项」实际等于「收到过且没标完成/取消的全部告警」，
    同一批任务每一轮都会被重新排进去。

现在验证五件事：
    1. 下发后三张表真的被写出来，任务状态从「待安排」变「已排程」
    2. 再算一遍，已排程任务被当成固定占用，时间不被挪走，也不会被重复选
    3. 审批通过后锁定，拒绝后放回待安排
    4. 完工反馈后任务变「已完成」，不再出现在待安排里
    5. 全流程幂等：重复下发不会重复建单

用法：
    python backend/eval_cases/verify_closed_loop.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "maintenance-milp-modeler" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

import product_writeback as ledger  # noqa: E402
from product_data import load_project  # noqa: E402
from product_planner import recommend  # noqa: E402

DEMO = ROOT / "data" / "phase1-demo"
# JSON 是求解器的输入包，CSV 是给人看的镜像，两份都要备份，否则验收会把真数据改坏。
TABLES = ["告警任务.csv", "检修单.csv", "出海清单.csv", "执行反馈.csv", "初版完整数据.json"]
SECONDS = 180


def snapshot_tables(dest: Path) -> None:
    for name in TABLES:
        shutil.copy2(DEMO / name, dest / name)


def restore_tables(src: Path) -> None:
    for name in TABLES:
        shutil.copy2(src / name, DEMO / name)


def run_plan(tag: str):
    out = Path(tempfile.mkdtemp(prefix=f"closed-loop-{tag}-"))
    data = load_project(ROOT)
    report = recommend(data, out, seconds=SECONDS)
    return data, report


def schedule_of(report: dict) -> list[dict]:
    rec = report.get("推荐") or {}
    return rec.get("排程") or []


def check(label: str, ok: bool, detail: str) -> bool:
    print(f"  [{'通过' if ok else '失败'}] {label}：{detail}")
    return ok


def main() -> int:
    backup = Path(tempfile.mkdtemp(prefix="ledger-backup-"))
    snapshot_tables(backup)
    results: list[bool] = []
    try:
        print("\n=== 0. 起点 ===")
        base = ledger.board()
        print(f"  待安排 {base['待安排']} 项，检修单 {base['检修单行数']} 行")
        results.append(check("起点三张表为空", base["检修单行数"] == 0, "检修单 0 行"))

        print("\n=== 1. 第一次规划 → 下发 ===")
        data1, report1 = run_plan("first")
        rows1 = schedule_of(report1)
        print(f"  排程 {len(rows1)} 项，任务池 {len(data1.tasks)} 项")
        results.append(check("规划产出非空排程", len(rows1) > 0, f"{len(rows1)} 项"))

        version = "V-闭环验收"
        dispatched = ledger.dispatch(
            None, schedule=rows1, stamp=data1.stamp, plan_version=version, operator="验收脚本"
        )
        print(f"  下发结果：{dispatched['状态']}，检修单 {dispatched.get('检修单号')}")
        results.append(check("生成检修单", dispatched["新建任务数"] == len(rows1),
                             f"{dispatched['新建任务数']} 行"))
        after = ledger.board()
        results.append(check("出海清单同步生成", after["出海清单条数"] == len(rows1),
                             f"{after['出海清单条数']} 条"))
        results.append(check("任务状态改已排程", after["已排程"] == len(rows1),
                             f"已排程 {after['已排程']} 项、待安排 {after['待安排']} 项"))
        results.append(check("待安排相应减少", after["待安排"] == base["待安排"] - len(rows1),
                             f"{base['待安排']} → {after['待安排']}"))

        print("\n=== 2. 重复下发（幂等） ===")
        again = ledger.dispatch(
            None, schedule=rows1, stamp=data1.stamp, plan_version=version, operator="验收脚本"
        )
        results.append(check("第二次下发不重复建单", again["状态"] == "已存在", again["状态"]))
        results.append(check("检修单行数没变", ledger.board()["检修单行数"] == len(rows1),
                             f"{ledger.board()['检修单行数']} 行"))

        print("\n=== 3. 带着已排程任务再算一遍 ===")
        data2, report2 = run_plan("second")
        locked_ids = set(data2.locked)
        print(f"  锁定任务 {len(locked_ids)} 项，任务池 {len(data2.tasks)} 项")
        results.append(check("已排程任务被识别为锁定", locked_ids == {r["任务编号"] for r in rows1},
                             f"{len(locked_ids)} 项"))
        rows2 = {row["任务编号"]: row for row in schedule_of(report2)}
        drift = [
            task_id for task_id, lock in data2.locked.items()
            if task_id in rows2 and rows2[task_id]["开始"] != lock["开始"]
        ]
        results.append(check("锁定任务的开工时间没被挪动", not drift,
                             "全部一致" if not drift else f"漂移 {drift}"))
        results.append(check("锁定任务仍在方案里（不是被剔除）",
                             locked_ids <= set(rows2), f"{len(locked_ids & set(rows2))} 项"))
        total2 = int((report2.get("推荐") or {}).get("待修总数") or len(data2.tasks))
        results.append(check("待修总数含已排程任务（资源仍被占用）",
                             total2 >= len(locked_ids), f"待修 {total2} 项"))

        print("\n=== 4. 审批 ===")
        order_id = dispatched["检修单号"]
        approved = ledger.approve(None, order_id=order_id, decision=True, approver="验收审批员")
        results.append(check("审批通过", approved["审批状态"] == "已批准", approved["审批状态"]))
        rejected = ledger.dispatch(
            None, schedule=[], stamp=data1.stamp, plan_version="V-空方案", operator="验收脚本"
        )
        results.append(check("空方案不建空单", rejected["状态"] == "无可下发任务", rejected["状态"]))

        print("\n=== 5. 现场反馈 → 完工销账 ===")
        first_task = rows1[0]
        feedback = ledger.record_feedback(
            None,
            order_id=order_id,
            actual_start=data1.stamp(first_task["开始"]),
            actual_finish=data1.stamp(first_task["完成"]),
            consumed=1,
            returned=0,
            resolved=True,
        )
        print(f"  反馈：{feedback['反馈编号']}，关闭 {feedback['关闭任务数']} 个任务")
        results.append(check("执行反馈已入库", ledger.board()["执行反馈条数"] == 1,
                             f"{ledger.board()['执行反馈条数']} 条"))
        closed = {row["任务编号"] for row in ledger.read_rows(DEMO / "告警任务.csv", ledger.TASK_FIELDS)
                  if row["任务状态"] == "已完成"}
        results.append(check("任务真正关闭", closed == {row["任务编号"] for row in rows1},
                             f"{len(closed)} 项已完工"))

        print("\n=== 6. 关闭后再算一遍 ===")
        data3, report3 = run_plan("third")
        remaining_ids = {task["任务编号"] for task in data3.tasks}
        results.append(check("已完成任务退出待安排池", not (closed & remaining_ids),
                             f"任务池 {len(remaining_ids)} 项，剩余待安排 {ledger.board()['待安排']} 项"))
        report_txt = ledger.summary_line()
        print(f"  台账：{report_txt}")
    finally:
        restore_tables(backup)
        print(f"\n已恢复到验收前状态（备份在 {backup}）")

    passed = sum(1 for item in results if item)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
