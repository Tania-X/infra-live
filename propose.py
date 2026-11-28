#!/usr/bin/env python3
"""propose.py —— 把一句话变成一条规则变更。

分工是这个文件唯一重要的事：

    模型：只填一个 JSON。字段固定，多一个字段就拒绝。
    代码：校验 → 套用到规则集 → 写规范形 YAML → 算 diff → 判风险。

模型不写 YAML，也不碰风险判定。它给的是"想要什么"；
差多少、危不危险，仍然是 plan.py 算的。

用法：
    python propose.py "把 443 只允许 203.0.113.0/24 访问"    # 调模型
    python propose.py --intent intent.json                   # 不调模型，直接套用
    python propose.py "..." --write                          # 写回 envs/web-sg.yaml
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

import model
import plan

DEFAULT_SPEC = "envs/web-sg.yaml"
DEFAULT_CURRENT = "current.json"

# 模型必须输出这个形状。op=set 表示"这个端口组的规则应该是这样"，
# op=remove 表示"这个端口组不要了"。
INTENT_EXAMPLE = {
    "op": "set",
    "rule": {
        "direction": "ingress",
        "protocol": "tcp",
        "ports": "443/443",
        "peer": "203.0.113.0/24",
        "action": "accept",
        "note": "HTTPS，仅公司网段",
    },
}


def system_prompt(ruleset: dict) -> str:
    """把当前规则塞进提示词，让模型知道"现在长什么样"。"""
    return f"""你是一个防火墙变更助手。用户会用一句话提出变更，你只输出一个 JSON，不要输出任何解释。

当前安全组的规则（按优先级顺序）：

{model.dump(ruleset)}

JSON 只允许两种形状：

1) 修改/新增某个端口的规则：
{json.dumps(INTENT_EXAMPLE, ensure_ascii=False, indent=2)}

2) 删除某个端口的全部规则：
{{"op": "remove", "direction": "ingress", "protocol": "tcp", "ports": "8080/8080"}}

规则：
- ports 写成 "443/443" 或 "8000/9000"；全端口写 "any"
- peer 是 CIDR，单个网段
- action 只能是 accept 或 drop
- 不要输出 note 以外的解释文字
- 不确定时，宁可输出 op=remove 也不要猜新网段
"""


def call_model(text: str, ruleset: dict) -> dict:
    """调 DeepSeek。没有 key 就直接失败，不静默降级。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit(
            "缺少 DEEPSEEK_API_KEY。\n"
            "离线调试可以用：python propose.py --intent intent.json"
        )

    payload = json.dumps(
        {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt(ruleset)},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode()

    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    return json.loads(body["choices"][0]["message"]["content"])


def parse_intent(raw: object) -> dict:
    """校验模型的输出。多一个字段、少一个字段，都直接拒绝。"""
    if not isinstance(raw, dict):
        raise ValueError("意图必须是一个 JSON 对象")

    op = raw.get("op")
    if op == "set":
        if set(raw) != {"op", "rule"}:
            raise ValueError(f"op=set 只允许 op 和 rule 两个字段，收到 {sorted(raw)}")
        return {"op": "set", "rule": model.parse_rule(raw["rule"])}

    if op == "remove":
        allowed = {"op", "direction", "protocol", "ports"}
        if not set(raw) <= allowed:
            raise ValueError(f"op=remove 只允许 {sorted(allowed)}，收到 {sorted(raw)}")
        for field in ("direction", "protocol", "ports"):
            if field not in raw:
                raise ValueError(f"op=remove 缺少 {field}")
        return {
            "op": "remove",
            "direction": str(raw["direction"]).strip().lower(),
            "protocol": str(raw["protocol"]).strip().lower(),
            "ports": model.parse_ports(raw["ports"]),
        }

    raise ValueError(f"op 只能是 set 或 remove，收到 {op!r}")


def apply_intent(ruleset: dict, intent: dict) -> dict:
    """把意图套到规则集上。

    定位用的是「端口组」：同方向 + 同协议 + 同端口。
    所以"把 443 改成只允许 X" = 这个端口组里只留一条新规则，
    不会出现"旧的 443 还挂在那儿"的情况。
    """
    rules = list(ruleset["rules"])

    if intent["op"] == "remove":
        target = (intent["direction"], intent["protocol"], intent["ports"])
        return {
            "name": ruleset["name"],
            "rules": [
                r for r in rules
                if (r["direction"], r["protocol"], r["ports"]) != target
            ],
        }

    new_rule = intent["rule"]
    target = (new_rule["direction"], new_rule["protocol"], new_rule["ports"])

    result: list[dict] = []
    placed = False
    for rule in rules:
        if (rule["direction"], rule["protocol"], rule["ports"]) != target:
            result.append(rule)
            continue
        if not placed:
            result.append(new_rule)
            placed = True
    if not placed:
        result.append(new_rule)
    return {"name": ruleset["name"], "rules": result}


def main(argv: list[str]) -> int:
    write = "--write" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    spec_path = DEFAULT_SPEC
    current_path = DEFAULT_CURRENT

    ruleset = model.parse_ruleset(yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")))
    current = model.parse_ruleset(json.loads(Path(current_path).read_text(encoding="utf-8")))

    # --intent 走离线路径：不调模型，直接套用一份 JSON
    if "--intent" in argv:
        raw = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        source = f"文件 {args[0]}"
    else:
        if not args:
            raise SystemExit('用法：python propose.py "把 443 只允许 203.0.113.0/24 访问"')
        raw = call_model(args[0], ruleset)
        source = f"模型（原话：{args[0]}）"

    intent = parse_intent(raw)
    updated = apply_intent(ruleset, intent)

    text = model.dump(updated)
    changes = model.diff(updated, current)
    level, findings = plan.judge(changes)

    print(f"=== 意图来源：{source} ===")
    print(f"=== 意图：{json.dumps(intent, ensure_ascii=False)} ===\n")
    print("=== 规则文件会变成 ===")
    print(text)
    print(plan.render(updated, current, (spec_path, current_path), changes, level, findings))

    if write:
        Path(spec_path).write_text(text, encoding="utf-8")
        print(f"\n已写回 {spec_path}（未提交）")

    return plan.EXIT_CODE[level]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
