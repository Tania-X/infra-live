"""数据模型：期望状态（YAML）与云端实际状态（API 返回）共用一套强类型。

这个文件只做三件事：

1. **字段白名单**：YAML 里出现模型没定义的字段，直接校验失败（extra="forbid"）。
   这是防止「模型/用户往配置里塞奇怪东西」的第一道闸。
2. **归一化**：把「写法不同但语义相同」的值收敛成唯一表示。
   `443` 与 `443/443` 必须相同，`203.0.113.7/24` 与 `203.0.113.0/24` 必须相同，
   否则 diff 会把这些当成两条不同规则，产生假变更。
3. **不碰网络**：这里没有一次 HTTP 调用，纯校验，可以放心单测。

规则的对齐键（唯一键）是 `(direction, protocol, port_range, source_cidr, ip_version)`，
其中 port_range 与 source_cidr 都已归一化，所以字符串比较即可。
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Direction = Literal["ingress", "egress"]
Protocol = Literal["tcp", "udp", "icmp", "icmpv6", "gre", "all"]
Action = Literal["accept", "drop"]
IpVersion = Literal["ipv4", "ipv6"]

#: 规则允许出现的字段全集。多一个字段就是校验失败，不接受「先存着以后再支持」。
RULE_FIELDS: tuple[str, ...] = (
    "direction",
    "protocol",
    "port_range",
    "source_cidr",
    "action",
    "priority",
    "description",
    "ip_version",
)

#: 对公网开放即视为高危的端口（管理面 + 数据面）。
#: 这不是「建议名单」，是 `risk.py` 里 blocked 判定的硬边界。
MANAGED_PORTS: tuple[int, ...] = (22, 3389, 3306, 5432, 6379, 9200, 11211, 27017)

_PORT_RE = re.compile(r"^(\d{1,5})(?:/(\d{1,5}))?$")
_SG_ID_RE = re.compile(r"^sg-[a-z0-9]{4,64}$")


def parse_port_range(value: str) -> tuple[int, int]:
    """解析端口区间，返回 `(start, end)`；`end == -1` 表示全部端口。

    - `"443"`       -> `(443, 443)`
    - `"8000/9000"` -> `(8000, 9000)`
    - `"-1/-1"`     -> `(1, -1)`   阿里云用 `-1/-1` 表示「所有端口」

    统一在这里解析，`models`、`risk` 都不用各自写一遍正则。
    """
    if value == "-1/-1":
        return (1, -1)
    m = _PORT_RE.match(value)
    if not m:
        raise ValueError(
            f"port_range 格式应为 '443' / '443/443' / '8000/9000' / '-1/-1'，收到 {value!r}"
        )
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if not (1 <= start <= 65535 and 1 <= end <= 65535):
        raise ValueError(f"端口越界（合法范围 1~65535）：{value!r}")
    if start > end:
        raise ValueError(f"端口区间起点大于终点：{value!r}")
    return (start, end)


class Rule(BaseModel):
    """一条安全组规则。frozen，因为它是「某一时刻的事实快照」。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: Direction
    protocol: Protocol
    port_range: str
    source_cidr: str
    action: Action = "accept"
    priority: int = Field(default=1, ge=1, le=100)
    description: str = Field(default="", max_length=64)
    ip_version: IpVersion = "ipv4"

    @model_validator(mode="before")
    @classmethod
    def _infer_ip_version(cls, data: object) -> object:
        """没显式写 ip_version 时，从 cidr 推断，避免 IPv6 地址被默认值误判为 v4。"""
        if isinstance(data, dict) and "ip_version" not in data:
            cidr = data.get("source_cidr")
            if isinstance(cidr, str) and ":" in cidr:
                return {**data, "ip_version": "ipv6"}
        return data

    @field_validator("port_range", mode="before")
    @classmethod
    def _normalize_port_range(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("port_range 必须是字符串，例如 '443/443'")
        start, end = parse_port_range(value.strip())
        return "-1/-1" if end == -1 else f"{start}/{end}"

    @field_validator("source_cidr", mode="before")
    @classmethod
    def _normalize_cidr(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("source_cidr 必须是字符串，例如 '203.0.113.0/24'")
        try:
            net = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"source_cidr 不是合法网段：{value!r}") from exc
        return str(net)

    @model_validator(mode="after")
    def _check_ip_version_matches_cidr(self) -> "Rule":
        net = ipaddress.ip_network(self.source_cidr, strict=False)
        expected = "ipv4" if net.version == 4 else "ipv6"
        if self.ip_version != expected:
            raise ValueError(
                f"ip_version={self.ip_version} 与 source_cidr={self.source_cidr} 不一致"
            )
        return self

    @field_validator("description")
    @classmethod
    def _single_line(cls, value: str) -> str:
        # 换行会破坏 PR 评论的 Markdown 结构，也给「注释注入」留了口子
        if "\n" in value or "\r" in value:
            raise ValueError("description 不得包含换行")
        return value.strip()

    @property
    def key(self) -> str:
        """用于 diff 的唯一键，四个分量都已归一化。"""
        return f"{self.direction}/{self.protocol}/{self.port_range}/{self.source_cidr}"

    @property
    def match_key(self) -> tuple[str, str, str, str]:
        """用于「配对成修改」的键：只看端口和方向，不看源网段。"""
        return (self.direction, self.protocol, self.port_range, self.ip_version)

    def port_span(self) -> tuple[int, int]:
        return parse_port_range(self.port_range)


class SecurityGroup(BaseModel):
    """一个安全组的期望状态或实际状态，结构完全相同，只有来源不同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["security_group"] = "security_group"
    id: str = Field(pattern=_SG_ID_RE.pattern)
    name: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1)
    rules: tuple[Rule, ...] = ()

    @field_validator("rules", mode="before")
    @classmethod
    def _freeze_rules(cls, value: object) -> tuple:
        if value is None:
            return ()
        return tuple(value)

    @model_validator(mode="after")
    def _no_duplicate_rules(self) -> "SecurityGroup":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.key in seen:
                raise ValueError(f"规则重复（同一方向/协议/端口/网段只能出现一次）：{rule.key}")
            seen.add(rule.key)
        return self

    def by_key(self) -> dict[str, Rule]:
        return {rule.key: rule for rule in self.rules}


def load_security_group_yaml(path: str | Path) -> SecurityGroup:
    """读 YAML 并校验。只读本地文件，不碰网络。"""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 的内容不是一个 YAML 映射")
    return SecurityGroup.model_validate(raw)
