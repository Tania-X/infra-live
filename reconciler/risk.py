"""风险判定：Diff → 风险等级。

这条链路上**唯一不允许模型参与**的判断。理由很直接：
模型对「0.0.0.0/0 开 22 端口有多危险」没有稳定的判断，而规则引擎每次都给同样的答案。

规则只做三件事：
1. 该拒绝的（blocked）明确拒绝，不是「警告」；
2. 该警惕的（high）标出来；
3. 收紧类的变更（narrower）给 low，别用一堆黄色噪声把人练到麻木。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal

from .diff import Diff, RuleChange
from .models import MANAGED_PORTS, Rule

Level = Literal["low", "medium", "high", "blocked"]

_SEVERITY: dict[Level, int] = {"low": 0, "medium": 1, "high": 2, "blocked": 3}


@dataclass(frozen=True)
class Finding:
    """一条具体的判定结论，和某一条规则变更一一对应。"""

    level: Level
    subject: str
    reason: str


@dataclass(frozen=True)
class RiskReport:
    overall: Level
    findings: tuple[Finding, ...]

    @property
    def blocked(self) -> bool:
        return self.overall == "blocked"

    def of(self, level: Level) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.level == level)


def _is_public(cidr: str) -> bool:
    """是否对全网开放：`0.0.0.0/0` 或 `::/0`（prefixlen == 0）。"""
    return ipaddress.ip_network(cidr, strict=False).prefixlen == 0


def _hits_managed(rule: Rule) -> bool:
    """端口区间是否与任一管理端口相交；`-1/-1` 视为命中。"""
    start, end = rule.port_span()
    if end == -1:
        return True
    return any(start <= port <= end for port in MANAGED_PORTS)


def _trend(before: Rule, after: Rule) -> Literal["narrower", "wider"] | None:
    """源网段是收窄了还是扩大了；无法比较（如 v4→v6）返回 None。"""
    old = ipaddress.ip_network(before.source_cidr, strict=False)
    new = ipaddress.ip_network(after.source_cidr, strict=False)
    if old.version != new.version:
        return None
    if new.subnet_of(old) and not old.subnet_of(new):
        return "narrower"
    if old.subnet_of(new) and not new.subnet_of(old):
        return "wider"
    return None


def _assess_one(change: RuleChange) -> Finding:
    before, after = change.before, change.after

    # 最高优先级：任何让「管理端口对全网开放」的变更，一律拒绝。
    # 放在最前面，是因为它同时覆盖 add 和 replace 两种形态。
    if (
        after is not None
        and after.action == "accept"
        and _is_public(after.source_cidr)
        and _hits_managed(after)
    ):
        return Finding(
            "blocked",
            after.key,
            f"{after.port_range} 对全网（{after.source_cidr}）放行且命中管理端口，禁止合并",
        )

    if change.op == "add":
        assert after is not None
        if after.action == "drop":
            return Finding("low", after.key, "新增拒绝规则，收紧访问")
        if _is_public(after.source_cidr):
            return Finding("high", after.key, f"{after.port_range} 对全网放行（非管理端口，仍需确认）")
        return Finding(
            "medium",
            after.key,
            f"新增放行规则 {after.port_range} ← {after.source_cidr}",
        )

    if change.op == "remove":
        assert before is not None
        if _hits_managed(before):
            return Finding(
                "high",
                before.key,
                f"删除管理端口 {before.port_range} 的规则，可能导致自己失联",
            )
        return Finding("medium", before.key, f"删除规则 {before.port_range} ← {before.source_cidr}")

    # replace：安全组规则没有原地修改，改的是「源网段」或「动作」
    assert before is not None and after is not None
    trend = _trend(before, after)
    if trend == "narrower":
        return Finding(
            "low",
            change.anchor.key,
            f"{after.port_range} 源网段收窄：{before.source_cidr} → {after.source_cidr}",
        )
    if trend == "wider":
        return Finding(
            "high",
            change.anchor.key,
            f"{after.port_range} 源网段扩大：{before.source_cidr} → {after.source_cidr}",
        )
    if before.action != after.action:
        return Finding(
            "high" if after.action == "drop" else "medium",
            change.anchor.key,
            f"{after.port_range} 动作变更：{before.action} → {after.action}",
        )
    if _hits_managed(after):
        return Finding("high", change.anchor.key, f"修改管理端口 {after.port_range} 的规则")
    return Finding("medium", change.anchor.key, f"规则属性变更：{after.port_range}")


def assess(diff: Diff) -> RiskReport:
    """整份 Diff 的风险 = 所有单条判定的最高等级。空 diff 视为 low。"""
    findings = tuple(_assess_one(change) for change in diff.changes)
    overall: Level = "low"
    for finding in findings:
        if _SEVERITY[finding.level] > _SEVERITY[overall]:
            overall = finding.level
    return RiskReport(overall=overall, findings=findings)
