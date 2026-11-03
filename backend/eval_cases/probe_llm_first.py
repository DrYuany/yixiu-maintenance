"""探针：验证「大模型先判读 + 自报覆盖账」这个契约在真实模型上是否成立。

只读、不改产线：复用注册表系统提示词，替换输出契约段，跑若干难例。

用法：
    MINIMAX_API_KEY=... python backend/eval_cases/probe_llm_first.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
CONTRACT = """## 输出格式（统一 JSON，不要解释、不要代码围栏）

{
  "route": "modify_plan" | "modify_data" | "query_plan" | "compare" | "explain" | "chitchat" | "reject" | "clarify",
  "dsl": {...} | null,
  "spans": [{"text": "原文中的一个连续片段（逐字复制）", "into": "吃下它的槽位，如 objectives[0].name"}],
  "unresolved": ["原文中没有任何槽位能吃的片段（逐字复制）"],
  "ask": "需要用户补充的一句话" | null,
  "confidence": 0.0~1.0
}

dsl 的骨架（照这个形状写，字段名一个都不能变）：
{
  "parameters": [{"field": "每日有效维修上限", "op": "=", "value": 8}],
  "constraints": [{"type": "budget", "quantity": "现金支出", "op": "<=", "value": 3000000}],
  "objectives": [{"type": "strategy", "name": "成本最低优先"}],
  "data": [{"kind": "part_arrival", "part": "部件01", "at": "2026-11-05T08:00:00+08:00"}]
}
parameters 必须是**数组**，每项是含 field/op/value 的对象。
禁止写成 {"每日有效维修上限": 8} 这种字典形式，系统会直接判结构错误。

范围类诉求的处理（重要）：
- 「只排/只看/仅限 某风机或某部件的任务」——DSL 没有范围原语，当前做不到。
  route=clarify，ask 里说清"目前只能按任务编号逐条指定，不能按风机/部件圈定"。
- 严禁用枚举 exclude 的方式伪造这个能力（写 19 条 exclude 去凑"只排风机003"）。

route 判据：
- modify_plan：要改排程的方案/目标/约束/参数 → dsl 必须有内容
- modify_data：要改底层数据（到货、工时、天气海况、电价时序）→ dsl 用 data 段
- query_plan：问当前方案的事实（排了什么、成本多少、哪天开工）
- compare：拿两个方案/两个策略对比
- explain：问为什么、怎么算的、模型原理
- chitchat：寒暄、与业务无关
- reject：明确越界（非线性目标、改颜色、未建模的业务概念）
- clarify：指代不明、缺基准、引用的任务/风机不存在、或诉求超出「你能改什么」

spans / unresolved 是硬要求，系统会拿它做对账，不是给你自己看的：
1. 先按标点、连词（，。；、和 还有 另外 同时）把原话切成小段；
2. 每一段都必须在 spans 里有归属，逐字复制原文，不要改写、不要合并；
3. 有一段吃不下（说不出它变成 DSL 的哪个字段）→ 放进 unresolved，dsl 里就不要硬编；
4. unresolved 非空时：能确定主体就 route=clarify 并在 ask 里问缺什么；完全越界才 reject；
5. route≠modify_plan/modify_data 时 dsl 写 null，spans 仍要覆盖全句。

其余规范与本提示词前面各段一致（参数名、op 写法、时间格式、金额单位、编号全称）。"""

CASES = [
    "按成本最低优先排每天只能干8小时",
    "钱不超过300万",
    "船租翻倍了，重新排一下",
    "经济优先和风险优先差多少",
    "先修任务001再修任务002，钱别超300万",
    "只排风机003的任务",
    "每天只能干8小时，另外最多上2条作业线，别排任务005",
    "为什么这一版比上一版贵了20万",
    "任务001和任务002不要同一天做",
    "明天风大，先别出海",
    "成本压到最低，但高风险的必须全修",
    "试试看能不能三天干完",
    "预算改成350万，其他不动",
]


def build_system(message: str) -> str:
    from dsl_core.registry import schema_prompt as sp

    base = sp.build_system_prompt(message)
    head, _, _ = base.partition("## 输出格式")
    return head + CONTRACT


def main() -> int:
    from anthropic import Anthropic
    import httpx

    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        print("缺少 MINIMAX_API_KEY")
        return 2
    client = Anthropic(
        api_key=key, auth_token="",
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.deepseek.com/anthropic"),
        http_client=httpx.Client(trust_env=False, timeout=90.0),
    )
    client.auth_token = None
    model = os.environ.get("MINIMAX_MODEL", "deepseek-chat")

    from dsl_core.validator import validate_dsl

    out = []
    for text in CASES:
        t0 = time.monotonic()
        resp = client.messages.create(
            model=model, max_tokens=1200, temperature=0.0,
            system=build_system(text),
            messages=[{"role": "user", "content": f"把这句话翻译成 JSON：{text}"}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        elapsed = round(time.monotonic() - t0, 2)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"\n【{text}】{elapsed}s 非 JSON：{raw[:200]}")
            out.append({"utterance": text, "error": "bad_json", "raw": raw[:400]})
            continue
        dsl = payload.get("dsl")
        errs = validate_dsl(dsl) if isinstance(dsl, dict) else []
        spans = payload.get("spans") or []
        unresolved = payload.get("unresolved") or []
        covered = "".join(s.get("text", "") for s in spans if isinstance(s, dict))
        print(f"\n【{text}】{elapsed}s route={payload.get('route')} conf={payload.get('confidence')}")
        print(f"   spans={json.dumps(spans, ensure_ascii=False)}")
        print(f"   unresolved={json.dumps(unresolved, ensure_ascii=False)}")
        print(f"   dsl={json.dumps(dsl, ensure_ascii=False)}")
        if errs:
            print(f"   校验错误: {errs[:3]}")
        if payload.get("ask"):
            print(f"   ask={payload['ask']}")
        out.append({
            "utterance": text, "elapsed": elapsed, "payload": payload,
            "validate_errors": errs, "covered_len": len(covered),
        })

    dest = ROOT / "eval_cases" / "_probe_llm_first_result.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
