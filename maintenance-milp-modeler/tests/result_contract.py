"""结果级判分：查「算出来的方案满足用户要求」，不查「生成了同一个模型」。

为什么要有这一层
----------------
旧评测里有 `test_same_input_same_model_without_tie_break` 这类**等价性断言**：
同一句话必须生成同一个模型 / 同一个指纹。它挡得住「翻译漂移」，但会把
「合法换了一种建模方式、结果照样满足要求」误判成退化；反过来，模型一字未变、
结果却漏排了用户点名的任务，它也放过去。

大一修 11-12 线上事故给了一个更硬的教训：只写「1~8 之和 = 8」并不等于
「只做 1~8」，16 个任务照样能排 12 个。所以判分要落在**结果**上：
用户说的「只修这些」是排除语义，必须检查结果里有没有组外任务。

本模块只做两件事，不碰求解器：
1. `snapshot(result)` 把 `yxcore.pipeline.run()` 的结果抽成可比较的纯数据；
2. `check(result, requirement)` 按结构化要求逐条查结果，返回违例列表（空 = 通过）。

用途：结果级基线回归（`test_result_baseline.py`）、评测判分、以及将来
50 条线上矩阵重录时的判分口径。
"""
from __future__ import annotations

from typing import Any

# 排程行里要冻结的字段。多了会绑死无关细节，少了会漏掉「换窗口/换线」。
ROW_FIELDS = ("任务编号", "风机编号", "部件名称", "严重等级", "窗口编号", "作业线", "开始", "完成")
SUMMARY_FIELDS = ("完成任务数", "待修总数", "总成本", "现金支出", "船租", "人工", "备件", "发电损失", "船舶艘天")
SOLVE_FIELDS = ("解状态", "最优性", "终止原因", "模型成本")
FACT_FIELDS = ("规划时点", "时点来源", "告警水位线", "规划边界")

# check() 认识的要求键。写了不认识的键直接报错，避免「写了断言没人判」。
REQUIREMENT_KEYS = frozenset({
    "只修任务", "至多修", "至少修", "恰好修", "必修任务", "禁修任务",
    "预算上限", "最早开工不早于", "最晚完工不晚于", "未安排任务",
})


def schedule_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """抽排程行，按 (开始, 作业线, 任务编号) 排定，去掉字典顺序的偶然性。"""
    rows = [{k: row.get(k) for k in ROW_FIELDS} for row in result["排程"]]
    return sorted(rows, key=lambda r: (r["开始"], r["作业线"], r["任务编号"]))


def snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """把一次求解抽成可冻结、可逐字段比较的结果快照。

    只取「用户能感知的结果」，不含模型指纹、产物文件、运行耗时这类
    实现细节——那些变了不该让基线变红。
    """
    rec = result["推荐"]
    return {
        "排程": schedule_rows(result),
        "未安排": sorted(row["任务编号"] for row in rec["未安排"]),
        "汇总": {k: rec[k] for k in SUMMARY_FIELDS},
        "求解": {k: result["求解"].get(k) for k in SOLVE_FIELDS},
        "事实层": {k: result["数据"].get(k) for k in FACT_FIELDS},
        "部分排程": bool(result.get("部分排程")),
    }


def check(result: dict[str, Any], requirement: dict[str, Any]) -> list[str]:
    """按结构化要求查结果，返回人类可读的违例说明；空列表表示满足要求。"""
    unknown = set(requirement) - REQUIREMENT_KEYS
    if unknown:
        raise ValueError(f"未知要求键 {sorted(unknown)}；可用：{sorted(REQUIREMENT_KEYS)}")

    rows = schedule_rows(result)
    placed = {r["任务编号"] for r in rows}
    undone = {row["任务编号"] for row in result["推荐"]["未安排"]}
    total = result["推荐"]["待修总数"]
    problems: list[str] = []

    if "只修任务" in requirement:
        wanted = set(requirement["只修任务"])
        # 排除语义：既不许少做点名的，也不许多做没点名的。
        # 只写「这些之和 = N」做不到后半句，这正是要防的陷阱。
        missing = sorted(wanted - placed)
        extra = sorted(placed - wanted)
        if missing:
            problems.append(f"只修这些：点名的任务没排上 {missing}")
        if extra:
            problems.append(f"只修这些：结果里出现了组外任务 {extra}（Σx=N 不等于只做 N 个）")

    if "至多修" in requirement and len(rows) > requirement["至多修"]:
        problems.append(f"至多修 {requirement['至多修']} 个，实际排了 {len(rows)} 个")
    if "至少修" in requirement and len(rows) < requirement["至少修"]:
        problems.append(f"至少修 {requirement['至少修']} 个，实际排了 {len(rows)} 个")
    if "恰好修" in requirement and len(rows) != requirement["恰好修"]:
        problems.append(f"恰好修 {requirement['恰好修']} 个，实际排了 {len(rows)} 个")

    if "必修任务" in requirement:
        missing = sorted(set(requirement["必修任务"]) - placed)
        if missing:
            problems.append(f"必修任务没进方案 {missing}")
    if "禁修任务" in requirement:
        present = sorted(set(requirement["禁修任务"]) & placed)
        if present:
            problems.append(f"禁修任务被排进方案 {present}")

    if "预算上限" in requirement:
        cost = result["推荐"]["总成本"]
        if cost > requirement["预算上限"]:
            problems.append(f"总成本 {cost} 超过预算上限 {requirement['预算上限']}")
    if "最早开工不早于" in requirement:
        early = [r["任务编号"] for r in rows if r["开始"] < requirement["最早开工不早于"]]
        if early:
            problems.append(f"以下任务开工早于约定时刻 {early}")
    if "最晚完工不晚于" in requirement:
        late = [r["任务编号"] for r in rows if r["完成"] > requirement["最晚完工不晚于"]]
        if late:
            problems.append(f"以下任务完工晚于约定时刻 {late}")

    if "未安排任务" in requirement:
        expected = set(requirement["未安排任务"])
        if undone != expected:
            problems.append(f"未安排任务应为 {sorted(expected)}，实际 {sorted(undone)}")

    # 一致性自洽：排了的 + 没排的 = 台账里的任务数。
    if placed & undone:
        problems.append(f"同一任务既排了又列为未安排 {sorted(placed & undone)}")
    if len(placed) + len(undone) != total:
        problems.append(f"排程 {len(placed)} + 未安排 {len(undone)} != 待修总数 {total}")

    return problems
