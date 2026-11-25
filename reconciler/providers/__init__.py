"""Provider 实现。

`AliyunProvider` 故意不在这里 import：它依赖可选的阿里云 SDK，
而离线跑 `plan --current` 的人不应该被一个没装的依赖挡住。
"""

from .base import SecurityGroupProvider
from .fixture import FixtureProvider

__all__ = ["SecurityGroupProvider", "FixtureProvider"]
