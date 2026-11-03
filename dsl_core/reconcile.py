"""意图对账：DSL 的每一条诉求 → 最终产物，逐条核对。

和 `product_planner.verify_schedule` 分工不同，两者不能互相替代：

- `verify_schedule` 查的是**数学与作业自洽**：窗口、工时、转场、库存、同机冲突。
  它保证这份排程"自己站得住"，不保证它"是你要的那份"。
- 本模块查的是**说的话有没有做到**：用户要排除的任务是否真的没排、要改的参数
  是否真的进了模型、声明了几个优化阶段就跑了几个。

事故背景：2026-10-30 那次"只修前 8 个"，复核通过了，但复核的是一个根本没
包含排除约束的模型。`verify_schedule` 从头到尾不可能发现这件事——它不知道
用户说过什么。所以这里只做一件事：拿 DSL 原文逐条对产物，对不上就列出来。

对账结果三态：
- 通过：产物里能找到**证据**证明这条诉求生效了
- 违例：找到了反证，或产物与诉求直接矛盾
- 未核对：缺少核对所需的输入（例如没有成本口径），不当成通过
"""

from __future__ import annotations

from typing import Any

from .registry.entities import resolve_day, resolve_minute, resolve_task
from .registry.parameters import to_internal_key
from .registry.quantities import resolve_quantity

PASS = "通过"
FAIL = "违例"
NA = "不适用"
UNVERIFIED = "未核对"

# 量引用里的过滤维度 → 排程行字段
_FILTER_FIELDS = {
    "task": "任务编号",
    "turbine": "风机编号",
    "part": "部件名称",
    "severity": "严重等级",
    "window": "窗口编号",
    "line": "作业线",
}

# budget 的口径名 → 报告里的成本字段
_COST_FIELDS = {"总经济成本": "总成本", "现金支出": "现金支出"}


def _schedule(report: dict[str, Any]) -> list[dict[str, Any]]:
    rec = report.get("推荐") or {}
    rows = rec.get("排程")
    if rows is None:
        rows = report.get("排程")
    return list(rows or [])


def _rows_by_task(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("任务编号")): row for row in _schedule(report)}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _op_ok(actual: Any, op: str, target: Any, tol: float = 1e-6) -> bool | None:
    left, right = _num(actual), _num(target)
    if left is None or right is None:
        return None
    if op == "<=":
        return left <= right + tol
    if op == ">=":
        return left >= right - tol
    if op == "==":
        return abs(left - right) <= tol
    return None


def _same(left: Any, right: Any, tol: float = 1e-6) -> bool:
    a, b = _num(left), _num(right)
    if a is not None and b is not None:
        return abs(a - b) <= tol
    return str(left) == str(right)


def _matching(rows: list[dict[str, Any]], filter_spec: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """按 DSL 的过滤维度筛排程行；遇到不认识的维度返回 None（未核对）。"""
    spec = filter_spec or {}
    matched: list[dict[str, Any]] = []
    for row in rows:
        ok = True
        for dim, value in spec.items():
            if dim == "day":
                ok = int(row.get("开始") or 0) // 1440 == int(value)
            else:
                field = _FILTER_FIELDS.get(dim)
                if field is None:
                    return None
                ok = str(row.get(field)) == str(value)
            if not ok:
                break
        if ok:
            matched.append(row)
    return matched


def _named_value(name: str, rows: list[dict[str, Any]], rec: dict[str, Any]) -> float | None:
    if name in ("总经济成本", "总成本"):
        return _num(rec.get("总成本"))
    if name == "现金支出":
        return _num(rec.get("现金支出"))
    if name == "完成任务数":
        return float(len(rows))
    if name == "最后完工时间":
        return float(max((int(r.get("完成") or 0) for r in rows), default=0))
    if name == "完工时间和":
        return float(sum(int(r.get("完成") or 0) for r in rows))
    for level in ("高", "中", "低"):
        if name == f"{level}严重完成数":
            return float(sum(1 for r in rows if str(r.get("严重等级")) == level))
        if name == f"{level}严重完工时间和":
            return float(
                sum(int(r.get("完成") or 0) for r in rows if str(r.get("严重等级")) == level)
            )
    return None


def _ref_value(ref: Any, rows: list[dict[str, Any]], rec: dict[str, Any]) -> float | None:
    if isinstance(ref, str):
        return _named_value(ref, rows, rec)
    resolved = resolve_quantity(ref)
    if isinstance(resolved, dict):
        form = resolved.get("form", "count")
        coeff = resolved.get("coeff", "one")
        filter_spec = resolved.get("filter") or {}
    elif isinstance(resolved, tuple):
        form, coeff, filter_spec = resolved
    else:
        return None
    selected = _matching(rows, filter_spec)
    if selected is None:
        return None
    if form == "count":
        return float(len(selected))
    if form == "weighted_sum":
        if coeff == "finish":
            return float(sum(int(r.get("完成") or 0) for r in selected))
        if coeff == "start":
            return float(sum(int(r.get("开始") or 0) for r in selected))
        if coeff == "day":
            return float(sum(int(r.get("开始") or 0) // 1440 for r in selected))
        # 成本类小计能从推荐报告整份读出，但只在 filter 为空时成立：
        # 「人工」是整份方案的合计，筛"高严重"就得逐行重算，而排程行里没有
        # 人工单价，重算不出来。那种情况返回 None（未核对），不猜一个数。
        cost_field = {"labor": "人工", "material": "备件", "other_fee": "其他费用"}.get(coeff)
        if cost_field and not filter_spec and rec.get(cost_field) is not None:
            return float(rec[cost_field])
    return None


def _item(label: str, verdict: str, evidence: str) -> dict[str, str]:
    return {"诉求": label, "结论": verdict, "证据": evidence}


def _parameter_checks(
    dsl: dict[str, Any],
    report: dict[str, Any],
    data: Any,
    overrides: dict[str, Any] | None,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    recorded = ((report.get("数据") or {}).get("参数覆盖")) or {}
    for spec in dsl.get("parameters") or []:
        field = str(spec.get("field") or "")
        internal = to_internal_key(field)
        label = f"参数 {field} {spec.get('op')} {spec.get('value')}"
        expected = None
        if overrides:
            expected = overrides.get(internal)
        elif spec.get("op") == "=":
            expected = spec.get("value")
        if expected is None:
            checks.append(_item(label, UNVERIFIED, "编译结果里没有这个参数的落点，无法确认改到了哪"))
            continue
        actual = None
        if data is not None and hasattr(data, "params"):
            actual = data.params.get(internal)
        elif internal in recorded:
            actual = recorded.get(internal)
        if actual is None:
            checks.append(_item(label, FAIL, f"模型输入里查不到 {internal}，参数没有生效"))
            continue
        if _same(actual, expected):
            checks.append(_item(label, PASS, f"模型输入 {internal} = {actual}"))
        else:
            checks.append(
                _item(label, FAIL, f"期望 {internal} = {expected}，模型输入里是 {actual}")
            )
    return checks


def _constraint_checks(
    dsl: dict[str, Any],
    report: dict[str, Any],
    data: Any,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    rows = list(_rows_by_task(report).values())
    rec = report.get("推荐") or {}

    def scheduled(task_id: str) -> dict[str, Any] | None:
        return next((r for r in rows if str(r.get("任务编号")) == task_id), None)

    for index, spec in enumerate(dsl.get("constraints") or [], 1):
        ctype = str(spec.get("type") or "")
        where = f"约束{index} {ctype}"
        try:
            if ctype == "must":
                if "task" in spec:
                    task_id = resolve_task(data, spec["task"])
                    hit = scheduled(task_id)
                    checks.append(
                        _item(
                            f"{where}：{task_id} 必须修",
                            PASS if hit else FAIL,
                            "已排入" if hit else "排程里没有这个任务",
                        )
                    )
                else:
                    filter_spec = spec.get("filter") or {}
                    if data is None:
                        checks.append(_item(f"{where}：按条件必做", UNVERIFIED, "缺少任务表"))
                        continue
                    from .compiler import task_matches

                    targets = [
                        str(t["任务编号"])
                        for t in data.tasks
                        if task_matches(t, filter_spec, data)
                    ]
                    missing = [t for t in targets if not scheduled(t)]
                    checks.append(
                        _item(
                            f"{where}：{filter_spec} 全部要修（{len(targets)} 项）",
                            PASS if targets and not missing else FAIL,
                            "全部排入"
                            if targets and not missing
                            else ("没有匹配到任务" if not targets else "未排入：" + "、".join(missing)),
                        )
                    )
            elif ctype == "exclude":
                task_id = resolve_task(data, spec["task"])
                hit = scheduled(task_id)
                checks.append(
                    _item(
                        f"{where}：{task_id} 不安排",
                        FAIL if hit else PASS,
                        "排程里仍然有它" if hit else "排程里没有它",
                    )
                )
            elif ctype == "fix":
                task_id = resolve_task(data, spec["task"])
                hit = scheduled(task_id)
                line = int(spec["line"])
                if hit is None:
                    checks.append(
                        _item(f"{where}：{task_id} 固定作业线{line}", PASS, "未排入，约束未被违反")
                    )
                else:
                    checks.append(
                        _item(
                            f"{where}：{task_id} 固定作业线{line}",
                            PASS if int(hit.get("作业线")) == line else FAIL,
                            f"实际作业线{hit.get('作业线')}",
                        )
                    )
            elif ctype in ("deadline", "earliest"):
                task_id = resolve_task(data, spec["task"])
                minute = resolve_minute(data, spec["time"])
                hit = scheduled(task_id)
                if hit is None:
                    checks.append(
                        _item(f"{where}：{task_id} {spec['time']}", PASS, "未排入，约束未被违反")
                    )
                elif ctype == "deadline":
                    ok = int(hit.get("完成") or 0) <= minute
                    checks.append(
                        _item(
                            f"{where}：{task_id} 不晚于 {spec['time']} 完成",
                            PASS if ok else FAIL,
                            f"实际完成分钟 {hit.get('完成')}（界 {minute}）",
                        )
                    )
                else:
                    ok = int(hit.get("开始") or 0) >= minute
                    checks.append(
                        _item(
                            f"{where}：{task_id} 不早于 {spec['time']} 开工",
                            PASS if ok else FAIL,
                            f"实际开始分钟 {hit.get('开始')}（界 {minute}）",
                        )
                    )
            elif ctype == "forbidden_dates":
                days = {resolve_minute(data, day) // 1440 for day in spec["dates"]}
                offenders = [
                    str(r.get("任务编号"))
                    for r in rows
                    if int(r.get("开始") or 0) // 1440 in days
                ]
                checks.append(
                    _item(
                        f"{where}：{'、'.join(spec['dates'])} 不开工",
                        FAIL if offenders else PASS,
                        "这些任务在那天开工：" + "、".join(offenders)
                        if offenders
                        else "没有任务在这些天开工",
                    )
                )
            elif ctype == "precedence":
                before = resolve_task(data, spec["before"])
                after = resolve_task(data, spec["after"])
                gap = int(spec.get("gap") or 0)
                a, b = scheduled(before), scheduled(after)
                if a is None or b is None:
                    checks.append(
                        _item(
                            f"{where}：{before} 先于 {after}",
                            NA,
                            "两者没有同时排入，按条件语义不适用（注意：编译侧会强制两者都排）",
                        )
                    )
                else:
                    ok = int(a.get("完成") or 0) + gap <= int(b.get("开始") or 0)
                    checks.append(
                        _item(
                            f"{where}：{before} 完成后 {gap} 分钟才轮到 {after}",
                            PASS if ok else FAIL,
                            f"{a.get('完成')} + {gap} vs {b.get('开始')}",
                        )
                    )
            elif ctype == "co_occur":
                tasks = [resolve_task(data, ref) for ref in spec["tasks"]]
                hits = [scheduled(t) for t in tasks]
                if any(hit is None for hit in hits):
                    checks.append(
                        _item(
                            f"{where}：{'、'.join(tasks)} {spec['mode']}",
                            NA,
                            "没有全部排入，按条件语义不适用（注意：编译侧会强制全部排入）",
                        )
                    )
                else:
                    days = {int(hit.get("开始") or 0) // 1440 for hit in hits}
                    ok = len(days) == 1 if spec["mode"] == "same_day" else len(days) == len(tasks)
                    checks.append(
                        _item(
                            f"{where}：{'、'.join(tasks)} {spec['mode']}",
                            PASS if ok else FAIL,
                            "实际所在日：" + "、".join(map(str, sorted(days))),
                        )
                    )
            elif ctype in ("at_most_one", "at_least_one"):
                tasks = [resolve_task(data, ref) for ref in spec["tasks"]]
                present = [task for task in tasks if scheduled(task) is not None]
                ok = (
                    len(present) <= 1
                    if ctype == "at_most_one"
                    else len(present) >= 1
                )
                checks.append(
                    _item(
                        f"{where}：{'、'.join(tasks)} "
                        + ("最多排一个" if ctype == "at_most_one" else "至少排一个"),
                        PASS if ok else FAIL,
                        f"实际排入 {len(present)} 个：{'、'.join(present) or '无'}",
                    )
                )
            elif ctype == "capacity":
                day = resolve_day(data, spec["time"])
                if day is None:
                    checks.append(
                        _item(f"{where}：{spec['resource']} 日配额", UNVERIFIED, "时间无法解析成某一天")
                    )
                    continue
                field = {"line": "作业线", "turbine": "风机编号", "part": "部件名称"}.get(
                    spec["resource"]
                )
                if field is None:
                    checks.append(
                        _item(f"{where}：{spec['resource']} 日配额", UNVERIFIED, "未知资源维度")
                    )
                    continue
                start_of_day, end_of_day = day * 1440, (day + 1) * 1440
                used: set = set()
                for row in rows:
                    segments = row.get("作业段")
                    if segments:
                        works = any(
                            a < end_of_day and b > start_of_day for a, b in segments
                        )
                    else:
                        # 产物没带作业段时退回整体跨度。口径更宽（跨天任务会被算进
                        # 两天），可能多算一个资源，但不会漏算——漏算才是假通过。
                        works = int(row.get("开始") or 0) < end_of_day and int(
                            row.get("完成") or 0
                        ) > start_of_day
                    if works:
                        used.add(row.get(field))
                checks.append(
                    _item(
                        f"{where}：第 {day} 天最多 {spec['limit']} 个 {spec['resource']}",
                        PASS if len(used) <= int(spec["limit"]) else FAIL,
                        f"实际用了 {len(used)} 个：{'、'.join(map(str, sorted(used, key=str))) or '无'}",
                    )
                )
            elif ctype == "only":
                # 「只修 X」的核对是**产物里不能有任何一行落在范围外**，
                # 不是"范围内至少排了一个"。少排是求解器的选择，多排才是违约。
                filter_spec = dict(spec.get("filter") or {})
                if "part" in filter_spec and data is not None:
                    # DSL 里 part 可以写编号（部件01）也可以写名称（叶片），
                    # 产物行只有名称，先归一再比，否则写编号会被误判成全部越界。
                    from .registry.entities import resolve_part

                    part_id = resolve_part(data, str(filter_spec["part"]))
                    if part_id is not None:
                        filter_spec["part"] = data.parts[part_id]["部件名称"]
                selected = _matching(rows, filter_spec)
                if selected is None:
                    checks.append(_item(f"{where}：只修范围", UNVERIFIED, "过滤维度无法核对"))
                    continue
                outside = [
                    str(row.get("任务编号")) for row in rows if row not in selected
                ]
                checks.append(
                    _item(
                        f"{where}：只修 {spec.get('filter')}",
                        PASS if not outside else FAIL,
                        f"排入 {len(rows)} 项，范围外 {len(outside)} 项："
                        + ("、".join(outside[:5]) or "无"),
                    )
                )
            elif ctype == "cardinality":
                filter_spec = spec.get("filter") or {}
                selected = _matching(rows, filter_spec)
                if selected is None:
                    checks.append(_item(f"{where}：完成数", UNVERIFIED, "过滤维度无法核对"))
                    continue
                ok = _op_ok(len(selected), str(spec["op"]), spec["value"])
                checks.append(
                    _item(
                        f"{where}：{filter_spec or '全部'} 完成数 {spec['op']} {spec['value']}",
                        UNVERIFIED if ok is None else (PASS if ok else FAIL),
                        f"实际 {len(selected)} 项",
                    )
                )
            elif ctype == "budget":
                field = _COST_FIELDS.get(str(spec["quantity"]))
                if field is not None and rec.get(field) is not None:
                    actual = rec.get(field)
                else:
                    # 不是那两个口径就走重算（人工/备件/其他费用这类小计）。
                    # 重算不出来就是未核对，不拿别的口径顶替——那是最容易
                    # "看起来核过了其实核的是另一个数"的地方。
                    actual = _ref_value(spec["quantity"], rows, rec)
                if actual is None:
                    checks.append(_item(f"{where}：{spec['quantity']} 限额", UNVERIFIED, "报告里没有这项成本口径"))
                    continue
                ok = _op_ok(actual, str(spec["op"]), spec["value"])
                checks.append(
                    _item(
                        f"{where}：{spec['quantity']} {spec['op']} {spec['value']}",
                        UNVERIFIED if ok is None else (PASS if ok else FAIL),
                        f"实际 {actual}",
                    )
                )
            elif ctype == "linear":
                total = 0.0
                unresolved = False
                for coef, ref in spec["terms"]:
                    value = _ref_value(ref, rows, rec)
                    if value is None:
                        unresolved = True
                        break
                    total += float(coef) * value
                if unresolved:
                    checks.append(_item(f"{where}：线性约束", UNVERIFIED, "有量引用无法从产物重算"))
                    continue
                ok = _op_ok(total, str(spec["op"]), spec["value"])
                checks.append(
                    _item(
                        f"{where}：{spec['op']} {spec['value']}",
                        UNVERIFIED if ok is None else (PASS if ok else FAIL),
                        f"实际 {round(total, 6)}",
                    )
                )
            else:
                checks.append(_item(where, UNVERIFIED, "这个原语没有对应的对账实现"))
        except Exception as exc:  # 对账本身出错，不能当成通过
            checks.append(_item(where, UNVERIFIED, f"对账失败：{exc}"))
    return checks


def _objective_checks(
    dsl: dict[str, Any],
    report: dict[str, Any],
    context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    ctx = context or {}
    info = report.get("求解") or {}
    plan = list(info.get("阶段计划") or [])
    objectives = dsl.get("objectives") or []
    declared = 0
    for spec in objectives:
        otype = str(spec.get("type") or "")
        if otype in ("minimize", "maximize"):
            declared += 1
        elif otype == "lexmin":
            declared += len(spec.get("stages") or [])
        elif otype == "strategy":
            name = str(spec.get("name") or "")
            actual = ctx.get("策略", report.get("策略"))
            if actual is None:
                checks.append(_item(f"目标策略 {name}", UNVERIFIED, "报告里没有策略字段"))
            else:
                checks.append(
                    _item(
                        f"目标策略 {name}",
                        PASS if str(actual) == name else FAIL,
                        f"实际策略 {actual}",
                    )
                )
        elif otype == "weighted":
            weights = spec.get("weights") or {}
            actual = ctx.get("自定义权重", report.get("自定义权重"))
            if actual is None:
                checks.append(_item("加权目标", UNVERIFIED, "报告里没有权重字段"))
            else:
                same = set(map(str, actual)) == set(map(str, weights)) and all(
                    _same(actual[key], value) for key, value in weights.items()
                )
                checks.append(
                    _item(
                        "加权目标 " + "、".join(f"{k}={v}" for k, v in weights.items()),
                        PASS if same else FAIL,
                        f"实际权重 {actual}",
                    )
                )
    if declared:
        executed = [
            item for item in plan if str(item.get("目标") or "").startswith("dsl_")
        ]
        skipped = [item for item in executed if item.get("结果") != "执行"]
        ok = len(executed) == declared and not skipped
        checks.append(
            _item(
                f"分层目标：声明 {declared} 段",
                PASS if ok else FAIL,
                f"阶段计划里有 {len(executed)} 段，其中跳过 {len(skipped)} 段",
            )
        )
    return checks


def reconcile(
    dsl: dict[str, Any] | None,
    report: dict[str, Any],
    *,
    data: Any = None,
    overrides: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逐条核对 DSL 与最终产物。

    data：任务表/时间基准；缺它时按过滤维度展开的诉求记为"未核对"，不算通过。
    overrides：编译后的参数覆盖（内部键）。缺它时只核对 op="=" 的绝对值。
    context：策略/模式/权重（runner 的报告没有这几个字段时补上）。
    """
    checks: list[dict[str, str]] = []
    info = report.get("求解") or {}
    source = str(info.get("方案来源") or "")
    if source == "HEURISTIC_FALLBACK":
        checks.append(
            _item(
                "整份方案按本次约束求解",
                FAIL,
                "方案来源是 HEURISTIC_FALLBACK：这是启发式基线，没有按本次约束求解",
            )
        )
    if not dsl:
        return _summary(checks)
    checks.extend(_parameter_checks(dsl, report, data, overrides))
    checks.extend(_constraint_checks(dsl, report, data))
    checks.extend(_objective_checks(dsl, report, context))
    return _summary(checks)


def _summary(checks: list[dict[str, str]]) -> dict[str, Any]:
    violations = [item for item in checks if item["结论"] == FAIL]
    unverified = [item for item in checks if item["结论"] == UNVERIFIED]
    return {
        "通过": not violations and not unverified,
        "检查项": len(checks),
        "违例": violations,
        "未核对": unverified,
        "明细": checks,
    }


def describe(audit: dict[str, Any], limit: int = 5) -> str:
    """给用户看的一句话：哪几条没对上。"""
    violations = audit.get("违例") or []
    unverified = audit.get("未核对") or []
    if not violations and not unverified:
        return f"意图对账通过（{audit.get('检查项', 0)} 项）"
    parts = []
    for item in violations[:limit]:
        parts.append(f"{item['诉求']}：{item['证据']}")
    if len(violations) > limit:
        parts.append(f"另有 {len(violations) - limit} 条违例")
    if unverified:
        parts.append(f"{len(unverified)} 条未能核对")
    return "；".join(parts)
