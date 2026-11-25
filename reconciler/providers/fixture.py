"""离线 provider：从本地 JSON 读「云端现状」。

存在的意义：M0 阶段手边还没有云凭证，但 `diff → risk → render` 这条链路
必须先跑通、先有测试。用一份冻结的 JSON 当「云端」，等价于把云端打了个快照。

JSON 结构与 YAML 期望状态完全一致（同一个 `SecurityGroup` 模型），
所以「快照」和「目标」可以逐字段对照，不会出现两套 schema 打架。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import SecurityGroup


class FixtureProvider:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def describe_security_group(self, sg_id: str) -> SecurityGroup:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{self._path} 的内容不是一个 JSON 对象")
        return SecurityGroup.model_validate(raw)


__all__ = ["FixtureProvider"]
