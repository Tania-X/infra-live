"""渲染：Diff + 风险 → 给人看的 Markdown。

只做展示，不做决策。所以这里可以「为了好读」做一些合并，
比如把 `replace` 写成一行「源网段：旧 → 新」。

输出直接贴到 PR 评论或终端里，两种场景都能读。
"""

from __future__ import annotations

from .diff import Diff, RuleChange
from .risk import RiskReport

_LEVEL_BADGE = {
    "low": "🟢 LOW",
    "medium": "🟡 MEDIUM",
    "high": "🔴 HIGH",
    "blocked": "⛔ BLOCKED",
}

_OP_LABEL = {"add": "新增", "remove": "删除", "replace": "修改"}


def _cell(value: object) -> str:
    """表格单元格转义：`|` 会破坏列结构，换行会破坏行结构。"""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _row(index: int, change: RuleChange) -> str:
    anchor = change.anchor
    before_src = change.before.source_cidr if change.before else "—"
    after_src = change.after.source_cidr if change.after else "—"
    return (
        f"| {index} | {_OP_LABEL[change.op]} | {anchor.direction} | "
        f"`{_cell(anchor.port_range)}` | `{_cell(before_src)}` | `{_cell(after_src)}` |"
    )


def render_markdown(
    diff: Diff,
    risk: RiskReport,
    *,
    resource_label: str | None = None,
    title: str = "plan 预览",
) -> str:
    lines: list[str] = [f"# 🔍 {title}", ""]

    resource_line = f"**资源**　`{diff.resource}`"
    if resource_label:
        resource_line += f"（{resource_label}）"
    lines += [resource_line, ""]

    if risk.blocked:
        lines += [
            "> ⛔ **禁止合并**：本次变更包含禁止项，`apply` 会直接失败。",
            "",
        ]

    lines += [
        f"**变更**　{diff.summary}",
        "",
        f"**风险**　{_LEVEL_BADGE[risk.overall]}",
        "",
    ]

    if not diff.is_empty:
        lines += [
            "| # | 操作 | 方向 | 端口 | 源（变更前） | 源（变更后） |",
            "|---|---|---|---|---|---|",
        ]
        lines += [_row(i, change) for i, change in enumerate(diff.changes, 1)]
        lines += [""]

    if risk.findings:
        lines += ["### 风险判定", ""]
        lines += [
            f"- {_LEVEL_BADGE[finding.level]}　{finding.reason}" for finding in risk.findings
        ]
        lines += [""]

    lines += ["> diff 由 `reconciler` 确定性计算，非模型生成；审批请在 GitHub 完成。"]
    return "\n".join(lines)
