#!/usr/bin/env python3
"""把规则文件写成规范形。

    python fmt.py             # 写回文件
    python fmt.py --check     # 只检查，不规范就返回 1（给 CI 用）

规范由 `model.dump` 定义：解析 → 按规范写回。
人类只负责写"大意"，规范形态永远由工具产出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

import model


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [Path(p) for p in argv[1:] if not p.startswith("--")]
    if not paths:
        paths = sorted(Path("envs").glob("*.yaml"))

    dirty: list[Path] = []
    for path in paths:
        original = path.read_text(encoding="utf-8")
        try:
            canonical = model.dump(model.parse_ruleset(yaml.safe_load(original)))
        except ValueError as exc:
            print(f"✗ {path}：{exc}")
            return 2
        if canonical == original:
            continue

        dirty.append(path)
        if not check:
            path.write_text(canonical, encoding="utf-8")

    if check:
        for path in dirty:
            print(f"✗ {path} 不是规范形，请运行 python fmt.py")
        if dirty:
            return 1
        print(f"✓ {len(paths)} 个文件都是规范形")
        return 0

    for path in dirty:
        print(f"✓ {path} 已规范")
    if not dirty:
        print(f"✓ {len(paths)} 个文件本来就是规范形")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
