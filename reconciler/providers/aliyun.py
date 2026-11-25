"""阿里云 ECS OpenAPI 封装。

**这个文件是拒绝执行的边界。** 只要 `reconciler` 里有人想碰网络，
必须经过这里；反过来，只要这里没有 `authorize` 之类的方法，
Agent 进程里就不可能发生写操作。

M0 只实现只读的 `describe_security_group`。写操作（Authorize / Revoke）留在 M1，
并且只在 CI 的 apply workflow 里调用。

> 状态：M0 未联调。SDK 是可选依赖（`pip install 'infra-live[aliyun]'`），
> 没装时不影响离线 `plan --current`。接入真实凭证后需要对着阿里云文档核对字段名。
"""

from __future__ import annotations

import os

from ..models import Rule, SecurityGroup


class AliyunProvider:
    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region_id: str,
        *,
        endpoint: str | None = None,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region_id = region_id
        self._endpoint = endpoint

    @classmethod
    def from_env(cls) -> "AliyunProvider":
        """从环境变量读凭证。CI 里由 Secrets 注入，本地从不落盘。"""
        access_key_id = os.environ.get("ALIYUN_ACCESS_KEY_ID")
        access_key_secret = os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
        if not access_key_id or not access_key_secret:
            raise RuntimeError(
                "缺少环境变量 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET；"
                "离线调试请改用 plan --current <fixture.json>"
            )
        return cls(
            access_key_id,
            access_key_secret,
            os.environ.get("ALIYUN_REGION_ID", "cn-hangzhou"),
            endpoint=os.environ.get("ALIYUN_ENDPOINT"),
        )

    def _build_client(self):
        try:
            from alibabacloud_ecs20140526.client import Client
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError as exc:
            raise RuntimeError(
                "未安装阿里云 SDK，请执行：pip install 'infra-live[aliyun]'"
            ) from exc

        config = open_api_models.Config(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region_id=self._region_id,
        )
        if self._endpoint:
            config.endpoint = self._endpoint
        return Client(config)

    def describe_security_group(self, sg_id: str) -> SecurityGroup:
        """读一个安全组的 ingress + egress 全部规则。只读，无任何副作用。"""
        try:
            from alibabacloud_ecs20140526 import models as ecs
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装阿里云 SDK，请执行：pip install 'infra-live[aliyun]'"
            ) from exc

        client = self._build_client()

        meta = client.describe_security_groups(
            ecs.DescribeSecurityGroupsRequest(region_id=self._region_id, security_group_ids=[sg_id])
        )
        groups = getattr(getattr(meta.body, "security_groups", None), "security_group", None) or []
        if not groups:
            raise RuntimeError(f"安全组不存在或当前凭证无权读取：{sg_id}")
        name = groups[0].security_group_name or sg_id

        rules: list[Rule] = []
        for direction in ("ingress", "egress"):
            response = client.describe_security_group_attribute(
                ecs.DescribeSecurityGroupAttributeRequest(
                    region_id=self._region_id,
                    security_group_id=sg_id,
                    direction=direction,
                )
            )
            permissions = getattr(getattr(response.body, "permissions", None), "permission", None) or []
            for perm in permissions:
                # egress 方向的网段字段是 dest_cidr_ip，ingress 是 source_cidr_ip
                cidr = perm.source_cidr_ip or perm.dest_cidr_ip or "0.0.0.0/0"
                rules.append(
                    Rule(
                        direction=direction,
                        protocol=(perm.ip_protocol or "all").lower(),
                        port_range=perm.port_range or "-1/-1",
                        source_cidr=cidr,
                        action="accept" if (perm.policy or "").lower() == "accept" else "drop",
                        priority=int(perm.priority or 1),
                        description=(perm.description or "")[:64],
                    )
                )

        return SecurityGroup(
            id=sg_id,
            name=name,
            region=self._region_id,
            rules=tuple(rules),
        )


__all__ = ["AliyunProvider"]
