"""Diff：期望状态 vs 云端实际状态。

纯函数，不发请求、不读文件、不写文件。输入两个 `SecurityGroup`，输出一个 `Diff`。
这是整个项目最值得写测试的地方——它决定了「PR 里给人看的东西对不对」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Rule, SecurityGroup

Op = Literal["add", "remove", "replace"]

_OP_ORDER: dict[str, int] = {"replace": 0, "add": 1, "remove": 2}


@dataclass(frozen=True)
class RuleChange:
    """一条规则的变更。

    安全组规则本质是集合，所以「修改」其实是「删一条 + 加一条」。
    但给人看的时候，`443 从 0.0.0.0/0 改成 203.0.113.0/24` 显然比
    「删一条/加一条」清楚得多，所以这里把同端口的删+加配对成 `replace`。
    """

    op: Op
    match_key: tuple[str, str, str, str]
    before: Rule | None
    after: Rule | None

    @property
    def anchor(self) -> Rule:
        """代表这条变更的规则对象：有 after 用 after，否则用 before。"""
        rule = self.after or self.before
        assert rule is not None
        return rule


@dataclass(frozen=True)
class Diff:
    resource: str
    changes: tuple[RuleChange, ...]

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def counts(self) -> dict[str, int]:
        result = {"add": 0, "remove": 0, "replace": 0}
        for change in self.changes:
            result[change.op] += 1
        return result

    @property
    def summary(self) -> str:
        if self.is_empty:
            return "无变更"
        counts = self.counts
        parts = []
        if counts["add"]:
            parts.append(f"{counts['add']} 处新增")
        if counts["remove"]:
            parts.append(f"{counts['remove']} 处删除")
        if counts["replace"]:
            parts.append(f"{counts['replace']} 处修改")
        return "，".join(parts)


def compute_diff(desired: SecurityGroup, actual: SecurityGroup) -> Diff:
    """计算「要达到期望状态，云端还差什么」。

    注意方向：返回的变更都是**对云端**的动作。
    `add` = 云端缺这条，要加；`remove` = 云端多这条，要删。
    """
    if desired.kind != actual.kind:
        raise ValueError(f"资源类型不一致：期望 {desired.kind}、实际 {actual.kind}")
    if desired.id != actual.id:
        raise ValueError(f"资源 ID 不一致：期望 {desired.id}、实际 {actual.id}")
    if desired.region != actual.region:
        raise ValueError(f"资源地域不一致：期望 {desired.region}、实际 {actual.region}")

    desired_map = desired.by_key()
    actual_map = actual.by_key()

    added = sorted(
        (rule for key, rule in desired_map.items() if key not in actual_map),
        key=lambda rule: rule.key,
    )
    removed = sorted(
        (rule for key, rule in actual_map.items() if key not in desired_map),
        key=lambda rule: rule.key,
    )

    # 把「同方向 + 同协议 + 同端口」的删除与新增配对成修改
    pool: dict[tuple[str, str, str, str], list[Rule]] = {}
    for rule in removed:
        pool.setdefault(rule.match_key, []).append(rule)

    changes: list[RuleChange] = []
    unmatched_add: list[Rule] = []
    for rule in added:
        bucket = pool.get(rule.match_key)
        if bucket:
            changes.append(RuleChange("replace", rule.match_key, bucket.pop(0), rule))
        else:
            unmatched_add.append(rule)

    unmatched_remove = [rule for bucket in pool.values() for rule in bucket]

    changes.extend(
        RuleChange("add", rule.match_key, None, rule) for rule in unmatched_add
    )
    changes.extend(
        RuleChange("remove", rule.match_key, rule, None) for rule in unmatched_remove
    )

    changes.sort(key=lambda change: (_OP_ORDER[change.op], change.match_key))

    return Diff(resource=f"security_group:{desired.id}", changes=tuple(changes))
