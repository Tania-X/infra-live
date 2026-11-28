#!/usr/bin/env python3
"""模型：一条规则的规范形、一个规则集、以及两者的比对。

三个词：

    Rule     一条规则 = 五个身份字段 + 一个说明
    Ruleset  一组规则 = 有序列，列表顺序就是优先级
    diff     两份 Ruleset 的差 = 新增 / 删除 / 修改 / 顺序变化

为什么必须有"规范形"：

    同一个意思可以有很多写法（22 / 22/22、裸 IP / 带掩码、-1/-1 / 1/65535）。
    不先收成一种写法，比对就会把"没变"读成"变了"。

规范形由 `dump` 定义：能被 `dump` 原样写出来的，才叫规范。
"""

from __future__ import annotations

import ipaddress
import json

# 五个身份字段。它们全同，才算同一条规则。
IDENTITY = ("direction", "protocol", "ports", "peer", "action")

DIRECTIONS = ("ingress", "egress")
PROTOCOLS = ("tcp", "udp", "icmp", "icmpv6", "gre", "all")
ACTIONS = ("accept", "drop")

# 一条规则允许出现的全部字段。多一个就是写错了，不猜。
FIELDS = set(IDENTITY) | {"note"}

MAX_NOTE = 64


# ---------------------------------------------------------------- 归一化

def parse_ports(value: object) -> str:
    """把各种端口写法收成两种：`any` 或 `起/止`。

        "22" / "22/22"        -> "22/22"
        "-1/-1" / "1/65535"   -> "any"
        "any"                 -> "any"
        "8000/9000"           -> "8000/9000"
    """
    text = str(value).strip().lower()
    if text in ("any", "-1/-1"):
        return "any"

    start_text, _, end_text = text.partition("/")
    try:
        start = int(start_text)
        end = int(end_text) if end_text else start
    except ValueError:
        raise ValueError(f"端口不合法：{value!r}") from None

    if not (1 <= start <= end <= 65535):
        raise ValueError(f"端口不合法（要求 1 <= 起 <= 止 <= 65535）：{value!r}")
    if (start, end) == (1, 65535):
        return "any"
    return f"{start}/{end}"


def parse_peer(value: object) -> str:
    """把网段收成"主机位全 0"的形式；裸 IP 补成 /32 或 /128。"""
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=False))
    except ValueError:
        raise ValueError(f"网段不合法：{value!r}") from None


def parse_rule(raw: object) -> dict:
    """宽松地读一条规则，产出规范形。

    宽松的地方：`direction` / `protocol` / `action` 不写就取默认，
    端口和网段各种写法都能读。
    不宽松的地方：不认识的字段直接报错，不悄悄吞掉。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"一条规则应该是一个映射，收到 {raw!r}")

    unknown = set(raw) - FIELDS
    if unknown:
        raise ValueError(f"不认识的字段：{sorted(unknown)}")
    for required in ("ports", "peer"):
        if required not in raw:
            raise ValueError(f"缺少必填字段 {required}")

    rule = {
        "direction": str(raw.get("direction", "ingress")).strip().lower(),
        "protocol": str(raw.get("protocol", "tcp")).strip().lower(),
        "ports": parse_ports(raw["ports"]),
        "peer": parse_peer(raw["peer"]),
        "action": str(raw.get("action", "accept")).strip().lower(),
        "note": str(raw.get("note", "")).strip(),
    }

    if rule["direction"] not in DIRECTIONS:
        raise ValueError(f"direction 只能是 {DIRECTIONS}，收到 {rule['direction']!r}")
    if rule["protocol"] not in PROTOCOLS:
        raise ValueError(f"protocol 只能是 {PROTOCOLS}，收到 {rule['protocol']!r}")
    if rule["action"] not in ACTIONS:
        raise ValueError(f"action 只能是 {ACTIONS}，收到 {rule['action']!r}")
    if "\n" in rule["note"] or "\r" in rule["note"]:
        raise ValueError("note 不能换行")
    if len(rule["note"]) > MAX_NOTE:
        raise ValueError(f"note 最多 {MAX_NOTE} 个字符")

    return rule


def parse_ruleset(raw: object) -> dict:
    """读一个安全组。`rules` 必须存在，不能靠空列表蒙混过去。"""
    if not isinstance(raw, dict):
        raise ValueError("一个安全组应该是一个映射")

    if "rules" not in raw:
        raise ValueError("缺少 rules 字段")
    if not isinstance(raw["rules"], list):
        raise ValueError("rules 应该是一个列表")

    rules = [parse_rule(item) for item in raw["rules"]]

    seen: set[tuple] = set()
    for rule in rules:
        key = identity(rule)
        if key in seen:
            raise ValueError(f"规则重复（五个字段全同）：{key}")
        seen.add(key)

    return {"name": str(raw.get("name", "")), "rules": rules}


def identity(rule: dict) -> tuple:
    """一条规则的身份：五个字段。`note` 不算。"""
    return tuple(rule[field] for field in IDENTITY)


def dump(ruleset: dict) -> str:
    """把规则集写成规范形的 YAML 文本。

    这是"规范"的唯一定义：`fmt` 写出来的就是这个样子，
    CI 只要检查"再 dump 一次有没有变化"就够了。
    """
    lines = [f"name: {ruleset['name']}", "rules:"]
    for rule in ruleset["rules"]:
        lines.append(f"  - direction: {rule['direction']}")
        lines.append(f"    protocol: {rule['protocol']}")
        lines.append(f"    ports: {rule['ports']}")
        lines.append(f"    peer: {rule['peer']}")
        lines.append(f"    action: {rule['action']}")
        if rule["note"]:
            lines.append(f"    note: {json.dumps(rule['note'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 比对

def _slot(rule: dict) -> tuple:
    """配对用的键：同方向 + 同协议 + 同端口 + 同地址族。

    它们相同，才允许把一条"删除"和一条"新增"读成"同一条规则改了一下"。
    地址族（v4/v6）也放进来，是因为 v4 换成 v6 本来就是两件事，硬配对会骗人。
    """
    version = ipaddress.ip_network(rule["peer"], strict=False).version
    return (rule["direction"], rule["protocol"], rule["ports"], version)


def diff(desired: dict, actual: dict, *, order_matters: bool = True) -> list[dict]:
    """算出"要达到期望，云上还差什么"。

    四种结果：

        {"kind": "add",     "rule": ..., "position": 0}
        {"kind": "remove",  "rule": ..., "position": 0}
        {"kind": "modify",  "before": ..., "after": ...}
        {"kind": "reorder", "before": [...], "after": [...]}

    `modify` 是配对出来的，不是云上真有"修改"这个动作——安全组规则本质是
    集合，改一条永远是"删一条 + 加一条"。配成 `modify` 只是为了让人一眼看懂。

    配对只发生在同一个 slot 内。配不上的，还是老老实实算新增/删除。

    `order_matters=True` 时，两边共有规则的相对顺序变了，也算一处变更。
    顺序只在 accept 和 drop 重叠时才有实际影响，但它确实是变更，所以默认不忽略。
    """
    want = desired["rules"]
    have = actual["rules"]

    want_ids = [identity(rule) for rule in want]
    have_ids = [identity(rule) for rule in have]
    want_set = set(want_ids)
    have_set = set(have_ids)

    added = [(pos, rule) for pos, rule in enumerate(want) if identity(rule) not in have_set]
    removed = [(pos, rule) for pos, rule in enumerate(have) if identity(rule) not in want_set]

    # 同 slot 的删除先排好队，等着被新增认领
    pool: dict[tuple, list[tuple[int, dict]]] = {}
    for pos, rule in removed:
        pool.setdefault(_slot(rule), []).append((pos, rule))

    changes: list[dict] = []
    paired_before: set[tuple] = set()
    paired_after: set[tuple] = set()
    for _, rule in added:
        bucket = pool.get(_slot(rule))
        if not bucket:
            continue
        _, before = bucket.pop(0)
        paired_before.add(identity(before))
        paired_after.add(identity(rule))
        changes.append({"kind": "modify", "before": before, "after": rule})

    changes += [
        {"kind": "add", "rule": rule, "position": pos}
        for pos, rule in added
        if identity(rule) not in paired_after
    ]
    changes += [
        {"kind": "remove", "rule": rule, "position": pos}
        for pos, rule in removed
        if identity(rule) not in paired_before
    ]

    if order_matters:
        # 只看两边共有的规则：插入一条不该让它后面的规则都算"动了"
        want_common = [rule for rule in want if identity(rule) in have_set]
        have_common = [rule for rule in have if identity(rule) in want_set]
        if [identity(r) for r in want_common] != [identity(r) for r in have_common]:
            changes.append({"kind": "reorder", "before": have_common, "after": want_common})

    return changes
