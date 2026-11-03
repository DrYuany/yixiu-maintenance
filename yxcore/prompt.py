"""提示词：从注册表反向生成，改注册表就自动跟着变。

为什么必须反向生成：大一修吃过亏——提示词按 51 项对外宣称、执行侧只认 16 项，
模型照着提示词拧了一个没有落点的旋钮，校验器才发现接不住。提示词与注册表两份
事实，必然漂移。这里提示词是从 ``slots.py`` 现场生成的，不可能不一致。

两类索引分开写，这是设计里最要紧的一处区别：

* **离散索引**（部件/风机/严重度/任务/窗口/作业线）：列出**当前快照里的真实取值**，
  并且附上「这一格里现在有几个任务」。模型写「窗口03 必须修 3 个叶片」时看得见
  只有 2 个，当场就知道过分了——**数字本身就是护栏**。
* **开放索引**（任务清单、日期区间）：预先不知道填什么，只能给格式模板。
  它们享受不到护栏，只能事后查，所以模板要给死，别让模型自由发挥。
"""
from __future__ import annotations

from typing import Any

from . import slots as S

# 做不到的清单（唯一来源）。README 里那份是人读的副本，改这里。
BOUNDARY: tuple[tuple[str, str], ...] = (
    ("格子之间比一比", "摊匀一点、别让某条船累死、出海别一天挤一天松、几台风机分得差不多"),
    ("条件约束", "如果 018 排进去，019 必须在 4 小时内开工"),
    ("组内自动两两", "风机001 的所有任务两两间隔 2 小时"),
    ("跨量加权组合", "中严重完工时间和 + 2 倍低严重完工时间和 ≤ 30 天"),
    ("过滤后取极值/比值", "高危里最晚完工的那个不晚于 X"),
    ("未登记的目标轴", "第 15 个权衡对象（只能走加族评审）、按紧迫程度排顺序"),
    ("安全库存下限", "每种部件至少留 2 套（缺业务依据，不做）"),
)

ROLE = """你是海上风电检修排程的「旋钮填写员」。

排程模型已经写好、约束的形状全部冻结。你的工作只有一件事：**往已注册的旋钮里填数**。

三条铁律：
1. 不许发明新约束。只能从下面「能拧的旋钮」清单里挑，挑不出来就直说没有这个旋钮。
2. 索引值只能照抄下面给的取值，不许自己造、不许模糊匹配（"叶片"不能当成"叶片备件"）。
3. 强度只报 hard / soft / unsure 三选一，**不要给置信度数字**。
   拿不准就报 unsure，别猜。
4. 「全都要修」不是写 999——值就写上面清单里那一组的**任务数**（清单里已经标了「几个」）。
   999 这种假数字会被校验层当成「超范围」拦下，明明听得懂的话也办不成。"""


def _discrete_catalog(data, options: dict[str, list[dict]] | None) -> str:
    """离散索引的当前取值 + 护栏数字。"""
    lines: list[str] = []
    tasks = data.tasks

    def able(pred) -> int:
        if not options:
            return sum(1 for t in tasks if pred(t))
        return sum(1 for tid, rows in options.items() if any(pred(c) for c in rows))

    parts = {}
    for pid, part in data.parts.items():
        count = able(lambda c, p=pid: c["部件编号"] == p)
        if count:
            parts[part["部件名称"]] = count
    lines.append("部件（当前在册任务数）：" + "、".join(f"{k} {v} 个" for k, v in parts.items()))

    sev = {level: sum(1 for t in tasks if t["严重等级"] == level) for level in ("高", "中", "低")}
    lines.append("严重度：" + "、".join(f"{k} {v} 个" for k, v in sev.items() if v))

    # 风机也要带任务数：「风机001 的活全修」这句话的值就是这一台的数。
    # 只列编号不列个数，模型只能瞎猜（实测会写 999 这种假数字，然后被校验层拦下）。
    turbines: dict[str, int] = {}
    for t in tasks:
        turbines[t["风机编号"]] = turbines.get(t["风机编号"], 0) + 1
    lines.append("风机（当前在册任务数）："
                 + "、".join(f"{k} {v} 个" for k, v in sorted(turbines.items())))

    lines.append("任务：" + "、".join(
        f"{t['任务编号']}({data.parts[t['部件编号']]['部件名称']}/{t['严重等级']})" for t in tasks))

    for window in data.windows:
        wid = window["编号"]
        lo = data.stamp(window["开始"])[:10]
        hi = data.stamp(window["结束"])[:10]
        rows = [c for tid, cs in (options or {}).items() for c in cs if c["窗口编号"] == wid]
        by_part: dict[str, int] = {}
        for c in rows:
            by_part[c["部件名称"]] = by_part.get(c["部件名称"], 0) + 1
        detail = "、".join(f"{k} {v}" for k, v in sorted(by_part.items())) if by_part else "无候选"
        lines.append(f"窗口 {wid}（{lo} ~ {hi}）：可开工候选 {len(rows)} 个；按部件 {detail}")

    days = range(data.now // 1440, data.end // 1440 + 1)
    shown = []
    for day in days:
        rows = [c for cs in (options or {}).values() for c in cs if c["开始"] // 1440 == day]
        if rows:
            shown.append(f"{data.stamp(day * 1440)[:10]}（{len(rows)} 个候选）")
    if shown:
        lines.append("可开工日期：" + "、".join(shown[:40]))

    lines.append("作业线：1 ~ " + str(min(data.params["最多作业线数"], data.params["人员总上限"])) + " 号")
    return "\n".join(lines)


def _open_templates(data) -> str:
    """开放索引的格式模板——享受不到护栏，所以格式要给死。

    示例日期取快照里真实可开工的一天：给个规划期外的日期当样板，模型会照抄出
    一个不存在的日子，那就白给了一次事后报错。
    """
    sample_day = data.stamp(max(data.now // 1440, 0) * 1440)[:10]
    tasks = [t["任务编号"] for t in data.tasks[:2]]
    return "\n".join([
        "任务清单（点名几个任务，任意组合）：",
        '    {"kind": "任务清单", "value": ["%s", "%s"]}' % tuple(tasks),
        "日期区间（任意起止，格式 YYYY-MM-DD，必须是上面「可开工日期」里有的日子）：",
        f'    {{"kind": "日期区间", "value": ["{sample_day}", "{sample_day}"]}}',
        "时刻（ISO 带时区）：",
        f'    "{sample_day}T00:00:00+08:00"',
    ])


def _slot_lines() -> str:
    """旋钮清单，从注册表反向生成。"""
    lines: list[str] = []
    order = ["C", "D", "E", "F", "G", "H", "I", "B", "A", "J"]
    titles = {"C": "修多少", "D": "何时修", "E": "时间界", "F": "用哪条船", "G": "花多少",
              "H": "先后同异", "I": "只修哪些", "B": "目标", "A": "参数与开关", "J": "改数据"}
    for group in order:
        lines.append(f"【{group} 组 · {titles[group]}】")
        for slot in S.SLOTS.values():
            if slot.group != group:
                continue
            idx = "，".join(
                f"{name}({'可选·' if name in slot.optional else ''}{kind})"
                for name, kind in slot.index) or "无索引"
            lines.append(f"* {slot.name}｜{slot.label}｜索引：{idx}｜默认：{slot.default_note}")
            lines.append(f"    例子：{'；'.join(slot.examples)}")
            if slot.note:
                lines.append(f"    注意：{slot.note}")
        if group == "A":
            for alias, spec in S.PARAM_SLOTS.items():
                kind = "是/否" if spec["类型"] == "bool" else ("时刻 HH:MM" if spec["单位"] == "HH:MM"
                                                             else f"数字（{spec['单位']}）")
                lines.append(f"* {alias}｜参数｜{kind}｜默认：基座值")
                if spec.get("说明"):
                    lines.append(f"    管什么：{spec['说明']}")
        if group == "B":
            # 枚举值必须印出来：不印模型就只能照话术自己编名字
            lines.append("  strategy 只能填这四个之一：" + "、".join(S.STRATEGY_NAMES))
            lines.append("  weight 的「量」只能填这 14 个之一：" + "、".join(S.METRIC_NAMES))
        lines.append("")
    return "\n".join(lines)


def _disambiguation() -> str:
    """易混旋钮的分界。这些是宪法层的边界（R1/R4/策略-权重），不写进提示词模型就只能猜。"""
    return "\n".join([
        "── 易混旋钮的分界（挑错旋钮=错的约束，按这几条判） ──",
        "1. 整轮 vs 某段时光：",
        "   * 「最多开两条船」「这轮最多修 8 个」——没有点名日期/窗口 → 整轮口径（参数「最多作业线数」/ count_max）",
        "   * 「11月16号最多开两条船」「这周别动风机003」——点名了时段 → cap_max / bucket_max",
        "2. 「只许」vs「不许」：",
        "   * 「只修叶片」「只在窗口03 干」——只留它、其余都不要 → scope_only",
        "   * 「窗口01 不修齿轮箱」「16号别排 2 号船」——只禁它、其余随意 → bucket_max（值 0）",
        "3. strategy vs weight：",
        "   * 这四句是内置策略的**典型说法**，命中就落 strategy，不要拆成 weight："
        "「按成本最低排」→成本最低优先；「整体按高风险抢修」→高风险优先抢修；"
        "「越早干完越好」→最短完成优先；「尽量多修几个」→检修最多器件优先",
        "   * strategy 是**整体**排程风格四选一，用户说「按成本最低排」「越早干完越好」这类才动它",
        "   * 「高危优先」「省钱更要紧」「尽量压租船天数」只点了**一个偏好量** → weight（软），不要动 strategy",
        "4. 点名实例 vs 数量上限（最容易落错的一对）：",
        "   * cap_max 的「维度」只能填这三类**名字**：作业线 / 风机 / 部件，不是编号。"
        "cap_max[维度=作业线, 桶=某日]=2 的意思是「这天最多占用 2 条船」，跟「2 号船」无关",
        "   * 点名了哪一条船/哪一台风机 → bucket_max："
        "「11月17号 2 号船别排」= bucket_max（桶=11月17号，组=作业线 2，值 0）",
        "   * 「2 号船别排」「风机003 别动」——点名了**具体哪条船/哪台机** → bucket_max / count_max（组=那个实例）",
        "   * cap_max 只管**数量**（最多几条船/几台风机/几个部件同时占用），永远不带具体编号",
    ])


def _boundary_text() -> str:
    lines = ["下面这些话**做不到**。不许拿一个近似的旋钮糊过去——"
             "硬凑出来的东西对不上用户的话，比明确拒绝更危险："]
    for name, example in BOUNDARY:
        lines.append(f"* {name}：{example}")
    lines.append("拿权重去近似均衡也算糊弄，同样不许做。")
    return "\n".join(lines)


def _turn_rules() -> str:
    """多轮与复合需求的口径。不写这几条，模型会把「改主意」做成「加一条」。"""
    return "\n".join([
        "── 多轮与一句话多个要求（照这几条落） ──",
        "1. 一句话里有几个要求就出几条补丁，各自独立；**不要**为了「合并」把两个不同格子"
        "拼成一条，也不要把一条拆成两条写同一格。",
        "2. 用户改主意（「改成…」「提高到…」「别超…了，改成…」）：输出**同一个 slot + 完全"
        "相同的 index**，op 用 =（或 + - *）。索引照抄下面「当前已生效」里那一条，"
        "一个字都别改——改一个字就是另一个格子，旧约束还在，等于加了条新的。",
        "3. 用户要去掉某条（「刚才那条不要了」「取消现金上限」「不用管顺序了」）："
        "op 用 reset，索引照抄那一条。要说「全都恢复默认」就为每一条各出一条 reset。",
        "3b. 「改成」但换的是另一格（「现金不超80万」→「改成人工费不超50万」，口径变了；"
        "「只修叶片」→「改成只修齿轮箱」，范围变了）：**先 reset 旧的那条，再给新的**。"
        "旧的那条不撤，两条会一起生效，等于加了条更严的。",
        "4. 相对量（涨两成/再加 5 万/降一点）只给 op 和数值，**不要**自己把上一轮的值算进去——"
        "执行器会以当前生效值为基数折算。",
        "5. 「只修」类（scope_only）有两种意图，看话里有没有「改成/换成」：",
        "   * 换成另一个范围（「改成只修齿轮箱」）→ 先把上一轮那条 reset，再给新的"
        "（两条scope_only 是**取交集**，只加新的会变成「只修叶片且只修齿轮箱」= 一个都修不成）",
        "   * 追加一层限制（「而且只在窗口03」）→ 直接再加一条，不用撤",
        "6. 用户指着上一轮说过的话（「刚才那个」「上一条」「那个上限」）却指不清是哪一格时："
        "strength 报 unsure，不要猜一个格子。",
    ])


def _output_spec() -> str:
    return "\n".join([
        "输出只要一个 JSON 对象，不要解释、不要 markdown 代码块：",
        '{"patches": [',
        '  {"slot": "bucket_min",',
        '   "index": {"桶": {"kind": "窗口", "value": "窗口03"},',
        '             "组": {"kind": "部件", "value": "叶片"}},',
        '   "op": "=", "value": 1, "strength": "hard", "utterance": "用户原话"}',
        "]}",
        "",
        "说明：",
        "* **间隔的单位是小时**（半天=12、一天=24），没说间隔就填 0。",
        "* **时刻类旋钮**（earliest / deadline_delta / part_arrival / window）只能用 = 给具体日期时间，"
        "不许用 + - *（「推迟两天」自己算成 11月17号，否则会被当成加 2 分钟）。",
        "* 索引**必须**写成 {\"kind\": \"…\", \"value\": …} 这样的对象，"
        "只给一个裸字符串（如 \"桶\": \"窗口03\"）不行——kind 说明这一格是哪一类，猜不得。",
        "* op 只能是 = / * / + / - / reset；相对运算（*+−）按基线值折算。",
        "* 一句话可能对应多个补丁，就都列出来；对应不上任何旋钮就回 {\"patches\": [], \"拒绝理由\": \"...\"}。",
        "* utterance 抄用户原话，一个字都别改——回显和对账要用。",
        "* 同一句话里既有硬性要求又有软性偏好，就分成两个补丁，各自标 strength。",
    ])


def build_prompt(data, options: dict[str, list[dict]] | None = None, *, extra: str = "",
                 session_text: str = "") -> str:
    """生成完整提示词。data 用来给离散索引附上当前数字护栏。

    ``session_text`` 是多轮时「当前已生效的旋钮」清单（由 ``Session.prompt_state``
    生成，补丁原格式）。空串表示这是第一轮——第一轮不该出现这一段，
    否则模型会以为已经有约束在生效。
    """
    sections = [
        ROLE,
        "",
        "── 能拧的旋钮（形状冻结，只填数） ──",
        _slot_lines(),
        "── 索引的当前取值（离散的，照抄，别造） ──",
        _discrete_catalog(data, options),
        "",
        "── 开放索引的填法（这几类只能照格式填） ──",
        _open_templates(data),
        "",
        _disambiguation(),
        "",
    ]
    if session_text:
        # 放在多轮规则**之前**：规则里说「照抄下面那一条」，得先有那一条可抄
        sections.extend(["── 当前已生效的旋钮（多轮累积） ──", session_text, ""])
    sections.extend([
        _turn_rules(),
        "",
        "── 边界 ──",
        _boundary_text(),
        "",
        _output_spec(),
    ])
    if extra:
        sections.extend(["", "── 本次补充 ──", extra])
    return "\n".join(sections)


def prompt_stats(data, options: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """提示词的规模统计——给第 1 期回归用：行数、旋钮数、离散/开放索引各多少。"""
    text = build_prompt(data, options)
    return {
        "字符数": len(text),
        "行数": text.count("\n") + 1,
        "旋钮数": len(S.SLOTS) + len(S.PARAM_SLOTS),
        "边界条数": len(BOUNDARY),
    }
