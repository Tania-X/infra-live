#!/usr/bin/env python3
"""v1 —— 用 model.py 的规范形，比对「期望」和「现状」。

三个概念：

    规则 = 方向 / 协议 / 端口 / 对端 / 动作（+ 一句说明）
    期望 = envs/*.yaml      人写的、Git 存的、以后 AI 也只写这个
    现状 = current.json     假装是从云上读回来的（还不连云）

这个文件只做两件事：判风险、打印。比对和归一化都在 model.py。

用法：
    python plan.py                          # 默认 envs/web-sg.yaml
    python plan.py envs/web-sg-redis.yaml   # 反面样例，返回 2
"""

from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path

import yaml

import model

# 对全网开放就拉响警报的端口（管理面 + 数据面）
DANGER_PORTS = {22, 3389, 3306, 5432, 6379, 9200, 11211, 27017}

WORLD = {"0.0.0.0/0", "::/0"}

LEVELS = ["低", "中", "高", "禁止合并"]
EXIT_CODE = {"低": 0, "中": 0, "高": 1, "禁止合并": 2}


def port_span(rule: dict) -> tuple[int, int]:
    """"any" -> 全部端口；"8000/9000" -> (8000, 9000)。"""
    if rule["ports"] == "any":
        return 1, 65535
    start, end = rule["ports"].split("/")
    return int(start), int(end)


def describe(rule: dict) -> str:
    return (
        f'{rule["direction"]:7} {rule["protocol"]:4} '
        f'{rule["ports"]:>9} ← {rule["peer"]:>18}  {rule["action"]}'
    )


def describe_modify(before: dict, after: dict) -> str:
    """一行写清"从什么改成什么"：哪个变了写哪个。"""
    line = (
        f'{after["direction"]:7} {after["protocol"]:4} {after["ports"]:>9} ← '
        f'{before["peer"]}'
    )
    if before["peer"] != after["peer"]:
        line += f' → {after["peer"]}'
    if before["action"] != after["action"]:
        line += f'  {before["action"]} → {after["action"]}'
    else:
        line += f'  {after["action"]}'
    return line


def peer_relation(before: str, after: str) -> str:
    """对端是收窄了、扩大了，还是换到了不相干的网段。"""
    old = ipaddress.ip_network(before)
    new = ipaddress.ip_network(after)
    if old == new:
        return "same"
    if old.version != new.version:
        return "other"
    if new.subnet_of(old):
        return "narrower"
    if old.subnet_of(new):
        return "wider"
    return "other"


def judge_modify(before: dict, after: dict) -> tuple[str, str]:
    """一条规则被改之后，判它属于哪一档。

    顺序很重要：先看红线（改完仍然是对全网放行管理端口），
    再看动作（拒绝改放行最危险），最后看网段是收窄还是扩大。
    """
    if after["action"] == "accept" and after["peer"] in WORLD:
        start, end = port_span(after)
        if any(start <= port <= end for port in DANGER_PORTS):
            return "禁止合并", f'{after["ports"]} 对全网（{after["peer"]}）开放，且是管理端口'

    parts: list[tuple[str, str]] = []

    if before["action"] != after["action"]:
        if after["action"] == "accept":
            parts.append(("高", "动作由拒绝改为放行"))
        else:
            parts.append(("低", "动作由放行改为拒绝"))

    relation = peer_relation(before["peer"], after["peer"])
    if relation == "narrower":
        parts.append(("低", f'对端收窄：{before["peer"]} → {after["peer"]}'))
    elif relation == "wider":
        parts.append(("高", f'对端扩大：{before["peer"]} → {after["peer"]}'))
    elif relation == "other":
        parts.append(("中", f'对端更换：{before["peer"]} → {after["peer"]}'))

    if not parts:
        return "中", f'{after["ports"]} 规则属性变化'

    level = max((part[0] for part in parts), key=LEVELS.index)
    return level, "；".join(part[1] for part in parts)


def judge(changes: list[dict]) -> tuple[str, list[str]]:
    """五条判定规则。多了就是噪音，所以先只留五条。"""
    level = "低"
    findings: list[str] = []

    def bump(new: str, why: str) -> None:
        nonlocal level
        if LEVELS.index(new) > LEVELS.index(level):
            level = new
        findings.append(f"[{new}] {why}")

    for change in changes:
        kind = change["kind"]

        if kind == "remove":
            rule = change["rule"]
            bump("中", f'删除 {rule["ports"]} ← {rule["peer"]}，可能中断访问')
            continue

        if kind == "reorder":
            bump("中", "规则顺序变化，只在 accept/drop 重叠时才有实际影响")
            continue

        if kind == "modify":
            bump(*judge_modify(change["before"], change["after"]))
            continue

        rule = change["rule"]
        if rule["action"] == "drop":
            bump("低", f'新增拒绝规则 {rule["ports"]} ← {rule["peer"]}')
            continue

        public = rule["peer"] in WORLD
        start, end = port_span(rule)
        danger = any(start <= port <= end for port in DANGER_PORTS)

        if public and danger:
            bump("禁止合并", f'{rule["ports"]} 对全网（{rule["peer"]}）开放，且是管理端口')
        elif public:
            bump("高", f'{rule["ports"]} 对全网（{rule["peer"]}）开放')
        else:
            bump("中", f'新增放行 {rule["ports"]} ← {rule["peer"]}')

    return level, findings


def short(rule: dict) -> str:
    return f'{rule["ports"]} ← {rule["peer"]}'


def render(
    spec: dict,
    current: dict,
    paths: tuple[str, str],
    changes: list[dict],
    level: str,
    findings: list[str],
) -> str:
    spec_path, current_path = paths
    lines = [
        f'期望：{spec["name"]}（{len(spec["rules"])} 条规则）  文件 {spec_path}',
        f'现状：{current["name"]}（{len(current["rules"])} 条规则）  文件 {current_path}',
        "",
        "无变更" if not changes else f"本次变更 {len(changes)} 处",
    ]

    modifies = [c for c in changes if c["kind"] == "modify"]
    adds = [c for c in changes if c["kind"] == "add"]
    removes = [c for c in changes if c["kind"] == "remove"]
    reorders = [c for c in changes if c["kind"] == "reorder"]

    if modifies:
        lines += [""] + [f"  ~ {describe_modify(c['before'], c['after'])}" for c in modifies]
    if adds:
        lines += [""] + [f"  + {describe(c['rule'])}" for c in adds]
    if removes:
        lines += [""] + [f"  - {describe(c['rule'])}" for c in removes]
    if reorders:
        lines.append("")
        for change in reorders:
            lines.append("  ↕ 顺序变化")
            lines.append("      旧：" + " / ".join(short(r) for r in change["before"]))
            lines.append("      新：" + " / ".join(short(r) for r in change["after"]))

    lines += ["", f"风险：{level}"]
    lines += [f"  · {finding}" for finding in findings]
    lines += ["", f"退出码 {EXIT_CODE[level]}（0=可合并，1=需人工确认，2=禁止合并）"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    spec_path = argv[1] if len(argv) > 1 else "envs/web-sg.yaml"
    current_path = argv[2] if len(argv) > 2 else "current.json"

    spec = model.parse_ruleset(yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")))
    current = model.parse_ruleset(json.loads(Path(current_path).read_text(encoding="utf-8")))

    changes = model.diff(spec, current)
    level, findings = judge(changes)
    print(render(spec, current, (spec_path, current_path), changes, level, findings))
    return EXIT_CODE[level]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
