"""多轮会话：把一轮轮补丁攒成一个「当前生效的旋钮表」，再交给 pipeline 跑。

单轮和多轮的区别不在模型层（模型每轮只做同一件事：把这句话翻成补丁），
而在**补丁怎么落地**。这里定四条规矩：

1. **同格覆盖 = 修订**（D13）。上一轮把现金设成 80 万，这一轮说「改成 100 万」，
   后一条覆盖前一条。同一轮内同一格写两次仍然报错（D2 不变）——
   一轮之内写两次是模型没合并，跨轮写两次是用户改了主意，两件事不能混。
2. **相对量叠加在当前生效值上**（D14）。「再涨一成」是在上一轮的结果上再涨，
   不是回到基线重算。折算在会话层做，进模型的一律是绝对值（继承 D2 的纪律）。
3. **reset 是撤销指令**（D15）。撤销一个不存在的格子不算错，但要回显「本来就没设」——
   用户以为自己撤掉了一条约束、其实那条从没生效过，这必须说出来。
4. **状态要进提示词**（D16）。「刚才那条去掉」能不能落地，取决于模型能不能
   照抄上一轮那一格的索引。所以每轮把生效补丁按补丁原格式印进提示词。

为什么不做「对话记忆压缩/摘要」：这里的状态是一张确定的表，压缩只会丢索引精度。
多轮的价值全在「同一格被反复改」，把表原样摆出来最省事也最不容易错。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import slots as S
from .patch import (Patch, absolute_value, duplicate_cells, fingerprint, index_shape_error,
                    normalize_patch, sort_patches)


@dataclass
class Turn:
    """一轮的落地记录。回显、审计、评测都读它。"""

    原话: str
    新增: list[Patch] = field(default_factory=list)
    修订: list[tuple[Patch, Patch]] = field(default_factory=list)   # (旧, 新)
    撤销: list[Patch] = field(default_factory=list)                 # 被撤掉的旧补丁
    空撤销: list[Patch] = field(default_factory=list)               # reset 了一个本来不存在的格子
    无变化: list[Patch] = field(default_factory=list)               # 又说了同一格同一个值
    待确认: list[str] = field(default_factory=list)
    提示: list[str] = field(default_factory=list)                   # 没拦下、但用户该知道的事
    澄清: list[dict] | None = None   # 候选意图 [{"意图": str, "patches": [Patch...]}, ...]，让用户点选
    场景: list[dict] | None = None   # 复合需求 N 套 [{"标签": str, "patches": [Patch...]}, ...]
    拒绝: str | None = None
    错误: list[str] = field(default_factory=list)
    数据: Any = None        # 快照：回显要把格子说成人话（部件名称、几号船）

    def echo(self) -> list[str]:
        lines: list[str] = []
        for patch in self.新增:
            lines.append(f"记下了：{S.describe_patch(patch, self.数据)}")
        for old, new in self.修订:
            was = f"（原来那句：{old.utterance}）" if old.utterance else ""
            lines.append(f"改主意了：{S.describe_patch(old, self.数据)} → "
                         f"{S.describe_patch(new, self.数据)}{was}")
        for patch in self.撤销:
            lines.append(f"撤掉了：{S.describe_patch(patch, self.数据)}")
        for patch in self.空撤销:
            lines.append(f"这条本来就没设，撤销无效果："
                         f"{S.describe_patch(patch, self.数据, show_value=False)}")
        for patch in self.无变化:
            lines.append(f"这条已经在了，不用改：{S.describe_patch(patch, self.数据)}")
        for text in self.待确认:
            lines.append(f"需要你确认：{text}")
        for text in self.提示:
            lines.append(f"提醒一句：{text}")
        if self.澄清:
            questions = [str(g.get("问", "")) for g in self.澄清 if g.get("问")]
            if questions:
                lines.append("需要澄清：" + "；".join(questions))
            else:
                lines.append("需要你再补一个条件才能填槽。")
        if self.拒绝:
            lines.append(f"这句没接：{self.拒绝}")
        # 错误必须回显。只记在 错误 里、回显却不带，界面上就是「什么都没发生」——
        # 用户以为改完了，下一轮拿着老表继续说，分歧再也对不上。
        for text in self.错误:
            lines.append(f"这句没照做：{text}")
        return lines

    def touched(self) -> list[str]:
        """这一轮动过的格子（新增 + 修订 + 撤销），评测拿它判「动的是不是该动的格」。"""
        return [p.cell for p in self.新增] + [n.cell for _, n in self.修订] + \
            [p.cell for p in self.撤销] + [p.cell for p in self.空撤销] + \
            [p.cell for p in self.无变化]


class Session:
    """一张会话来来往往的旋钮表。

    ``state`` 的键是 ``Patch.cell``（槽位 + 规范化索引），值是**已经是绝对值**的补丁。
    用格子当键而不是列表追加，是因为「改主意」必须落在同一格里——追加列表会让
    「船租涨两成」和「船租涨到 15 万」同时留在表上，最后由谁生效取决于顺序。
    """

    def __init__(self, base_data=None, *, base_id: str = "基座01") -> None:
        self.base_data = base_data
        self.base_id = base_id
        self.state: dict[str, Patch] = {}
        self.turns: list[Turn] = []

    # ── 落地 ────────────────────────────────────────────────────────────
    def _baseline(self, patch: Patch) -> float | None:
        """这一格的基线值：参数读快照，其余没有基线。"""
        if patch.slot in S.PARAM_SLOTS and self.base_data is not None:
            return self.base_data.params.get(S.PARAM_ALIASES[patch.slot])
        return None

    def _absolute(self, patch: Patch) -> tuple[Patch, str | None]:
        """把相对运算折算成绝对值。基数取**当前生效值**（没有则取基线）。

        折算失败不在这里编一个数：原样返回，让 apply 用同一套口径报错，
        免得两处各说一句话。
        """
        if patch.op in ("=", "reset"):
            return patch, None
        # 数据/时序类槽位存的是**增量**（维修期限提前几天、工时改多少），不是绝对值。
        # 把两次「提前三天」「提前两天」折成「= -7200」会变成「把期限定到规划起点前五天」，
        # 语义整个翻掉。这一类只做增量累加，别折叠成绝对量。
        slot = S.SLOTS.get(patch.slot)
        if slot is not None and slot.route in ("data", "timeline") and patch.op in ("+", "-"):
            current = self.state.get(patch.cell)
            if current is None or not isinstance(current.value, (int, float)):
                return patch, None            # 还没有生效值：这条增量就是相对基线的增量
            delta = float(patch.value) if patch.op == "+" else -float(patch.value)
            if current.op == "=":             # 上一轮是绝对值：累加后仍是绝对值
                value, op = round(float(current.value) + delta, 6), "="
            else:                             # 上一轮也是增量：两份增量加起来
                value, op = round(float(current.value) + delta, 6), "+"
            return Patch(slot=patch.slot, index=patch.index, op=op, value=value,
                         strength=patch.strength, utterance=patch.utterance), None
        current = self.state.get(patch.cell)
        base = current.value if current is not None and isinstance(current.value, (int, float)) \
            else self._baseline(patch)
        if base is None:
            return patch, None
        value, error = absolute_value(patch.op, patch.value, base)
        if error:
            return patch, error
        # 浮点尾数不是业务信息：14400*1.1 会算出 15840.000000000002，
        # 原样落库会让指纹随机器抖动，回显也不像人话。
        if isinstance(value, float):
            value = round(value, 6)
        folded = Patch(slot=patch.slot, index=patch.index, op="=", value=value,
                       strength=patch.strength, utterance=patch.utterance)
        return folded, None

    def apply(self, patches: list[Patch], *, utterance: str = "") -> Turn:
        """把一轮补丁落到状态上。返回这一轮的落地记录（不抛错，错误记在 Turn 里）。

        **落地级的原子性在这里**：折叠失败 / 同格重复 / 空补丁，任何一条出问题就整轮回滚。
        但注册层（是否在册、是否在区间）与编译层（能否编成内核入参）的校验**不在这里**——
        那是 `say()` 和 `check()` 的事。直接调 `apply` 的调用方（界面、评测、将来的 API）
        必须自己接着 `check()`，否则非法补丁会留在表里。走 `say()` 就没有这个问题。
        """
        before = dict(self.state)
        bad = [str(i) for i, p in enumerate(patches, 1) if p is None]
        turn = Turn(原话=utterance or "；".join(p.utterance for p in patches if p and p.utterance),
                    数据=self.base_data)

        if bad:
            turn.错误.append(f"第 {'、'.join(bad)} 条补丁是空的（解析没成功）。整句先没执行")
            self.turns.append(turn)
            return turn
        shapes = [f"第 {i} 条：{err}" for i, p in enumerate(patches, 1)
                  if (err := index_shape_error(p.slot, p.index))]
        if shapes:
            turn.错误.extend(shapes)
            self.turns.append(turn)
            return turn
        patches = [normalize_patch(p) for p in patches]

        # 同一轮内同格重复仍然是模型没合并（D2），不替它猜哪条优先
        dups = duplicate_cells(patches)
        if dups:
            turn.错误.append("同一句话里同一格被写了两次（请合并成一个补丁）：" + "；".join(dups))
            self.turns.append(turn)
            return turn

        for patch in patches:
            if patch.strength == "unsure":
                turn.待确认.append(patch.utterance or S.describe_patch(patch, self.base_data))
            if patch.op == "reset":
                # 不带索引的 reset 是「这一族整个不要了」——D15 承诺过，得真做到
                if not patch.index:
                    cells = [cell for cell, old in self.state.items() if old.slot == patch.slot]
                    if not cells:
                        turn.空撤销.append(patch)
                    for cell in cells:
                        turn.撤销.append(self.state.pop(cell))
                    continue
                old = self.state.pop(patch.cell, None)
                if old is None:
                    turn.空撤销.append(patch)
                else:
                    turn.撤销.append(old)
                continue
            folded, error = self._absolute(patch)
            if error:
                turn.错误.append(f"{S.describe_patch(folded, self.base_data)}：{error}")
                continue
            old = self.state.get(folded.cell)
            if old is None:
                turn.新增.append(folded)
            elif old.op == folded.op and old.value == folded.value \
                    and old.strength == folded.strength:
                # 又说了一遍同一件事：不算改主意，也别回显成「A → A」
                turn.无变化.append(folded)
                continue
            else:
                turn.修订.append((old, folded))
            self.state[folded.cell] = folded

        if turn.错误:
            # 一条不行，整轮作废：留着「改成功的那几条」，下一轮的分歧就再也追不回来了
            self.state = before
            turn.新增.clear()
            turn.修订.clear()
            turn.撤销.clear()
            turn.空撤销.clear()
        else:
            turn.提示.extend(self._wider_hints(turn, before))

        self.turns.append(turn)
        return turn

    def _wider_hints(self, turn: Turn, before: dict) -> list[str]:
        """「全局那一条还挂着，又加了组上一条」——最典型的改主意失败。

        用户说「不要 8 个了，改成风机003最多 2 个」，模型只加了新格、没撤旧的，
        结果表上两条一起生效，排出来的比用户想要的更紧。这不是错（两条都能解释得通），
        所以不拦；但不吭声就成了静默多一条限制，必须说出来。
        """
        out: list[str] = []
        seen: set[tuple[str, str]] = set()
        dropped = {p.cell for p in turn.撤销}          # 这一轮自己撤掉的，不算「还挂着」
        for patch in turn.新增 + [new for _, new in turn.修订]:
            group = patch.index.get("组") if isinstance(patch.index, dict) else None
            kind = (group or {}).get("kind") if isinstance(group, dict) else None
            if kind in (None, "全部"):
                continue
            for cell, old in before.items():
                if cell == patch.cell or cell in dropped or old.slot != patch.slot:
                    continue
                old_group = old.index.get("组") if isinstance(old.index, dict) else None
                if not isinstance(old_group, dict) or old_group.get("kind") != "全部":
                    continue
                if (cell, patch.cell) in seen:
                    continue
                seen.add((cell, patch.cell))
                out.append(f"「{S.describe_patch(old, self.base_data)}」还在生效，"
                           f"现在又加了「{S.describe_patch(patch, self.base_data)}」，"
                           "两条限制同时在。如果本意是换掉旧的那条，说一声我去掉")
        return out

    def clear(self) -> None:
        self.state.clear()
        self.turns.clear()

    # ── 说一句话 ────────────────────────────────────────────────────────
    def say(self, utterance: str, data=None, options=None, *, timeout: float = 90.0) -> Turn:
        """翻译 + 落地一轮。这是多轮的正常入口：一句话进，一张表出。

        翻译失败（没配 key / 模型跑偏）不写进状态——半句话不许改变排程依据。
        """
        from . import PROJECT_ROOT, solver_modules
        from .parse import LLMUnavailable, translate

        modules = solver_modules()
        notes: list[str] = []
        if data is None:
            data = self.base_data or modules["load_project"](PROJECT_ROOT, base_id=self.base_id)
            self.base_data = data
        if options is None:
            options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
        try:
            patches, reason, clarify, scenarios = translate(utterance, data, options,
                                                            timeout=timeout,
                                                            session_text=self.prompt_state(),
                                                            notes_out=notes)
        except LLMUnavailable as exc:
            # 翻译失败不毁会话：这一轮记一条「没翻成」，表不动，用户可以再说一遍。
            # 一路抛出去的话，界面层拿到的是一条栈，而不是「刚才那句没听懂」。
            turn = Turn(原话=utterance, 数据=self.base_data)
            # 直接放原因，不再加「翻成补丁失败」这类前缀——回显那边已经有「这句没照做」
            turn.错误.append(str(exc))
            self.turns.append(turn)
            return turn
        if scenarios:
            # 复合需求：不落本会话状态，直接把 N 套场景交出去
            turn = Turn(原话=utterance, 数据=self.base_data)
            turn.场景 = scenarios
            self.turns.append(turn)
            return turn
        before = dict(self.state)
        turn = self.apply(patches, utterance=utterance)
        if reason:
            # 有补丁又有拒绝理由时两件事都记：不许悄悄只留一半
            turn.拒绝 = reason
        if clarify:
            turn.澄清 = clarify
        if notes:
            # 绝对优先复核改了什么，必须摆到用户面前，不静默改
            turn.提示.extend(notes)
        if not turn.错误 and patches:
            # 一轮要么整体落地、要么整体不落地。半个补丁进状态比拦下来危险得多：
            # 下一轮会拿着这张被污染的表继续改，错的地方再也不会被提出来。
            # 校验两道都要过：注册层（在册/区间）+ 编译层（真能编成内核入参）。
            errors, _ = self.check(data, options)
            if errors:
                self.state = before
                turn.新增.clear()
                turn.修订.clear()
                turn.撤销.clear()
                turn.空撤销.clear()
                turn.错误.extend(errors)
        return turn

    def say_all(self, utterances: list[str], *, timeout: float = 90.0) -> list[Turn]:
        """连续说几句（评测和回归用）。每轮都看得见上一轮的结果。"""
        return [self.say(text, timeout=timeout) for text in utterances]

    # ── 读出 ────────────────────────────────────────────────────────────
    def patches(self) -> list[Patch]:
        """当前生效的补丁集（排序稳定，指纹才可复现）。"""
        return sort_patches(list(self.state.values()))

    def is_empty(self) -> bool:
        return not self.state

    def summary(self) -> list[str]:
        """当前生效的旋钮，一句话一条——这是回给用户看的「现在到底按什么在排」。"""
        return [S.describe_patch(p, self.base_data) for p in self.patches()]

    def prompt_state(self) -> str:
        """给提示词用的当前状态：**补丁原格式**，索引一个字都不改。

        为什么不用人话版：模型要照抄索引才能定位同一格。人话版（「叶片的任务」）
        抄回去会变成另一个 kind，落到别的格子上——「改主意」就成了「加一条」。
        """
        if not self.state:
            return ""
        lines = [f"这是第 {len(self.turns) + 1} 轮。当前已生效 {len(self.state)} 条旋钮"
                 "（要改哪一格，就在下面这条上改：slot 与 index 必须原样照抄）："]
        for i, patch in enumerate(self.patches(), 1):
            payload = patch.to_json()
            payload.pop("原话", None)
            lines.append(f"{i}. " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)

    def trace(self) -> list[dict[str, Any]]:
        """逐轮轨迹：审计用。同一批生效补丁可能是不同对话路径走到的。"""
        return [{
            "原话": t.原话,
            "新增": [p.to_json() for p in t.新增],
            "修订": [{"旧": o.to_json(), "新": n.to_json()} for o, n in t.修订],
            "撤销": [p.to_json() for p in t.撤销],
            "空撤销": [p.to_json() for p in t.空撤销],
            "无变化": [p.to_json() for p in t.无变化],
            "待确认": t.待确认,
            "澄清": (
                [{"问": g.get("问"),
                  "候选": [{"意图": c.get("意图"),
                            "性质": c.get("性质"), "推荐": c.get("推荐"),
                            "patches": [p.to_json() for p in c.get("patches") or []]}
                           for c in (g.get("候选") or [])]}
                 for g in t.澄清] if t.澄清 else None
            ),
            "场景": (
                [{"标签": s.get("标签"), "patches": [p.to_json() for p in s.get("patches") or []]}
                 for s in t.场景] if t.场景 else None
            ),
            "拒绝": t.拒绝,
            "错误": t.错误,
        } for t in self.turns]

    def fingerprint(self) -> str:
        """会话指纹：算的是**生效补丁 + 轮数**。

        只算生效补丁的话，「说了三遍才拧对」和「一次说对」会撞同一个指纹，
        审计时分不清用户到底改过几次主意。
        """
        return fingerprint(self.patches(), {"轮数": len(self.turns), "基座": self.base_id})

    # ── 校验 / 求解 ─────────────────────────────────────────────────────
    def _ensure_data(self, data=None, options=None):
        from . import PROJECT_ROOT, solver_modules

        modules = solver_modules()
        if data is None:
            data = self.base_data or modules["load_project"](PROJECT_ROOT, base_id=self.base_id)
            self.base_data = data
        if options is None:
            options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
        return data, options

    def validate(self, data=None, options=None) -> tuple[list[str], list[str]]:
        """只校验当前状态（注册层），不求解、不编译。"""
        from .validate import validate as _validate

        data, options = self._ensure_data(data, options)
        return _validate(self.patches(), data, options)

    def check(self, data=None, options=None) -> tuple[list[str], list[str]]:
        """校验 + 编译两道一起过。返回 (错误, 告警)。

        为什么多一道编译：注册层只管「槽位/索引/数值在册」，
        有些错（部件名解析不到、时刻读不出、注入口里没候选）要到编译才暴露。
        多轮里这一道是必须的——否则脏补丁会留在状态里，下一轮继续在错的基础上改。
        """
        from .apply import compile_patches
        from .validate import validate as _validate

        data, options = self._ensure_data(data, options)
        patches = self.patches()
        errors, warnings = _validate(patches, data, options)
        _request, compile_errors = compile_patches(patches, data, data)
        for error in compile_errors:
            if error not in errors:
                errors.append(error)
        return errors, warnings

    def run(self, **kwargs) -> dict[str, Any]:
        """用当前状态跑一次求解。"""
        from .pipeline import run as _run

        return _run(self.patches(), base_id=self.base_id, **kwargs)
