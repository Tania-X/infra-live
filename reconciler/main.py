"""CLI 入口。

M0 只有 `plan`：读云端现状（或本地 fixture）→ 算 diff → 判风险 → 打出来。

    # 离线：用快照当「云端」，不需要任何凭证
    python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml ^
        --current fixtures/web-sg.current.json

    # 直连阿里云（需要 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET）
    python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml --live

退出码是给 CI 用的：0 正常 / 1 high / 2 blocked。`plan.yml` 直接拿它当门禁。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diff import Diff, compute_diff
from .models import load_security_group_yaml
from .providers.base import SecurityGroupProvider
from .providers.fixture import FixtureProvider
from .render import render_markdown
from .risk import RiskReport, assess

EXIT_OK = 0
EXIT_HIGH = 1
EXIT_BLOCKED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="infra-live 的确定性执行器（无 AI 参与）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="算出期望状态与云端现状的差异（只读）")
    plan.add_argument("--file", required=True, type=Path, help="期望状态 YAML")
    source = plan.add_mutually_exclusive_group()
    source.add_argument("--current", type=Path, help="离线快照 JSON（当「云端」用）")
    source.add_argument("--live", action="store_true", help="直连阿里云读实时状态")
    plan.add_argument("--format", choices=("md", "json"), default="md")
    plan.add_argument("--out", type=Path, help="写入文件而不是打印到终端")

    apply_cmd = sub.add_parser("apply", help="执行差异（M1 实现，M0 只有占位）")
    apply_cmd.add_argument("--file", required=True, type=Path)

    return parser


def _resolve_provider(args: argparse.Namespace) -> SecurityGroupProvider:
    if args.current:
        return FixtureProvider(args.current)
    if args.live:
        from .providers.aliyun import AliyunProvider

        return AliyunProvider.from_env()
    raise SystemExit("必须指定 --current <fixture.json>（离线）或 --live（直连阿里云）")


def _diff_to_dict(diff: Diff) -> dict:
    return {
        "resource": diff.resource,
        "summary": diff.summary,
        "changes": [
            {
                "op": change.op,
                "key": change.anchor.key,
                "before": change.before.model_dump() if change.before else None,
                "after": change.after.model_dump() if change.after else None,
            }
            for change in diff.changes
        ],
    }


def _risk_to_dict(risk: RiskReport) -> dict:
    return {
        "overall": risk.overall,
        "findings": [
            {"level": finding.level, "subject": finding.subject, "reason": finding.reason}
            for finding in risk.findings
        ],
    }


def command_plan(args: argparse.Namespace) -> int:
    desired = load_security_group_yaml(args.file)
    actual = _resolve_provider(args).describe_security_group(desired.id)

    diff = compute_diff(desired, actual)
    risk = assess(diff)

    if args.format == "json":
        payload = _diff_to_dict(diff) | {"risk": _risk_to_dict(risk)}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(
            diff,
            risk,
            resource_label=f"{desired.name} · {desired.region}",
        )

    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)

    if risk.blocked:
        return EXIT_BLOCKED
    if risk.overall == "high":
        return EXIT_HIGH
    return EXIT_OK


def command_apply(args: argparse.Namespace) -> int:
    raise SystemExit("apply 在 M1 实现：只允许由 push:main 触发，且只在此处使用写凭证")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 控制台默认 GBK，含中文和 emoji 的输出会炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = _build_parser().parse_args(argv)
    if args.command == "plan":
        return command_plan(args)
    return command_apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
