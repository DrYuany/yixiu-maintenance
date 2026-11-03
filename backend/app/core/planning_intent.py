from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from dsl_core import objective_catalog
from dsl_core.part_catalog import (
    _PART_ALIASES,
    _PART_ALIAS_KEYS,
    _PART_ALIAS_TO_ID,
    _SEVERITY_ALIASES,
    part_catalog_text,
)
from dsl_core.planning_request import PlanningRequest

_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class CompiledIntent:
    """编译结果，连同要告诉用户的口径说明和阻塞问题。"""

    request: PlanningRequest
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # 这句话里"确实读懂并取走"的部分。注意与 request 的差异不同：
    # 用户说"按成本最低优先排"，而默认策略恰好就是成本最低优先——
    # 字段没变化，但这句话读懂了。没有这个信号，就会把已识别的句子
    # 误判成"什么都没取到"而丢给大模型。
    consumed: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


class PlanningIntentCompiler:
    """Deterministic compiler for the product's registered planning language."""

    _STRATEGIES = (
        (("高风险", "风险优先", "严重性优先"), "高风险优先抢修"),
        (("成本优先", "经济性优先", "成本最低"), "成本最低优先"),
        (("最短完成", "尽快完成", "工期优先"), "最短完成优先"),
        (("最多器件", "检修最多", "完成数量优先"), "检修最多器件优先"),
    )

    # 只保留目标模板里真实存在的 7 个可加权指标。
    # 现金支出、各级完工时间和不属于任何策略的加权项，不能在这里对用户许诺。
    _WEIGHT_ALIASES = {
        "高严重完成数": ("高严重完成数", "高风险完成数", "高严重度完成数", "严重度完成数"),
        "中严重完成数": ("中严重完成数", "中风险完成数", "中严重度完成数"),
        "低严重完成数": ("低严重完成数", "低风险完成数", "低严重度完成数"),
        "总经济成本": ("总经济成本", "总成本", "经济成本"),
        "完成任务数": ("完成任务数", "完成数量"),
        "最后完工时间": ("最后完工时间", "完工时间"),
        "完工时间和": ("完工时间和",),
    }

    _WEIGHT_MODES = (
        (("改成加权", "使用加权", "加权模式", "加权目标", "设为加权"), "加权"),
        (("改成分层", "使用分层", "分层模式", "分层目标", "设为分层"), "分层"),
    )

    _MODE_TOKENS = (
        "改成加权", "使用加权", "加权模式", "加权目标", "设为加权",
        "改成分层", "使用分层", "分层模式", "分层目标", "设为分层",
    )

    def compile(
        self,
        message: str,
        current: PlanningRequest | None = None,
    ) -> PlanningRequest:
        return self.compile_detailed(message, current).request

    def compile_detailed(
        self,
        message: str,
        current: PlanningRequest | None = None,
    ) -> CompiledIntent:
        text = self._normalize(message)
        # 「叶片在高风险需要修 15 小时」里的"高风险"是工时档位，不是"按风险优先排"。
        # 策略匹配是全文本包含判断，不遮掉这段就会把一句改工时的话读成换策略——
        # 用户没说过的话，不能自己补进请求里。
        text_for_strategy = self._HOURS_LEVEL_PHRASE.sub(
            lambda match: "○" * len(match.group(0)), text
        )
        request = replace(current) if current else PlanningRequest()
        notes: list[str] = []
        errors: list[str] = []
        consumed: list[str] = []

        original_strategy = request.strategy
        for phrases, strategy in self._STRATEGIES:
            if any(phrase in text_for_strategy for phrase in phrases):
                request = replace(request, strategy=strategy)
                consumed.append(f"策略={strategy}")
                break
        strategy_changed = request.strategy != original_strategy

        overrides = dict(request.overrides)
        relative_overrides = dict(request.relative_overrides)
        limits = dict(request.limits)

        ship_rent = re.search(r"船租(?:上涨|上调|增加)\s*(\d+(?:\.\d+)?)\s*%", text)
        if ship_rent:
            relative_overrides["船租（元每艘每天）"] = 1 + float(ship_rent.group(1)) / 100
        ship_rent_absolute = re.search(
            r"船租(?:改成|改为|设为|调整为|定为)\s*(\d+(?:\.\d+)?)\s*(万|元)?",
            text,
        )
        if ship_rent_absolute:
            value = float(ship_rent_absolute.group(1))
            if ship_rent_absolute.group(2) == "万":
                value *= 10_000
            overrides["船租（元每艘每天）"] = value

        work_lines = re.search(
            r"(?:最多|不超过|限定|只能)?\s*(?<![同这那])(\d+|一|两|二|三|四)\s*条(?:作业)?线", text
        )
        if work_lines:
            number = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4}.get(work_lines.group(1))
            overrides["最多作业线数"] = number or int(work_lines.group(1))

        daily_hours = re.search(
            r"每天[^\d]{0,6}(?:最多|不超过|只能|限定|只有|至多|只|仅)"
            r"(?:维修|作业|施工|检修|干活|干|做|工作|时间|修)?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:个)?小时",
            text,
        )
        if not daily_hours:
            daily_hours = re.search(
                r"每天(?:作业|工作|维修|施工)(?:时间)?(?:改成|改为|设为|调整为|控制在|只能|限定)?\s*"
                r"(\d+(?:\.\d+)?)\s*(?:个)?小时",
                text,
            )
        if daily_hours:
            value = float(daily_hours.group(1))
            overrides["每天最多维修小时数"] = int(value) if value.is_integer() else value

        # ——人工单价（元每人时），默认 150——
        labor_rate = re.search(
            r"人工(?:单价|成本|费用|工时费)?\s*(?:改成|改为|设为|调整为|定为|调成|涨到|降到)?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)?(?:\s*(?:每|/)\s*(?:人)?时)?",
            text,
        )
        if labor_rate:
            overrides["人工单价（元每人时）"] = float(labor_rate.group(1))
        labor_rate_up = re.search(
            r"人工(?:单价|成本|费用|工时费)?\s*(?:上涨|上调|增加|提高|下降|降低)\s*(\d+(?:\.\d+)?)\s*%",
            text,
        )
        if labor_rate_up:
            factor = 1 + float(labor_rate_up.group(1)) / 100
            if re.search(r"下降|降低", text):
                factor = 1 - float(labor_rate_up.group(1)) / 100
            relative_overrides["人工单价（元每人时）"] = factor

        # ——人员总上限，默认 30——
        headcount = re.search(
            r"(?:人员|人手|工人|施工|作业)?(?:总)?(?:人数|人员|人力|上限)\s*"
            r"(?:改成|改为|设为|调整为|定为|控制在|不超过|最多|只有|限定)?\s*(\d+)\s*(?:人|个)?",
            text,
        )
        if not headcount:
            # 「最多 30 人」这种不带人员名词的说法
            headcount = re.search(r"(?:最多|不超过|限定|只有)\s*(\d+)\s*人", text)
        if headcount:
            overrides["人员总上限"] = int(headcount.group(1))

        # ——风机间转移小时数，默认 6——
        transfer = re.search(
            r"(?:风机间|风机之间|机位间|两台风机间?)?\s*转移(?:时间|小时数|耗时|用时)?\s*"
            r"(?:改成|改为|设为|调整为|定为|需要|要|用)?\s*(\d+(?:\.\d+)?)\s*(?:个)?小时",
            text,
        )
        if transfer:
            value = float(transfer.group(1))
            overrides["风机间转移小时数"] = int(value) if value.is_integer() else value

        # ——每天维修开始 / 结束时间，默认 08:00 / 18:00——
        # 时段用「点」或冒号表达，时长用「小时」表达，两者不会互相误吞。
        for key, starts, ends in (
            ("每天维修开始时间", ("开工", "开始", "上班", "出海", "出门"), ()),
            ("每天维修结束时间", (), ("收工", "结束", "下班", "停止", "回港")),
        ):
            clock_value = self._parse_clock(text, starts=starts, ends=ends)
            if clock_value:
                overrides[key] = clock_value

        # 口径按参数总表：说"总成本"才卡含发电损失的总经济成本；
        # 只说"预算"按"费用预算上限"的默认口径卡现金支出（发电损失是机会成本，不是花出去的钱）。
        budget = re.search(r"总成本(?:最多|不超过|控制在|上限)\s*(\d+(?:\.\d+)?)\s*万", text)
        if budget:
            limits["最大总成本"] = float(budget.group(1)) * 10_000

        # "钱"和"预算"同口径：都是花出去的钱（现金支出），不是含发电损失的机会成本。
        fee_budget = re.search(
            r"(?:预算|钱|花费|开销)(?:最多|不超过|不能超过|别超过|控制在|上限|封顶)?\s*"
            r"(\d+(?:\.\d+)?)\s*万",
            text,
        )
        if fee_budget and "最大总成本" not in limits:
            limits["现金预算"] = float(fee_budget.group(1)) * 10_000

        cash = re.search(r"现金支出(?:最多|不超过|控制在|上限)?\s*(\d+(?:\.\d+)?)\s*万", text)
        if not cash:
            cash = re.search(r"现金(?:预算|支出)(?:最多|不超过|控制在|上限)\s*(\d+(?:\.\d+)?)\s*万", text)
        if cash:
            limits["现金预算"] = float(cash.group(1)) * 10_000

        required = re.findall(r"(?:T|任务)\s*(\d{1,3})(?:必须|一定要|务必)(?:修|安排|完成|做)", text)
        if required:
            limits["必修任务"] = sorted({f"任务{int(item):03d}" for item in required})

        parsed_patches, patch_notes, patch_errors = self._parse_data_patches(text)
        notes.extend(patch_notes)
        errors.extend(patch_errors)
        # 补丁要跨轮次累加，否则第二句会悄悄丢掉第一句的改动。
        patches = self._merge_patches(request.data_patches, parsed_patches)

        mode_set_here = False
        for tokens, mode in self._WEIGHT_MODES:
            if any(token in text for token in tokens):
                request = replace(request, mode=mode)
                mode_set_here = True
                break

        explicit_weights = self._parse_weights(text, weighted_mode=False)
        if explicit_weights and not mode_set_here and request.mode != objective_catalog.WEIGHTED_MODE:
            # 用户直接给了“某某权重 0.8”，不用再让他补一句“改成加权”。
            request = replace(request, mode=objective_catalog.WEIGHTED_MODE)
            mode_set_here = True
        if (
            explicit_weights
            and request.mode == objective_catalog.LAYERED_MODE
            and mode_set_here
        ):
            errors.append("这条同时要求了分层和权重；分层模式不接收权重，请二选一。")

        mentioned = self._parse_weights(
            text, weighted_mode=request.mode == objective_catalog.WEIGHTED_MODE
        )
        request, weight_notes, weight_errors = self._resolve_weights(
            request,
            mentioned=mentioned,
            previous=current,
            strategy_changed=strategy_changed,
        )
        notes.extend(weight_notes)
        errors.extend(weight_errors)

        # 只有用户这句话里真的点了时限才写进请求；没点就留 None，
        # 由参数总表（求解01「总求解预算」）决定——公告不能再自己编一个数。
        seconds = request.seconds
        solve_minutes = re.search(r"(?:求解)?(?:最多|最长|上限)?\s*(\d+(?:\.\d+)?)\s*分钟", text)
        solve_seconds = re.search(r"(?:求解)?(?:最多|最长|上限)?\s*(\d+)\s*秒", text)
        if solve_minutes:
            seconds = int(float(solve_minutes.group(1)) * 60)
        elif solve_seconds:
            seconds = int(solve_seconds.group(1))
        if seconds is not None:
            seconds = min(1800, max(30, seconds))

        # 多轮上下文：同一参数本轮新说的口径取代旧口径（绝对 vs 相对不叠加）。
        prev_overrides = dict(request.overrides)
        prev_relative = dict(request.relative_overrides)
        new_abs = {key for key, value in overrides.items() if prev_overrides.get(key) != value}
        new_rel = {key for key, value in relative_overrides.items() if prev_relative.get(key) != value}
        for key in new_abs:
            relative_overrides.pop(key, None)
        for key in new_rel:
            overrides.pop(key, None)

        return CompiledIntent(
            request=replace(
                request,
                seconds=seconds,
                overrides=overrides,
                relative_overrides=relative_overrides,
                limits=limits,
                data_patches=patches,
            ),
            notes=notes,
            errors=errors,
            consumed=consumed,
        )

    # ------------------------------------------------------------- 行级数据补丁
    _MONTH_DAY = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]")

    # 「高风险 / 中风险」紧跟着「修 N 小时」——这是工时档位，不是策略口径。
    # 只做遮蔽，不做抽取；抽取在 _parse_data_patches 里按部件名锚定。
    _HOURS_LEVEL_PHRASE = re.compile(
        r"(?:高风险|中风险|低风险|高严重度|中严重度|低严重度)"
        r"(?:下|时|的情况下|的)?"
        r"(?:需要|需|要|得|只|就)?"
        r"修\s*\d+(?:\.\d+)?\s*(?:个)?小时"
    )

    # 部件名 + （可选定语）+ 修 N 小时 = 行级工时定点。用于路由判定：
    # 必须锚定到部件名，"这个任务要修 15 小时"那种说法不进规则路径
    # （部件库没有任务级的工时刻，硬猜等于编造改动）。
    _HOURS_ABSOLUTE = re.compile(
        r"(?:" + "|".join(re.escape(alias) for alias in _PART_ALIAS_KEYS) + r")"
        r"[^，。；;]{0,12}?"
        r"(?:修|工时\s*(?:定|改|设|调整|调)(?:成|为|到))\s*\d+(?:\.\d+)?\s*(?:个)?小时"
    )

    # 部件名和「修 N 小时」之间允许出现的定语。出现别的东西就说明这不是在给这个
    # 部件定工时而是在说日常参数（「叶片每天最多修 8 小时」）。
    _HOURS_SCOPE_GLUE = re.compile(
        r"(?:在|的|下|时|的?情况|高|中|低|风险|严重度|等级|常态|恶劣|但安全|海况|"
        r"维修|工时|需要|需|要|得|只|就|也|都|这|那)+"
    )

    @staticmethod
    def _resolve_moment(year: str | None, month: str, day: str) -> str:
        """把「7月5日」变成带时区的绝对时间。

        用户说月日不说年，这里用一条确定性规则：取今年；若今年这天已过，顺延到明年。
        采用哪一年会写进预览，不让用户猜。
        """
        now = datetime.now(_TZ)
        try:
            moment = datetime(int(year or now.year), int(month), int(day), tzinfo=_TZ)
        except ValueError:
            return ""
        if moment < now.replace(hour=0, minute=0, second=0, microsecond=0):
            moment = moment.replace(year=moment.year + 1)
        return moment.isoformat()

    def declared_strategy(self, text: str) -> str | None:
        """这句话点名了哪个优化策略。用于判断用户是在完整描述一个配置。"""
        for phrases, strategy in self._STRATEGIES:
            if any(phrase in text for phrase in phrases):
                return strategy
        return None

    # 时刻写法：8点 / 8点半 / 8点30分 / 08:00 / 8:00
    _CLOCK = re.compile(
        r"(?<![\d:])(\d{1,2})\s*(?:[:：]\s*(\d{2})(?!\d)|点\s*(?:(半)|(\d{1,2})\s*分)?)"
    )
    _CLOCK_START_HINT = ("开工", "开始", "上班", "出海", "出门", "早上", "上午", "凌晨")
    _CLOCK_END_HINT = ("收工", "结束", "下班", "停止", "回港", "下午", "傍晚", "晚上", "夜里")

    @classmethod
    def _parse_clock(
        cls, text: str, *, starts: tuple[str, ...], ends: tuple[str, ...]
    ) -> str | None:
        """把「8点」「8点半」「08:00」翻成求解器认的 "HH:MM"。

        方向靠附近的方向词判断；判不出来就返回 None，不猜。
        """
        # 按标点切段，否则「8点开工，下午5点半收工」里前一句会读到后一句的“下午”
        for segment in re.split(r"[，,。;；、]", text):
            value = cls._parse_clock_in(segment, starts=starts, ends=ends)
            if value:
                return value
        return None

    @classmethod
    def _parse_clock_in(
        cls, text: str, *, starts: tuple[str, ...], ends: tuple[str, ...]
    ) -> str | None:
        for match in cls._CLOCK.finditer(text):
            hour = int(match.group(1))
            if match.group(2):
                minute = int(match.group(2))
            elif match.group(3):
                minute = 30
            elif match.group(4):
                minute = int(match.group(4))
            else:
                minute = 0
            around = text[max(0, match.start() - 8) : match.end() + 6]
            # 「下午 5 点半」是 17:30，不是 05:30
            if hour < 12 and any(
                word in around for word in ("下午", "傍晚", "晚上", "夜里", "晚")
            ):
                hour += 12
            if not 0 <= hour <= 23 or not 0 <= minute < 60:
                continue
            is_start = any(word in around for word in starts) or any(
                word in around for word in cls._CLOCK_START_HINT
            )
            is_end = any(word in around for word in ends) or any(
                word in around for word in cls._CLOCK_END_HINT
            )
            if is_start and not is_end:
                direction = "start"
            elif is_end and not is_start:
                direction = "end"
            else:
                continue  # 认不出是开工还是收工，不猜
            wanted = "start" if starts else "end"
            if direction == wanted:
                return f"{hour:02d}:{minute:02d}"
        return None

    @staticmethod
    def _match_part(text: str) -> tuple[str, str] | None:
        for alias in _PART_ALIAS_KEYS:
            if alias in text:
                return _PART_ALIAS_TO_ID[alias], alias
        return None

    @staticmethod
    def _match_severity(text: str) -> str | None:
        for aliases, level in _SEVERITY_ALIASES:
            if any(alias in text for alias in aliases):
                return level
        return None

    @staticmethod
    def _patch_identity(patch: dict[str, Any]) -> tuple:
        """同一件事只留一条：后说的取代先说的，避免重复叠加。

        part_hours 的身份是（部件, 严重度, 海况）这一格，不是整个部件：
        「整件 ×1.2」和「高风险定 15 小时」是两件不同的事，都该留着。
        """
        kind = patch.get("kind")
        if kind == "part_hours":
            return (kind, patch.get("partId"),
                    patch.get("severity") or "*", patch.get("sea") or "*")
        if kind == "part_arrival":
            return (kind, patch.get("partId"), patch.get("at"))
        if kind == "task_deadline":
            return (kind, patch.get("severity"), patch.get("scope"))
        return (kind,)

    @classmethod
    def _merge_patches(
        cls, existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged = {cls._patch_identity(patch): patch for patch in existing}
        for patch in incoming:
            merged[cls._patch_identity(patch)] = patch
        return sorted(
            merged.values(),
            key=lambda p: (
                p.get("kind", ""),
                p.get("partId", ""),
                p.get("at", ""),
                p.get("scope", ""),
            ),
        )

    def _parse_data_patches(
        self, text: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        patches: list[dict[str, Any]] = []
        notes: list[str] = []
        errors: list[str] = []
        seen: set[str] = set()

        # 到货：「叶片备件 7 月 5 日到货」
        for match in re.finditer(
            r"(.{0,6}?)" + self._MONTH_DAY.pattern + r"(?:之前|前)?(?:才|能)?到货(?:(\d+)\s*(?:套|个|件|台))?",
            text,
        ):
            part = self._match_part(match.group(1))
            if not part:
                continue
            moment = self._resolve_moment(match.group(2), match.group(3), match.group(4))
            if not moment:
                errors.append(f"「{match.group(3)}月{match.group(4)}日」不是一个有效日期。")
                continue
            quantity = int(match.group(5)) if match.group(5) else 1
            if quantity <= 0:
                errors.append(f"「{part[1]}」的到货数量必须是正整数。")
                continue
            key = f"arrival:{part[0]}:{moment}"
            if key in seen:
                continue
            seen.add(key)
            # 「到货，之前不能开工」是同一件事的两半：到货前这个部件不可用。
            block_before = bool(
                re.search(
                    r"(?:之前|前)(?:不能|不可|禁止|无法|不许|不得)(?:开工|修|用|施工|安排)",
                    text,
                )
            )
            patches.append(
                {
                    "kind": "part_arrival",
                    "partId": part[0],
                    "partName": part[1],
                    "at": moment,
                    "quantity": quantity,
                    "blockBefore": block_before,
                }
            )
            if block_before:
                notes.append(
                    f"按你说的“到货前不能开工”，{moment[:10]} 之前「{part[1]}」的可用量按 0 计算。"
                )
            if not match.group(5):
                notes.append(
                    f"「{part[1]}」到货数量没有说明，按 {quantity} 套处理；"
                    "要改数量请说“到货 3 套”。"
                )

        # 期限：「严重故障 7 月 15 日前必须修完」
        for match in re.finditer(
            r"(.{0,8}?)" + self._MONTH_DAY.pattern + r"前(?:必须|得|要)?(?:修完|完成|修好|搞定|结束)",
            text,
        ):
            moment = self._resolve_moment(match.group(2), match.group(3), match.group(4))
            if not moment:
                errors.append(f"「{match.group(3)}月{match.group(4)}日」不是一个有效日期。")
                continue
            level = self._match_severity(match.group(1))
            key = f"deadline:{level}:{moment}"
            if key in seen:
                continue
            seen.add(key)
            patch = {
                "kind": "task_deadline",
                "at": moment,
                "scope": "全部任务" if level is None else f"{level}严重任务",
            }
            if level:
                patch["severity"] = level
            patches.append(patch)

        # 工时绝对值 + 定点：「叶片在高风险需要修 15 小时」「把叶片工时定成 15 小时」。
        # 和下面那条倍率写法的区别：这里给的是**这一档的最终小时数**，不是乘数。
        # 只在能锚定到部件名时才收——「015 要 15 小时」这种没点部件的说法，
        # 部件库里没有对应列可改，硬猜一个部件等于编造改动。
        for match in re.finditer(
            r"(?:修|工时\s*(?:定|改|设|调整|调)(?:成|为|到))\s*(\d+(?:\.\d+)?)\s*(?:个)?小时",
            text,
        ):
            head = text[max(0, match.start() - 16): match.start()]
            if re.search(r"每天|每日|最多|不超过|上限|只能", head):
                continue
            part = self._match_part(head)
            if not part:
                continue
            # 部件名之后到"修"之间只能有定语，否则不是给这个部件定工时。
            index = head.rfind(part[1])
            glue = head[index + len(part[1]):]
            if self._HOURS_SCOPE_GLUE.sub("", glue).strip():
                continue
            hours = float(match.group(1))
            if hours <= 0:
                errors.append(f"「{part[1]}」的维修工时必须是正数。")
                continue
            level = self._match_severity(glue)
            sea = "恶劣但安全" if "恶劣" in glue else ("常态" if "常态" in glue else None)
            key = f"hours:{part[0]}:{level or '*'}:{sea or '*'}"
            if key in seen:
                continue
            seen.add(key)
            patch = {
                "kind": "part_hours",
                "partId": part[0],
                "partName": part[1],
                "hours": round(hours, 6),
            }
            if level:
                patch["severity"] = level
            if sea:
                patch["sea"] = sea
            patches.append(patch)

        # 工时：「齿轮箱维修工时上涨 20%」
        for match in re.finditer(
            r"(.{0,6}?)维修工时(?:上涨|上调|增加|提高|涨|变长)\s*(\d+(?:\.\d+)?)\s*%",
            text,
        ):
            part = self._match_part(match.group(1))
            if not part:
                errors.append(
                    "没有认出要改工时的部件。现有部件："
                    + part_catalog_text()
                    + "。"
                )
                continue
            factor = 1 + float(match.group(2)) / 100
            if factor <= 0:
                errors.append(f"「{part[1]}」的工时倍率必须为正。")
                continue
            key = f"hours:{part[0]}"
            if key in seen:
                continue
            seen.add(key)
            patches.append(
                {
                    "kind": "part_hours",
                    "partId": part[0],
                    "partName": part[1],
                    "factor": round(factor, 6),
                }
            )

        return patches, notes, errors

    # ------------------------------------------------------------------ weights
    def _parse_weights(self, text: str, *, weighted_mode: bool) -> dict[str, float]:
        mentioned: dict[str, float] = {}
        registered = set(objective_catalog.all_weighted_metrics())
        for metric, aliases in self._WEIGHT_ALIASES.items():
            if metric not in registered:
                continue
            for alias in aliases:
                escaped = re.escape(alias)
                patterns = [
                    rf"{escaped}(?:的)?(?:权重|占比)\s*(?:为|=|:|：)?\s*(\d+(?:\.\d+)?)",
                ]
                if weighted_mode:
                    # 加权语境下，指标后面紧跟的数字就是权重；
                    # 但要排除“总成本 60 万”“完工时间 11 月 9 日”这类业务量。
                    patterns.append(
                        rf"{escaped}\s*(?:为|=|:|：|改成|设为|设成)?\s*"
                        r"(\d+(?:\.\d+)?)(?!\s*(?:万|元|天|艘|小时|分钟|年|月|日|%|％))"
                    )
                match = next(
                    (found for found in (re.search(p, text) for p in patterns) if found),
                    None,
                )
                if match:
                    mentioned[metric] = float(match.group(1))
                    break
        return mentioned

    def _resolve_weights(
        self,
        request: PlanningRequest,
        *,
        mentioned: dict[str, float],
        previous: PlanningRequest | None,
        strategy_changed: bool,
    ) -> tuple[PlanningRequest, list[str], list[str]]:
        if request.mode != objective_catalog.WEIGHTED_MODE:
            return replace(request, weights=None), [], []

        allowed = objective_catalog.weighted_metrics(request.strategy)
        notes: list[str] = []
        errors: list[str] = []

        invalid = [name for name in mentioned if name not in allowed]
        if invalid:
            for name in invalid:
                owners = objective_catalog.strategies_with_metric(name)
                where = "、".join(f"「{item}」" for item in owners) or "任何策略"
                errors.append(
                    f"「{name}」不在「{request.strategy}」的可加权指标里，"
                    f"它属于{where}；请换成对应策略，或删掉这项再算。"
                )
            errors.append(
                f"「{request.strategy}」当前可加权指标："
                + "、".join(allowed)
                + "。"
            )
            return replace(request, weights=None), notes, errors

        if mentioned:
            aligned = {name: float(mentioned.get(name, 0.0)) for name in allowed}
            zeroed = [name for name in allowed if name not in mentioned]
            if zeroed:
                notes.append(
                    "加权必须覆盖该策略的全部指标，未提到的 "
                    + "、".join(zeroed)
                    + " 按 0 处理。"
                )
            return replace(request, weights=aligned), notes, errors

        defaults = objective_catalog.weighted_defaults(request.strategy)
        inherited = (previous.weights if previous else None) or {}
        if not strategy_changed and inherited and set(inherited) == set(allowed):
            return replace(request, weights=dict(inherited)), notes, errors

        reason = (
            "更换了优化策略，原有权重不再适用，已改用该策略的默认权重"
            if strategy_changed
            else "未指定权重，已使用该策略的默认权重"
        )
        notes.append(
            reason
            + "："
            + "、".join(f"{name} {value:.2f}" for name, value in defaults.items())
            + "。"
        )
        return replace(request, weights=dict(defaults)), notes, errors

    # ------------------------------------------------------------------ routing
    @staticmethod
    def is_planning_request(message: str) -> bool:
        text = re.sub(r"\s+", "", message)
        explicit_action = any(
            token in text
            for token in ("重新排程", "重新安排", "生成方案", "帮我排", "调整排程", "开始求解", "重新求解")
        ) or bool(re.search(r"(?:排|生成|出|做|算)\s*(?:一版|一个|一份|个|版)?\s*(?:方案|排程|计划)", text))
        question = any(
            token in text
            for token in ("为什么", "是什么", "是多少", "什么算法", "怎么理解", "怎么", "如何",
                          "有哪些", "能否介绍", "？", "?")
        ) or bool(re.search(r"多少|几[个项天条人艘]|哪个|哪一版|对比|比较|区别|差别|差在哪", text))
        if question and not explicit_action:
            return False
        if explicit_action:
            return True
        if PlanningIntentCompiler._is_weight_change(text):
            return True
        if PlanningIntentCompiler._is_record_change(text):
            return True
        # 注意：这里不要放裸的「开工」「收工」。「候选开工粒度是多少」是查询，
        # 带上这两个字就会被误判成排程请求；真正的时刻改动由 _RECORD_PATTERNS
        # 里带数字的模式（“开工改成9点”“下午5点半收工”）兜住。
        return any(
            token in message
            for token in (
                "船租上涨", "船租上调", "最多作业线", "不超过", "每天最多维修",
                "船租改成", "船租改为", "船租设为", "船租调整为",
                "每天作业时间", "每天工作时间", "每天维修时间",
                "人工单价", "人工成本", "人工费用", "人员上限", "人员总上限",
                "转移时间", "转移小时数",
                "成本优先", "成本最低优先", "经济性优先", "风险优先", "高风险优先", "最短完成优先",
                "检修最多器件优先", "预算控制在", "改成加权", "改成分层",
            )
        )

    # 指向业务记录行的改动。这些说法不在原来的八个参数里，
    # 但它们改动的都是模型本来就支持的业务数据，不是新约束。
    _RECORD_PATTERNS = (
        r"到货",
        r"\d{1,2}月\d{1,2}[日号]前(?:必须|得|要)?(?:修完|完成|修好|搞定|结束)",
        r"维修工时(?:上涨|上调|增加|提高|涨|变长)\s*\d",
        r"现金(?:支出|预算)(?:最多|不超过|控制在|上限)\s*\d",
        r"(?:T|任务)\s*\d{1,3}(?:必须|一定要|务必)(?:修|安排|完成)",
        r"(?:最多|不超过|限定|只能)?\s*(?<![同这那])(?:\d+|一|两|二|三|四)\s*条(?:作业)?线",
        r"每天[^\d]{0,6}(?:只能|最多|不超过|限定|只有|至多|只|仅)(?:维修|作业|施工|检修|干活|干|做|工作|时间)?\s*\d+(?:\.\d+)?\s*(?:个)?小时",
        r"人工(?:单价|成本|费用|工时费)?\s*(?:改成|改为|设为|调整为|定为|调成|涨到|降到|上涨|上调|下降|降低)\s*\d",
        r"(?:人员|人手|工人|施工|作业)?(?:总)?(?:人数|人员|人力|上限)\s*(?:改成|改为|设为|调整为|定为|控制在|不超过|最多|只有|限定)?\s*\d",
        r"(?:最多|不超过|限定|只有)\s*\d+\s*人",
        r"转移(?:时间|小时数|耗时|用时)?\s*(?:改成|改为|设为|调整为|定为|需要|要|用)?\s*\d+(?:\.\d+)?\s*(?:个)?小时",
        r"\d{1,2}\s*(?:点|[:：]\s*\d{2})[^，。]{0,6}(?:开工|开始|上班|出海|收工|结束|下班|回港)",
        r"(?:开工|开始|上班|出海|收工|结束|下班|回港)[^，。]{0,6}\d{1,2}\s*(?:点|[:：]\s*\d{2})",
    )

    @classmethod
    def _is_record_change(cls, text: str) -> bool:
        if any(re.search(pattern, text) for pattern in cls._RECORD_PATTERNS):
            return True
        return bool(cls._HOURS_ABSOLUTE.search(text))

    @classmethod
    def _is_weight_change(cls, text: str) -> bool:
        """「权重」只有在带数值或明确的模式变更时才算改动。

        “我有哪些指标可以设置权重”是能力问句，不能触发求解。
        """
        if re.search(r"(?:权重|占比)[^，。；;！？?]{0,10}?(\d+(?:\.\d+)?)", text):
            return True
        if "权重" in text and re.search(r"(?:为|=|:|：|改成|设为)\s*0?\.\d+", text):
            return True
        return any(token in text for token in cls._MODE_TOKENS)

    @staticmethod
    def _normalize(message: str) -> str:
        return re.sub(r"\s+", "", message.strip())
