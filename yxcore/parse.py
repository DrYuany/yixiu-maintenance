"""自然语言 → 补丁。只做翻译，不做任何判断。

这一层是整条链路里唯一调大模型的地方，也是最薄的一层：
提示词由注册表反向生成（prompt.py），吐回来的是三元组，校验交给 validate.py。
大模型在这里**不写模型代码、不发明约束**，它只填数。

复用项目已有的环境变量约定（DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL，
兼容 MINIMAX_API_KEY），不另造配置。没配 key 就明确报 LLMUnavailable，不假装能用。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .patch import Patch, normalize_patch, parse_patch
from .prompt import build_prompt

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class LLMUnavailable(RuntimeError):
    """没配 key 或调不通。调用方要如实告诉用户，不要降级成猜。"""


class Refused(RuntimeError):
    """大模型明确说这句话做不到（对应边界清单）。"""


def api_key() -> str:
    return (os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or "").strip()


def configured() -> bool:
    return bool(api_key())


def _chat(system: str, user: str, *, timeout: float = 90.0, temperature: float = 0.0) -> str:
    key = api_key()
    if not key:
        raise LLMUnavailable("未配置 DEEPSEEK_API_KEY（也可用 MINIMAX_API_KEY）")
    base = (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE).rstrip("/")
    # 模型名与后端对齐：后端用 MINIMAX_MODEL（本账号配 deepseek-flash），
    # yxcore 原来只认 DEEPSEEK_MODEL，没配时默认落到 deepseek-chat，与后端不一致。
    model = (os.environ.get("DEEPSEEK_MODEL")
             or os.environ.get("MINIMAX_MODEL")
             or DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LLMUnavailable(f"大模型调用失败：{exc}") from exc
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(f"大模型返回结构异常：{body!r}") from exc


def _strip_fences(text: str) -> str:
    """模型偶尔还是会给 markdown 代码块，剥掉再解析（只剥 fence，不改内容）。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_clarify(payload: dict[str, Any]) -> list[dict] | None:
    """把「澄清」字段解析成**问题组** ``[{"问": str, "候选": [{"意图": str, "patches": [Patch...]}, ...]}, ...]``。

    一句话可能有多个歧义（复合意图），模型把它们拆成多个问题组，每组一个问句 + 若干候选补丁，
    让用户逐个点选。兼容旧的字符串形式（退回成开放式反问，无候选）。
    """
    raw = payload.get("澄清") or payload.get("clarify") or payload.get("需要澄清")
    if not raw:
        return None
    if isinstance(raw, str):
        return [{"问": raw, "候选": []}]
    if not isinstance(raw, list):
        return None

    def _patches(items) -> list[Patch]:
        patches: list[Patch] = []
        for p in items or []:
            if not isinstance(p, dict):
                continue
            patch, error = parse_patch(p)
            if patch is None:
                continue  # 某条候选补丁解析失败就跳过这条，不影响别的候选
            patches.append(normalize_patch(patch))
        return patches

    groups: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "候选" in item or "candidates" in item:
            # 分组形式：一个问句 + 一组候选
            question = str(item.get("问") or item.get("question") or "").strip()
            cands: list[dict] = []
            for c in item.get("候选") or item.get("candidates") or []:
                if not isinstance(c, dict):
                    continue
                intent = str(c.get("意图") or c.get("label") or c.get("name") or "").strip()
                pats = _patches(c.get("patches") or c.get("补丁"))
                if intent or pats:
                    cand: dict[str, Any] = {"意图": intent, "patches": pats}
                    # 硬/软标签与推荐位要一路带到界面：绝对优先那条必须看得出来是硬约束
                    if c.get("性质"):
                        cand["性质"] = str(c["性质"])
                    if "推荐" in c:
                        cand["推荐"] = bool(c["推荐"])
                    cands.append(cand)
            groups.append({"问": question, "候选": cands})
        else:
            # 旧平铺形式：一条候选，包成一个单候选组
            intent = str(item.get("意图") or item.get("label") or item.get("name") or "").strip()
            pats = _patches(item.get("patches") or item.get("补丁"))
            groups.append({"问": "", "候选": [{"意图": intent, "patches": pats}]})
    return groups or None


def _parse_scenarios(payload: dict[str, Any]) -> list[dict] | None:
    """把「scenarios」字段解析成复合场景 ``[{"标签": str, "patches": [Patch...]}, ...]``。

    模型在「分别按几种目标排然后对比」这类复合需求时，不拆成单场景补丁，而是给 N 套独立补丁。
    每套各写一个 strategy（或一组旋钮），求解时各跑一份再对比。
    """
    raw = payload.get("scenarios") or payload.get("场景")
    if not raw or not isinstance(raw, list):
        return None
    result: list[dict] = []
    for sc in raw:
        if not isinstance(sc, dict):
            continue
        label = str(sc.get("标签") or sc.get("label") or sc.get("name") or "").strip()
        patches: list[Patch] = []
        for p in sc.get("patches") or sc.get("补丁") or []:
            if not isinstance(p, dict):
                continue
            patch, error = parse_patch(p)
            if patch is None:
                continue  # 某套里的补丁解析失败就跳过这条，不影响别的套
            patches.append(normalize_patch(patch))
        if patches:
            result.append({"标签": label, "patches": patches})
    return result or None


def _translate_core(utterance: str, data, options: dict[str, list[dict]] | None = None, *,
                    extra: str = "", timeout: float = 90.0,
                    session_text: str = "") -> tuple[list[Patch], str | None, list[dict] | None,
                                                     list[dict] | None, list[str]]:
    """翻译成补丁集，并在出口做一次「绝对优先 → 硬约束」的形状复核。

    返回 (补丁, 拒绝理由, 澄清候选, 复合场景, 回显提示)。提示由会话层摆给用户看：
    改了什么、为什么改，不静默改。

    拒绝理由非空表示模型认为这句话做不到（边界清单里的一类）——这是**诚实的拒绝**，
    调用方要原样转述给用户，不许拿近似槽位补上。但「高危必须全修」不在此列：
    它能用 count_min 硬约束表达，模型判做不到时这里会自己补出来。

    澄清候选非空表示模型听懂了这句话，但**指代/范围/顺序/硬软强度不清**。此时不猜
    唯一解，而是枚举候选解释、各带一条能落地的补丁让用户点选；硬约束那条排在第一个
    并标「推荐」。旧字符串形式则退化成开放式反问。

    ``session_text`` 是多轮时上一段的「当前已生效旋钮」清单：模型靠它才能把
    「刚才那条去掉」落成同一个格子的 reset，否则只能凭记忆重写索引（十次有九次跑偏）。

    复核为什么放在这里而不是校验器：校验器只认补丁形状，不认「这句话该怎么翻译」；
    脚本手搓、回归 gold、多轮里已落地的补丁都直接进校验器，给它们套语义规则会把
    一批合法用例判死。只有大模型刚吐出来的这一批才需要复核。
    """
    from . import absolute  # noqa: PLC0415

    system = build_prompt(data, options, extra=extra, session_text=session_text)
    user = "\n".join([
        f"规划期：{data.stamp(0)[:10]} ~ {data.stamp(data.end)[:10]}",
        f"当前时点：{data.stamp(data.now)}",
        "",
        "用户说的话：",
        utterance,
    ])
    raw = _chat(system, user, timeout=timeout)
    try:
        payload = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"大模型返回的不是 JSON：{raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise LLMUnavailable(f"大模型返回的不是对象：{raw[:200]}")
    reason = payload.get("拒绝理由") or payload.get("refusal")
    clarify = _parse_clarify(payload)
    scenarios = _parse_scenarios(payload)
    if scenarios:
        # 复合需求：N 套独立补丁，各跑一份再对比
        return [], None, None, scenarios, []
    items = payload.get("patches") or payload.get("补丁") or []
    if not items:
        if clarify:
            return [], None, absolute.enrich_clarification(utterance, clarify, data, options), None, []
        # 「高风险的必须全修」历史上常被当成表达不了而拒掉（错误分类学 E4.3）。
        # 它其实能表达：count_min 硬约束。模型说做不到时不跟着拒，先自己复核一遍。
        outcome = absolute.enforce(utterance, [], data, options)
        if outcome.patches and not outcome.clarify:
            notes = [f"模型把这句判成做不到，但「{utterance}」可以落成硬约束，已按硬约束处理"] \
                + outcome.notes
            return outcome.patches, None, None, None, notes
        return [], reason or "大模型没有给出任何补丁", outcome.clarify, None, outcome.notes
    patches: list[Patch] = []
    errors: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            errors.append(f"补丁不是对象：{item!r}")
            continue
        item.setdefault("utterance", utterance)
        patch, error = parse_patch(item)
        if patch is None:
            errors.append(error or "补丁解析失败")
        else:
            patches.append(normalize_patch(patch))
    # 有一条没读懂就整句不执行。只返回解析成功的那几条，等于把「两个要求完成一个」
    # 说成「按你说的改了」——用户下一轮会拿着半张表继续改，漏掉的那半再也不会被提起。
    if errors:
        detail = "；".join(errors)
        if patches:
            raise LLMUnavailable(
                f"这句话里有一条我没读懂（{detail}）。整句先没执行——"
                "请再说一遍，或者把它拆成两句")
        raise LLMUnavailable(detail)
    outcome = absolute.enforce(utterance, patches, data, options)
    if outcome.clarify:
        # 语义含糊：不落补丁，把硬/软两条解释摆出来让人选
        return [], None, outcome.clarify, None, outcome.notes
    if clarify:
        clarify = absolute.enrich_clarification(utterance, clarify, data, options)
    return outcome.patches, reason, clarify, None, outcome.notes


def translate(utterance: str, data, options: dict[str, list[dict]] | None = None, *,
              extra: str = "", timeout: float = 90.0, session_text: str = "",
              notes_out: list[str] | None = None
              ) -> tuple[list[Patch], str | None, list[dict] | None, list[dict] | None]:
    """把一句话翻译成补丁集。返回 (补丁, 拒绝理由, 澄清候选, 复合场景)。

    兼容入口：评测、脚本、外部调用方拿的还是四元组。会话层把 ``notes_out`` 传进来
    就能收到「这句按硬约束处理了」这类回显提示；不传不影响旧调用方。
    两边共用同一份复核逻辑，不存在「从哪条路进来行为不同」。
    """
    patches, reason, clarify, scenarios, notes = _translate_core(
        utterance, data, options, extra=extra, timeout=timeout, session_text=session_text)
    if notes_out is not None:
        notes_out.extend(notes)
    return patches, reason, clarify, scenarios


def translate_full(utterance: str, data, options: dict[str, list[dict]] | None = None, *,
                   extra: str = "", timeout: float = 90.0,
                   session_text: str = "") -> tuple[list[Patch], str | None, list[dict] | None,
                                                    list[dict] | None, list[str]]:
    """同 ``translate``，但把回显提示作为第五项直接返回。"""
    return _translate_core(utterance, data, options, extra=extra, timeout=timeout,
                           session_text=session_text)


def translate_and_validate(utterance: str, data, options: dict[str, list[dict]] | None = None, *,
                           extra: str = "") -> tuple[list[Patch], list[str], list[str]]:
    """翻译 + 校验一步到位。返回 (补丁, 错误, 告警)。"""
    from .validate import validate

    patches, reason, clarify, _scenarios = translate(utterance, data, options, extra=extra)
    if not patches:
        if clarify:
            intents = [str(c.get("意图", "")) for c in clarify if c.get("意图")]
            raise Refused("需要澄清：" + "；".join(intents) if intents else "这句话没有对应的旋钮")
        raise Refused(reason or "这句话没有对应的旋钮")
    errors, warnings = validate(patches, data, options)
    return patches, errors, warnings


def self_check(data, options: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """给回归脚本用：提示词规模 + 是否配了 key。"""
    from .prompt import prompt_stats

    stats = prompt_stats(data, options)
    return {**stats, "已配置大模型": configured()}
