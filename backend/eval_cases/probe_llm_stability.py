"""实验：大模型判读的「可复现性」与「未见改写」表现。

回答一个问题：凭什么相信模型给出的槽位？
  不靠「它看起来很准」，靠两个可测的指标：
    A. 同一输入重复调用的一致性（temperature=0 是否真的确定）
    B. 同一语义、未见过的说法上的通过率（这是泛化，不是记忆）

用法：
    MINIMAX_API_KEY=... python backend/eval_cases/probe_llm_stability.py [--repeat N]
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: dsl_core
from eval_cases.probe_llm_first import build_system  # noqa: E402  复用同一份契约


def _client():
    from anthropic import Anthropic
    import httpx

    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        raise SystemExit("缺少 MINIMAX_API_KEY")
    c = Anthropic(
        api_key=key, auth_token="",
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.deepseek.com/anthropic"),
        http_client=httpx.Client(trust_env=False, timeout=90.0),
    )
    c.auth_token = None
    return c, os.environ.get("MINIMAX_MODEL", "deepseek-chat")


def ask(client, model: str, text: str) -> tuple[dict | None, str, float]:
    t0 = time.monotonic()
    resp = client.messages.create(
        model=model, max_tokens=1200, temperature=0.0,
        system=build_system(text),
        messages=[{"role": "user", "content": f"把这句话翻译成 JSON：{text}"}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    el = round(time.monotonic() - t0, 2)
    try:
        return json.loads(raw), raw, el
    except json.JSONDecodeError:
        return None, raw, el


# 同一句话，重复调用看是否一致
STABILITY_TEXT = "先修任务001再修任务002，钱别超300万"

# 同一语义（先001后002 + 预算300万），换没见过 6 种说法
PARAPHRASES = [
    "任务001要赶在任务002前头修，预算卡在300万以内",
    "先干001，干完再弄002，钱不能过三百万",
    "001 排前面、002 排后面，总开销别超过三百万元",
    "顺序上 001 在 002 之前，费用上限 3000000",
    "把001提前，002放后面，花钱别超300w",
    "任务002必须排在任务001之后，总花费控制在300万元以下",
]

# 长句 + 无关噪声：看能否剥出两个真诉求
NOISY = ("我在现场看了一圈，风有点大，海况也不太好，船租最近涨了不少，"
         "你先把方案重排一下，另外钱还是不能超过300万")


def _brief(payload: dict | None) -> str:
    if not payload:
        return "<非JSON>"
    dsl = payload.get("dsl") or {}
    return json.dumps({"route": payload.get("route"),
                       "params": [(p.get("field"), p.get("value")) for p in (dsl.get("parameters") or [])],
                       "cons": [(c.get("type"), c.get("value")) for c in (dsl.get("constraints") or [])],
                       "unresolved": payload.get("unresolved")}, ensure_ascii=False)


def _has_precedence(payload: dict | None) -> bool:
    dsl = (payload or {}).get("dsl") or {}
    return any(c.get("type") == "precedence" for c in (dsl.get("constraints") or []))


def _has_budget(payload: dict | None) -> bool:
    dsl = (payload or {}).get("dsl") or {}
    for c in dsl.get("constraints") or []:
        if c.get("type") == "budget" and abs(float(c.get("value") or 0) - 3_000_000) < 1:
            return True
    return False


def main() -> int:
    repeat = 6
    if "--repeat" in sys.argv:
        repeat = int(sys.argv[sys.argv.index("--repeat") + 1])

    client, model = _client()
    from dsl_core.validator import validate_dsl

    print("=" * 76)
    print(f"A. 可复现性：同一句话重复 {repeat} 次（temperature=0）")
    print(f"   输入：{STABILITY_TEXT}")
    sigs, both, ok_struct = Counter(), [], 0
    for i in range(repeat):
        payload, raw, el = ask(client, model, STABILITY_TEXT)
        if payload is None:
            print(f"   #{i+1} {el}s 非 JSON：{raw[:80]}")
            sigs["<bad_json>"] += 1
            continue
        dsl = payload.get("dsl")
        errs = validate_dsl(dsl) if isinstance(dsl, dict) else ["dsl 非对象"]
        if not errs:
            ok_struct += 1
        p, b = _has_precedence(payload), _has_budget(payload)
        both.append(p and b)
        sig = json.dumps(dsl, ensure_ascii=False, sort_keys=True)
        sigs[sig] += 1
        print(f"   #{i+1} {el}s 校验{'通过' if not errs else '失败'} 顺序={p} 预算={b} "
              f"sig={hash(sig) % 10000}{'' if i == 0 else ''}")
    print(f"\n   结构合法 {ok_struct}/{repeat}；两项诉求都对 {sum(both)}/{repeat}；"
          f"不同输出形状 {len(sigs)} 种（1 种=完全确定）")

    print("\n" + "=" * 76)
    print("B. 未见改写：同一语义 6 种说法，是否都给出「顺序 + 300万」")
    hits = 0
    for text in PARAPHRASES:
        payload, raw, el = ask(client, model, text)
        p, b = _has_precedence(payload), _has_budget(payload)
        hits += bool(p and b)
        print(f"   {'✓' if p and b else '✗'} {el}s 顺序={p} 预算={b}  {text}")
        if not (p and b):
            print(f"        实际：{_brief(payload)[:200]}")
    print(f"\n   6 种说法全对 {hits}/6")

    print("\n" + "=" * 76)
    print("C. 噪声长句：剥出真诉求")
    payload, raw, el = ask(client, model, NOISY)
    print(f"   {el}s route={(payload or {}).get('route')}")
    print(f"   顺序={_has_precedence(payload)} 预算={_has_budget(payload)}")
    print(f"   实际：{_brief(payload)[:300]}")
    print(f"   unresolved={(payload or {}).get('unresolved')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
