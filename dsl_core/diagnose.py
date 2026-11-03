"""失败诊断：没算出来的时候，告诉用户**为什么**——而且只用算得出来的事实。

事故背景是两类反馈：

1. "求解器没有在满足这些约束的前提下找到解；请放宽约束" —— 这句话没错，
   但等于什么都没说。用户不知道是哪条约束、差多少。
2. 模型看到"没找到解"之后自己编一句"因为工时不够"—— 系统没说过这句话，
   用户却把它当成解释。

所以这里只做一件事：把"不可能"翻译成可复算的证据。三条纪律：

- **只报算得出来的**。每一项检查都必须是纯 Python 能判定的（任务有没有候选、
  两条约束是否直接矛盾、最乐观的供给够不够）。算不出来就说"未能定位"。
- **标明判定强度**。``已证明`` 是逻辑推导（一定成立），``上界证明`` 是用
  "最乐观假设"推出的不可能（再乐观也不行，所以结论仍然确定）。不写
  "可能""大概"这类没有证据的措辞。
- **数字必须来自数据/模型**。证据里出现的每个数，都能用同一次求解的输入
  复算出来；不引用模型生成的自然语言。

返回结构：
    {
      "结论": "不可行" | "未算完" | "未能定位",
      "原因": [{"判定": "已证明"|"上界证明", "说明": str, "证据": str}],
      "建议": [str, ...],
    }
"""

from __future__ import annotations

from typing import Any

from .registry.entities import resolve_minute, resolve_task

PROVEN = "已证明"
UPPER_BOUND = "上界证明"
UNKNOWN = "未能定位"

# 窗口内可用的天数上限取整时，宁可多算一天，也不要因为少算一天而误报"不够"。
_MINUTES_PER_DAY = 1440


def _minutes(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _shortest_work_minutes(options: dict[str, list[dict]]) -> dict[str, int]:
    """每个任务在所有候选里最短的一次作业时长（分钟）。取最短 = 最乐观。"""
    result: dict[str, int] = {}
    for task_id, candidates in options.items():
        spans = [
            sum(_minutes(b) - _minutes(a) for a, b in (candidate.get("作业段") or []))
            for candidate in candidates
        ]
        spans = [span for span in spans if span > 0]
        if spans:
            result[task_id] = min(spans)
    return result


def _required_tasks(dsl: dict, data, partial: bool) -> tuple[list[str], str]:
    """这次必须排进去的任务，以及这个名单是怎么来的（要写进证据）。"""
    constraints = dsl.get("constraints") or []
    must_tasks: list[str] = []
    for spec in constraints:
        if spec.get("type") != "must":
            continue
        if spec.get("task"):
            resolved = resolve_task(data, spec["task"])
            if resolved:
                must_tasks.append(resolved)
            continue
        filter_spec = spec.get("filter") or {}
        from .compiler import task_matches

        must_tasks.extend(
            task["任务编号"] for task in data.tasks if task_matches(task, filter_spec, data)
        )
    if must_tasks:
        return sorted(set(must_tasks)), "must 点名的任务"
    if partial:
        # 部分排程不强制任何任务，给不出"必须"的名单。
        return [], ""
    return [task["任务编号"] for task in data.tasks], "完整模式要求每个任务都排入"


def _conflicts(dsl: dict, data) -> list[dict[str, str]]:
    """约束之间的直接矛盾：纯逻辑，不需要求解。"""
    found: list[dict[str, str]] = []
    constraints = dsl.get("constraints") or []

    must: set[str] = set()
    excluded: set[str] = set()
    for spec in constraints:
        if spec.get("type") == "must" and spec.get("task"):
            resolved = resolve_task(data, spec["task"])
            if resolved:
                must.add(resolved)
        if spec.get("type") == "must" and spec.get("filter"):
            from .compiler import task_matches

            must.update(
                task["任务编号"]
                for task in data.tasks
                if task_matches(task, spec["filter"] or {}, data)
            )
        if spec.get("type") in ("exclude", "at_most_one"):
            if spec.get("type") == "exclude" and spec.get("task"):
                resolved = resolve_task(data, spec["task"])
                if resolved:
                    excluded.add(resolved)
    both = sorted(must & excluded)
    if both:
        found.append(
            {
                "判定": PROVEN,
                "说明": "同一个任务既要求必修、又要求排除，这两条不可能同时成立",
                "证据": "冲突任务：" + "、".join(both),
            }
        )

    for spec in constraints:
        if spec.get("type") != "at_most_one":
            continue
        tasks = {resolve_task(data, ref) for ref in spec.get("tasks") or []}
        tasks.discard(None)
        overlap = sorted(tasks & must)
        if len(overlap) > 1:
            # at_most_one 会自动切部分排程，但 must 会把它重新顶回"都要排"。
            found.append(
                {
                    "判定": PROVEN,
                    "说明": (
                        "「最多挑一个」和「这些任务必须修」互相顶："
                        f"at_most_one 圈了 {len(tasks)} 个，其中 {len(overlap)} 个被 must 要求必修"
                    ),
                    "证据": "被 must 要求必修的：" + "、".join(overlap),
                }
            )
    return found


def _capacity_bound(dsl: dict, data, options: dict[str, list[dict]], required: list[str]) -> list[dict[str, str]]:
    """日配额的最乐观供给够不够。

    只对 line 维度算：一条线一天能提供的工时是「每天最多维修小时数」，
    窗口有多少天就有多少份。若 limit 条线在最乐观情况下（每个任务都取最短
    作业时长）也装不下必须排的任务，那就是真的装不下。
    """
    found: list[dict[str, str]] = []
    constraints = dsl.get("constraints") or []
    if not required or not options:
        return found
    windows = list(getattr(data, "windows", None) or [])
    if not windows:
        return found
    days = sum(
        max(1, (_minutes(window["结束"]) - _minutes(window["开始"])) // _MINUTES_PER_DAY)
        for window in windows
    )
    per_line_hours = float(data.params.get("每天最多维修小时数") or 0)
    if per_line_hours <= 0:
        return found
    shortest = _shortest_work_minutes(options)
    demand_hours = sum(shortest.get(task, 0) for task in required) / 60.0
    if demand_hours <= 0:
        return found

    for spec in constraints:
        if spec.get("type") != "capacity" or spec.get("resource") != "line":
            continue
        limit = _minutes(spec.get("limit"))
        supply_hours = limit * days * per_line_hours
        if demand_hours > supply_hours:
            found.append(
                {
                    "判定": UPPER_BOUND,
                    "说明": (
                        f"上限 {limit} 条线，即使每天都跑满、每个任务都按最短时长算，也装不下"
                    ),
                    "证据": (
                        f"需要 {demand_hours:.1f} 小时；"
                        f"最乐观供给 = {limit} 条线 × {days} 天 × {per_line_hours:g} 小时"
                        f" = {supply_hours:.1f} 小时"
                    ),
                }
            )
    return found


def _starved(dsl: dict, data, options: dict[str, list[dict]], required: list[str]) -> list[dict[str, str]]:
    """必须排、但在当前数据下连一个可选开工时刻都没有的任务。

    候选是按窗口 × 工时 × 截止时间生成的。一个任务如果在这段时间里根本干不完，
    候选数就是 0 —— 它连"被选中"的机会都没有，任何约束都救不回来。
    """
    starved = [task for task in required if not options.get(task)]
    if not starved:
        return []
    evidence = []
    for task_id in starved:
        task = next((t for t in data.tasks if t["任务编号"] == task_id), None)
        if task is None:
            evidence.append(f"{task_id}（不在当前快照）")
            continue
        evidence.append(
            f"{task_id}（{task.get('风机编号')}/{task.get('部件名称')}"
            f"/{task.get('严重等级')}严重）"
        )
    return [
        {
            "判定": PROVEN,
            "说明": "这些任务在窗口内没有任何可行的开工时刻（工时、窗口、截止时间三者挤没了）",
            "证据": "无候选任务：" + "、".join(evidence),
        }
    ]


def _time_window_facts(dsl: dict, data) -> list[dict[str, str]]:
    """同任务的 deadline 早于 earliest：时间窗是空的。"""
    found: list[dict[str, str]] = []
    deadlines: dict[str, int] = {}
    earlies: dict[str, int] = {}
    for spec in dsl.get("constraints") or []:
        if spec.get("type") == "deadline":
            task = resolve_task(data, spec.get("task"))
            minute = resolve_minute(data, spec.get("time"))
            if task and minute is not None:
                deadlines[task] = minute
        if spec.get("type") == "earliest":
            task = resolve_task(data, spec.get("task"))
            minute = resolve_minute(data, spec.get("time"))
            if task and minute is not None:
                earlies[task] = minute
    for task in sorted(set(deadlines) & set(earlies)):
        if deadlines[task] < earlies[task]:
            found.append(
                {
                    "判定": PROVEN,
                    "说明": f"{task} 要求最早开工晚于最晚完工，时间窗为空",
                    "证据": (
                        f"最晚完工 {deadlines[task]} 分钟 < 最早开工 {earlies[task]} 分钟"
                    ),
                }
            )
    return found


def _budget_bound(dsl: dict, data, options: dict[str, list[dict]], required: list[str]) -> list[dict[str, str]]:
    """预算下界：每个任务都挑最便宜的那个，加起来的钱还超预算，就是真超。

    这里只算**直接成本**（人工 + 维修包 + 其他费用），不含停机损失和船租。
    直接成本是总成本的严格下界，所以"下界 > 预算"是确定的不可能；
    反过来"下界 ≤ 预算"什么都证明不了——那种情况不写。
    """
    found: list[dict[str, str]] = []
    labor = float(data.params.get("人工单价（元每人时）") or 0)
    for spec in dsl.get("constraints") or []:
        if spec.get("type") != "budget" or spec.get("op") != "<=":
            continue
        quantity = str(spec.get("quantity"))
        if quantity not in ("总经济成本", "现金支出"):
            continue
        budget = float(spec.get("value") or 0)
        lower = 0.0
        priced: list[str] = []
        for task_id in required:
            candidates = options.get(task_id) or []
            if not candidates:
                continue
            costs = []
            for candidate in candidates:
                hours = sum(_minutes(b) - _minutes(a) for a, b in candidate.get("作业段") or []) / 60.0
                part = data.parts[candidate["部件编号"]]
                costs.append(
                    hours * labor
                    + float(part.get("维修包成本（元每套）", 0))
                    + float(part.get("其他费用（元每任务）", 0))
                )
            if costs:
                lower += min(costs)
                priced.append(task_id)
        if priced and lower > budget:
            found.append(
                {
                    "判定": UPPER_BOUND,
                    "说明": f"{quantity}上限 {budget:g} 元，连直接成本都盖不住",
                    "证据": (
                        f"{len(priced)} 个具体任务各挑最便宜的做法，直接成本合计 "
                        f"{lower:.0f} 元（不含停机损失与船租，只是下界）"
                    ),
                }
            )
    return found


def _cardinality_bound(dsl: dict, data, options: dict[str, list[dict]], required: list[str]) -> list[dict[str, str]]:
    """要求完成 N 个，但够格的任务本来就不够 N 个。"""
    found: list[dict[str, str]] = []
    from .compiler import task_matches

    for spec in dsl.get("constraints") or []:
        if spec.get("type") != "cardinality" or spec.get("op") not in (">=", "=="):
            continue
        need = int(spec.get("value") or 0)
        filter_spec = spec.get("filter") or {}
        if filter_spec:
            pool = [
                task["任务编号"]
                for task in data.tasks
                if task_matches(task, filter_spec, data)
            ]
            scope = "命中过滤条件的任务"
        else:
            pool = required or [task["任务编号"] for task in data.tasks]
            scope = "必须排入的任务"
        schedulable = [task_id for task_id in pool if options.get(task_id)]
        if need > len(schedulable):
            found.append(
                {
                    "判定": PROVEN,
                    "说明": f"要求完成 {need} 个，但能排进去的只有 {len(schedulable)} 个",
                    "证据": (
                        f"{scope}共 {len(pool)} 个，其中有可行开工时刻的 {len(schedulable)} 个"
                        + (f"；没有候选的：{'、'.join(sorted(set(pool) - set(schedulable)))}"
                           if len(pool) > len(schedulable) else "")
                    ),
                }
            )
    return found


def diagnose(
    *,
    dsl: dict[str, Any],
    data: Any,
    options: dict[str, list[dict]],
    info: dict[str, Any],
    partial: bool = False,
) -> dict[str, Any]:
    """没产出方案时，给一份可复算的原因说明。"""
    status = str(info.get("解状态") or "")
    reason = str(info.get("终止原因") or "")
    elapsed = info.get("总耗时秒")
    budget = (info.get("求解器参数") or {}).get("max_time_in_seconds")

    # 求解器自己说"没算完"，那就不是无解。这一条必须先判：
    # 把 UNKNOWN 讲成 INFEASIBLE，会让用户白改一通本来没问题的约束。
    if status == "UNKNOWN" or reason == "TIME_LIMIT":
        evidence = f"用时 {elapsed:.1f} 秒" if isinstance(elapsed, (int, float)) else "用时未知"
        if budget:
            evidence += f"，时限 {budget} 秒"
        return {
            "结论": "未算完",
            "原因": [
                {
                    "判定": PROVEN,
                    "说明": "求解器在时限内没有给出解，也没有证明无解；这不是「不可行」",
                    "证据": evidence,
                }
            ],
            "建议": [
                "放宽求解时限再跑一次（这是唯一能区分「算不完」和「无解」的办法）",
                "如果要更快出结果，可以缩小题量或放宽约束",
            ],
        }

    constraints = dsl.get("constraints") or []
    if not constraints:
        return {
            "结论": "未能定位",
            "原因": [
                {
                    "判定": UNKNOWN,
                    "说明": "这次没有附加约束，求解仍然没有解；系统没有能力进一步定位",
                    "证据": f"解状态 {status or '未知'}，终止原因 {reason or '未知'}",
                }
            ],
            "建议": ["检查基座数据是否完整（任务、窗口、备件库存）"],
        }

    required, required_reason = _required_tasks(dsl, data, partial)
    checks: list[dict[str, str]] = []
    checks.extend(_conflicts(dsl, data))
    checks.extend(_time_window_facts(dsl, data))
    checks.extend(_starved(dsl, data, options, required))
    checks.extend(_capacity_bound(dsl, data, options, required))
    checks.extend(_budget_bound(dsl, data, options, required))
    checks.extend(_cardinality_bound(dsl, data, options, required))

    if not checks:
        names = [str(spec.get("type")) for spec in constraints]
        return {
            "结论": "未能定位",
            "原因": [
                {
                    "判定": UNKNOWN,
                    "说明": (
                        "逐项检查（直接矛盾、时间窗为空、任务无候选、配额上限、"
                        "成本下界、完成数上限）都没有命中；"
                        "系统没有能力把这次无解归到某一条约束上"
                    ),
                    "证据": f"约束：{names}；解状态 {status or '未知'}",
                }
            ],
            "建议": [
                "逐条去掉一条约束重跑，能最快定位是哪条",
                "如果去掉某条就可行，那条就是原因；如果都不行，问题在基座数据",
            ],
        }

    advice = ["按上面列出的证据调整；系统只报了算得出来的原因，没算出来的不会写在这里"]
    if required_reason:
        advice.insert(0, f"当前「必须排入」的名单来自：{required_reason}")
    return {"结论": "不可行", "原因": checks, "建议": advice}


def describe(diagnosis: dict[str, Any], limit: int = 4) -> str:
    """一行给用户看的结论（供日志/兜底文案用；界面按结构展示更好）。"""
    reasons = diagnosis.get("原因") or []
    if not reasons:
        return str(diagnosis.get("结论") or "未能定位")
    lines = [str(diagnosis.get("结论") or "未能定位")]
    for item in reasons[:limit]:
        lines.append(f"[{item.get('判定')}] {item.get('说明')}（{item.get('证据')}）")
    return "；".join(lines)
