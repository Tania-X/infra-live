"""Provider 接口。

`providers/` 是整个项目唯一跟云通信的地方，向上只暴露「把一个安全组的现状读出来」
这一个能力。`diff` / `risk` / `render` 全都不知道阿里云的存在，因此可以离线单测。

接口故意写得很窄：没有分页、没有重试、没有缓存。那些等真的需要时再加，
现在加只会掩盖问题。
"""

from __future__ import annotations

from typing import Protocol

from ..models import SecurityGroup


class SecurityGroupProvider(Protocol):
    """能读出某个安全组当前状态的东西（真实云 / 本地 fixture）。"""

    def describe_security_group(self, sg_id: str) -> SecurityGroup: ...
