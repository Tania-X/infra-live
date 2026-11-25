# infra-live 领域模型 v0.1

> 面向「大中型企业内网防火墙策略统一管理」的 DDD 建模。
> 状态：已定稿，作为 L0 编码的输入。日期：2026-11-26。

---

## 0. 这份文档要解决什么

`DESIGN.md` 解决的是**「变更怎么安全地落下去」**（提案 → PR → CI → 回滚）。
本文档解决的是**「落下去的到底是什么」**——如果领域模型是错的，再安全的管道也只是把错误的东西安全地推上线。

具体要回答三个问题：

1. 企业里「防火墙规则」到底包含哪些东西？（大多数人只想到安全组）
2. 这些东西怎么用一套领域语言统一表达，又不丢各自的语义？
3. 哪些规则必须写死在模型里，哪些应该可配置，哪些只能审计？

---

## 1. 前提：防火墙不是一层，是五层

企业里规则失控，根源是把这五层当成一件事管。

| 层 | 典型载体 | 粒度 | 语义能力 | 通常谁在管 |
|---|---|---|---|---|
| **互联网边界（南北向）** | 云防火墙、WAF、DMZ 硬件 FW | VPC 边界 / 域名 | 七层：URL、地理、威胁情报、CC 防护 | 安全团队 |
| **VPC 间（南北向）** | 对等连接、云企业网、TR 防火墙 | 网络实例 | 只有地址段 + 端口 | 网络团队 |
| **子网 / 实例（东西向）** | **安全组**、网络 ACL | 实例 / 子网 | 有状态；allow/deny；双向分离 | 业务 + 平台 |
| **主机** | iptables / firewalld / Windows FW | 进程 / 端口 | 进程级，最细但最难治理 | 运维 |
| **容器** | K8s NetworkPolicy、服务网格 | Pod 标签 | 标签选择器，声明式 | 平台团队 |

**关键结论：层与层之间的差异不是「谁能拦」，而是「同一条策略能不能被表达」。**

例子：「只允许 `app=payment` 访问 `db.prod:5432`」

- 安全组：能表达（前提是 Pod 有固定 IP / ENI）
- NetworkPolicy：能表达（标签选择器）
- 云防火墙：**不能表达**，它只认 IP 和域名

所以统一的对象必须是**意图（Intent）**，不是规则。规则是意图在某个载体上的投影。

### 1.1 本项目的范围

| 项 | 决定 |
|---|---|
| 建模范围 | 多载体（五层都建模，含 `TrafficPath`） |
| 实现范围 | 只实现阿里云：安全组（读写）+ 云防火墙（只读，后续） |
| 不做的 | 硬件 FW 具体厂商适配、服务网格、多集群联邦 |

理由见 ADR-001。

---

## 2. 建模主线

### 2.1 统一意图，分离载体

```
业务侧声明：谁 → 谁 → 什么端口 → 允许/拒绝 → 为什么 → 到什么时候
                    │
                    ▼  PolicyCompiler（领域服务）
         每个载体一份 TargetConfig（该载体的规则集）
```

业务同学永远不需要知道「安全组」这个词。他只说「支付服务要访问订单库的 5432」。
翻译是系统的责任，不是人的责任。

### 2.2 TrafficPath：为什么绕不开

一条真实的流量会穿过多个载体：

```
办公网 office
  → [VPN / 专线]
  → 云防火墙（互联网边界）
  → 子网 ACL
  → 目的实例安全组
  → 主机防火墙
```

**任何一跳拒绝就断，任何一跳漏开就不通。** 只盯着安全组，必然出现两种事故：

- 「安全组明明开了，还是连不上」→ 上游 ACL 没开
- 「安全组收窄了，但实际还敞着」→ 云防火墙上还有一条 allow

所以一条 Policy 必须落到**路径上的每一跳**。

### 2.3 TrafficPath 是拓扑推导的，不是人配的

这是本模型最容易做错的地方。如果让每条规则手写路径，规则量会爆炸，且必然不一致。

正确做法：**`Zone` 之间声明 `ZoneLink`，`ZoneLink` 里固定了载体链**。

```python
@dataclass(frozen=True)
class ZoneLink:
    src: ZoneId
    dst: ZoneId
    hops: tuple[CarrierId, ...]     # 这一跳的流量要穿过哪些载体，按顺序
```

编译一条 Policy 时：查 `(src.zone, dst.zone)` 的 `ZoneLink` → 得到载体链 → 逐跳编译。
同一个 zone 内部（两台 ECS 同 VPC），`ZoneLink` 的 `hops` 就只有 `[dst 的安全组]`。

### 2.4 编译而非映射：Capability 与 NotExpressible

每个载体必须声明自己的能力：

```python
@dataclass(frozen=True)
class TargetCapability:
    supports_domain_name: bool      # 云防火墙 True，安全组 False
    supports_tag_selector: bool     # NetworkPolicy True，安全组 False
    supports_expiry: bool           # 绝大多数为 False → 到期回收必须由系统自己做
    supports_deny_rule: bool        # 部分载体只有白名单，没有显式拒绝
    max_rules: int                  # 配额
```

编译时如果策略要求的东西目标表达不了，**必须报 `NotExpressible`，不许静默降级**。

静默降级是企业安全事件最常见的成因：「按域名放行」被悄悄翻成「放行该域名当前解析到的 IP」，域名一换就成了黑洞，或者翻成 `0.0.0.0/0` 就彻底破防。

---

## 3. 六个限界上下文

| # | 上下文 | 聚合根 | 职责 | 不做什么 |
|---|---|---|---|---|
| 1 | **Zone & Asset** | `SecurityZone` | 安全域边界、资产归属、标签、拓扑 | 不判断能不能访问 |
| 2 | **Access Policy** | `AccessPolicy` | 声明访问意图与正当性 | 不碰任何云 API |
| 3 | **Policy Realization** | `TargetConfig` | 编译、冲突检测、能力校验 | 不决定审批 |
| 4 | **Change & Release** | `ChangeRequest` | 审批分级、窗口、执行、回滚 | 不改语义 |
| 5 | **Compliance** | `Baseline` / `Exemption` / `Finding` | 基线检查、豁免、漂移 | 不直接改配置 |
| 6 | **Emergency** | `EmergencyGrant` | 紧急放行、限时、事后复盘 | 不跳过硬性禁止项 |

第 6 个是多数设计的盲区。真实事故时刻「线上挂了，必须马上开一条」，如果没有合规通道，运维就会**绕过系统直接去控制台改**，这条规则从此没人删。所以应急通道不是可选项，是**防止系统被绕开的必要条件**。

### 3.1 上下文映射

```
[Zone & Asset] ──Endpoint 解析 / ZoneLink 拓扑──► [Access Policy]
                                                        │ PolicySubmitted
                                                        ▼
                                              [Policy Realization]
                                               编译 + 冲突检测
                                                        │ TargetConfig
                                                        ▼
                                              [Change & Release]
                                               PR → plan → merge → apply
                                                        │ Applied / Failed
                                              ┌─────────┴─────────┐
                                              ▼                   ▼
                                        [Compliance]        [Emergency]
                                        漂移 / 基线 / 豁免     break-glass
```

集成方式：

- 上下文之间**只用领域事件**或**明确的 DTO** 通信，不共享聚合内部状态。
- `Access Policy` 不 import 任何 `providers/`。它不知道阿里云存在。
- `Policy Realization` 是唯一知道「安全组」这个词的地方（除了 `providers/`）。

---

## 4. 模型

### 4.1 值对象

值对象的三个要求：**不可变、自校验、按值相等**。Python 里用 `@dataclass(frozen=True)` + `__post_init__` 校验。

| 值对象 | 约束 | 为什么必须是值对象 |
|---|---|---|
| `ZoneId` / `PolicyId` / `RuleId` / `AssetId` | 强类型 ID，非裸字符串 | 防止「把 zone 当 policy 传」这类静默错误 |
| `Endpoint` | `zone` + `selector`，两者都非空 | 端点缺一半就没有意义 |
| `AssetSelector` | 标签表达式，可解析为资产集合 | 相等性按表达式判，不按解析结果（否则资产增减会改变「同一条规则」的身份） |
| `PortSpec` | 归一化；`any` 需显式标记 | 复用 `models.py` 已验证的归一化逻辑 |
| `Effect` | `ALLOW` / `DENY` | 区分「显式拒绝」与「默认拒绝」，这两者在审计上完全不同 |
| `Expiry` | 绝对时间 + 时区；到期即失效 | 改时间就是新对象，不能原地修改 |
| `Justification` | `ticket` + `owner` + `reason`，三者非空 | 企业里「为什么」比「是什么」重要 |
| `TargetCapability` | 能力声明 | 编译器的输入约束，是「不许静默降级」的依据 |

### 4.2 聚合与不变量

`AccessPolicy` 是聚合根，代表**一个业务系统的完整访问矩阵**。所有结构性约束守在这里。

```python
class AccessPolicy:
    """一个业务系统的访问矩阵。规则只能通过 add_rule / revoke 进入或离开。"""

    def __init__(self, policy_id: PolicyId, app: ApplicationId, rules=()):
        self._id = policy_id
        self._app = app
        self._rules: list[AccessRule] = list(rules)

    def add_rule(self, rule: AccessRule) -> None:
        self._ensure_complete(rule)          # INV-04：owner + ticket
        self._ensure_expiry(rule)            # INV-05：临时规则必须有到期时间
        self._ensure_no_duplicate(rule)      # INV-06：同一五元组不重复
        self._ensure_within_blast_radius(rule)  # INV-02 / INV-03
        self._rules.append(rule)

    def revoke(self, rule_id: RuleId, by: operator, reason: str) -> None:
        # 撤销也要留痕，不能静默删除
        ...
```

不变量清单见 §5.2。

### 4.3 核心模型草图

```python
@dataclass(frozen=True)
class Endpoint:
    zone: ZoneId
    selector: AssetSelector          # app=payment, env=prod


@dataclass(frozen=True)
class PortSpec:
    protocol: Protocol               # tcp / udp / icmp / all
    span: tuple[int, int]            # (5432, 5432)；(-1, -1) 表示 any，需显式标记
    is_any: bool = False             # 只有显式声明才允许 any（INV-03）


@dataclass(frozen=True)
class Justification:
    ticket: str                      # CHG-2026-1126
    owner: str                       # 必须是真实的人，不是团队名
    reason: str


@dataclass(frozen=True)
class AccessRule:
    id: RuleId
    source: Endpoint
    target: Endpoint
    ports: PortSpec
    effect: Effect                   # ALLOW | DENY
    expiry: Expiry | None            # None = 永久
    justification: Justification
    created_at: datetime
```

### 4.4 领域服务

| 服务 | 输入 | 输出 | 职责 |
|---|---|---|---|
| `PolicyCompiler` | `AccessPolicy` + `Topology` + `Capability[]` | `CompileResult` | 逐跳编译；不可表达时报 `NotExpressible` |
| `ConflictDetector` | `TargetConfig` | `list[Conflict]` | 重叠、遮蔽、allow/deny 打架 |
| `RiskAssessor` | `AccessRule` + 变更类型 | `RiskLevel` + `ApprovalLevel` | 审批分级（对应现有 `risk.py` 的升级版） |
| `ExpiryReaper` | 到期规则集合 | `Revocation 事件` | 定时回收，**这是临时规则真正生效的前提** |
| `DriftDetector` | 期望 `TargetConfig` + 云端实际 | `list[Finding]` | 发现绕过系统的直接修改 |

`ExpiryReaper` 值得单独说：绝大多数云载体**不支持规则级到期**（`supports_expiry=False`）。所以「临时放行 2 小时」不是靠云平台实现，是靠**系统定时扫描 + 主动撤销**。没有这个服务，临时规则 100% 会变成永久规则——这是企业规则债的第一大来源。

### 4.5 仓储与防腐层

```python
class PolicyRepository(Protocol):
    def get(self, policy_id: PolicyId) -> AccessPolicy: ...
    def save(self, policy: AccessPolicy) -> None: ...

class TargetStateRepository(Protocol):
    """某个载体的期望状态（Git 里的 TargetConfig）。"""
    def load(self, carrier: CarrierId) -> TargetConfig: ...

class CloudAdapter(Protocol):
    """防腐层：把领域语言翻译成某家云的 API 调用。"""
    def read(self, carrier: CarrierId) -> TargetConfig: ...
    def write(self, config: TargetConfig) -> ApplyResult: ...
```

**`CloudAdapter` 是唯一允许出现云厂商概念的地方。** 领域层里不该出现 `security_group`、`vpc`、`ram` 这些词。

### 4.6 Python 里怎么落 DDD

Java 靠模块边界，Python 靠包边界 + import 规则：

```
domain/                  # 纯领域：只有标准库 + pydantic，无 IO
  zones.py
  policy.py
  realization.py
application/             # 用例编排：事务边界、事件发布
  submit_policy.py
  apply_change.py
infrastructure/          # 适配器：云 SDK、Git、数据库
  adapters/aliyun.py
  repositories/sqlite.py
```

三条硬规则：

1. `domain/` **不许** import `infrastructure/`，也不许 `import requests`。
2. 跨上下文只通过 DTO 或事件，不互相 import 聚合。
3. 现有 `reconciler/models.py` 的 `Rule` **原样保留**，它是 `TargetConfig` 层的值对象，正好是领域模型的最底层投影。不用重写，只需要在它上面加一层 `Endpoint` / `AccessRule`。

---

## 5. 业务规则的三层

把所有规则写成一个大 if-else 是这类系统最常见的失败方式。分三层：

| 层 | 性质 | 违反后果 | 放在哪 |
|---|---|---|---|
| **不变式 INV** | 永远不能违反 | 是 bug，直接拒绝 + 报警 | 聚合方法里，代码硬编码 |
| **策略 POL** | 可配置，随组织演进 | 需要审批或例外 | 策略引擎 / 配置文件 |
| **基线 BL** | 可审计、可豁免但留痕 | 产出 Finding，不阻断 | Compliance 上下文 |
| **应急 EMG** | 特殊通道 | 事后强制复盘 | Emergency 上下文 |

### 5.1 不变式（INV）

| ID | 规则 | 理由 |
|---|---|---|
| INV-01 | 默认拒绝：没有显式 ALLOW 的流量一律不通 | 白名单模型是所有安全体系的地基 |
| INV-02 | 管理端口不得对 world / 办公网直连，必须经堡垒机 | 22/3389/DB/中间件是勒索软件首选入口 |
| INV-03 | 禁止 any 端口（`1-65535`），除非带豁免记录 | any 端口等于没配 |
| INV-04 | 每条规则必须有 `ticket` + `owner` | 没有主的规则永远没人敢删 |
| INV-05 | 临时规则必须有 `expiry` | 否则「临时」必然变「永久」 |
| INV-06 | 同一聚合内，同一五元组不得重复 | 重复规则会让人误判「已经开过了」 |
| INV-07 | 生产 Zone 的变更不得 direct 写，必须走提案 | 这是 GitOps 的底线 |

INV 全部在 `AccessPolicy.add_rule()` 与 `ChangeRequest` 构造时校验，违反抛 `DomainInvariantViolated`。

### 5.2 策略（POL）

| ID | 规则 | 默认值 |
|---|---|---|
| POL-01 | 生产只接受来自 DMZ / 负载均衡 / 应用层，不接受办公网直连 | 强制 |
| POL-02 | 测试 ↔ 生产双向隔离 | 强制 |
| POL-03 | 东西向默认不通，跨业务系统调用必须显式声明依赖 | 强制 |
| POL-04 | 出站（egress）与入站同等管理 | 强制（多数企业只做 ingress，这是数据外泄主通道） |
| POL-05 | 端口尽量具体，按协议 + 端口 + 源收敛到最小集合 | 推荐 |
| POL-06 | 生产变更限定在变更窗口内 | 可配置窗口 |
| POL-07 | 审批分级（见 §5.5） | 见下表 |
| POL-08 | 规则命名必须包含业务系统 + 用途，禁止「test」「临时」这类无信息命名 | 强制 |

### 5.3 合规基线（BL）

| ID | 基线 | 检测方式 |
|---|---|---|
| BL-01 | 规则年龄超过 N 年未复核 | 按 `created_at` + 最近复核时间 |
| BL-02 | 零命中规则（长期无流量） | 需流量日志；无日志时降级为「无法评估」而不是「通过」 |
| BL-03 | 配置漂移（云端实际 ≠ Git 期望） | 定期 re-describe 比对 |
| BL-04 | 影子规则（被前面规则完全遮蔽，永不生效） | 规则集静态分析 |
| BL-05 | 冲突规则（同一五元组同时 ALLOW 和 DENY） | 规则集静态分析 |

BL 只产出 Finding，不直接改配置。处理 Finding 要走正常的变更流程。

### 5.4 应急（EMG）

| ID | 规则 | 要求 |
|---|---|---|
| EMG-01 | Break-glass 放行 | 必须留痕、限时（默认 ≤ 4h）、事后 24h 内补审批；即使 INV-02 也允许临时突破，但必须双人确认 + 自动到期 |
| EMG-02 | 一次性授权 | 按人 / 按 IP / 按小时，用完即失效，不可续期 |

EMG-01 是唯一允许突破 INV-02 的通道——**但必须承认「有通道」比「假装没有」安全**。没有合规通道，人就会绕过系统去控制台改，结果连留痕都没有。

### 5.5 审批分级（POL-07 的具体映射）

| 变更 | 风险 | 审批 | 对应现有 `risk.py` |
|---|---|---|---|
| 新增 DENY 规则 | 低 | 自动 | low |
| 收窄现有 ALLOW（源网段变小） | 低 | 自动 / 单人 | low |
| 新增窄网段 ALLOW（非管理端口） | 中 | 单人 | medium |
| 新增 ALLOW 到办公网（非管理端口） | 中 | 单人 + 安全组知会 | medium |
| 扩大源网段 | 高 | 双人 | high |
| 删除 ALLOW 规则 | 高 | 双人 | high |
| 涉及管理端口 | 高 | 双人 + 安全团队 | high |
| 管理端口对 world | 禁止 | 拒绝 | **blocked** |

这张表就是 `risk.py` 的展开版。现有实现只覆盖了「安全组单跳」，升级后要加上「跨 Zone」和「载体能力不足」两类判定。

---

## 6. 领域事件与生命周期

### 6.1 事件

| 事件 | 触发 | 消费方 |
|---|---|---|
| `PolicySubmitted` | 业务提交策略变更 | Realization（编译）、Change（开 PR） |
| `PolicyApproved` | 审批通过 | Change（允许 merge） |
| `RulesCompiled` | 编译成功 | Change（写 TargetConfig） |
| `RulesApplied` | apply 成功 | Compliance（更新期望快照） |
| `ApplyFailed` | apply 失败 | Change（回滚标记）、告警 |
| `DriftDetected` | 漂移检出 | Compliance（产 Finding）、通知 owner |
| `ExemptionExpired` | 豁免到期 | Policy（自动撤销规则） |
| `EmergencyGranted` | break-glass 开启 | Compliance（挂复盘任务） |
| `RuleRevoked` | 规则撤销 | Compliance（更新台账） |

### 6.2 一条规则的可追溯链

```
用户原话 → 意图 → AccessRule → Policy → 编译产物 → PR → commit → apply 日志
   ↑                                                        │
   └──────────── 任意一条生效规则都能反查到这里 ──────────────┘
```

企业里最贵的不是「加规则」，是**「没人敢删规则」**——因为不知道它为什么存在。
如果系统能对任意一条云上规则回答「哪条策略、哪个工单、谁批的、什么时候到期」，它就已经超过大多数商业产品。

这也是 `proposals/` 留档和 `Justification` 值对象存在的意义。

---

## 7. 与 GitOps 的衔接

### 7.1 意图进 Git，产物不进

| 内容 | 进 Git？ | 理由 |
|---|---|---|
| `policies/<app>/access.yaml`（业务意图） | **是** | 人 review 的对象应该是策略，不是几百条规则 |
| 编译出的规则集（`TargetConfig`） | 否（存 audit 分支 / 对象存储） | 企业量级下几千条，全进主分支会让人 review 不动 |

「review 策略，不 review 产物」是这个模型能规模化前提。产物只用于执行和取证。

### 7.2 目录建议

```
policies/
  payment/
    access.yaml            # 意图
  order/
    access.yaml
zones/
  topology.yaml            # ZoneLink 载体链
baselines/
  prod-isolation.yaml
```

### 7.3 apply 流程不变

编译产物仍然走现有的 `plan → PR → merge → apply`。区别只在于：

- `plan` 多了「编译」这一步（策略 → 规则集）
- diff 发生在**规则集层**，不是策略层
- PR 评论里同时给出「策略变更」和「规则变更」两层视图

---

## 8. 落地分级

| 阶段 | 范围 | 验收 |
|---|---|---|
| **L0** | `AccessPolicy` + `PolicyCompiler`（只阿里云安全组）+ 现有 GitOps 链路 | 一句话 → 策略 YAML → 编译成安全组规则 → PR → apply |
| **L1** | 加 `Zone & Asset`（含 `ZoneLink`）+ 审批分级 | 跨 Zone 的策略能编译到多跳；高风险双人审批 |
| **L2** | 加 `Compliance` + `Emergency` + `ExpiryReaper` | 漂移能发现；临时规则能自动回收；break-glass 有复盘 |
| **L3** | 加云防火墙 / NetworkPolicy 等第二载体 + `TrafficPath` 完整化 | 同一条策略落到两种载体，能力不足时报 `NotExpressible` |

**关键原则：L0 就按 L3 的形状建模。** 代价是多写两三个值对象（`Endpoint`、`Capability`、`ZoneLink`），收益是后面不用重写。

也明确**不做**的：一次上六个上下文的完整实现。DDD 最大的风险是过度设计，单人项目尤其如此。

---

## 9. 已拍板的决策（ADR 摘要）

### ADR-001 多载体建模，单实现落地

- **状态**：已采纳（2026-11-26）
- **背景**：企业内网有 5 层防火墙载体，只按安全组建模会在第二个载体出现时整体重写。
- **决策**：领域层建模全部五层，实现只做阿里云安全组（后续加云防火墙）。
- **后果**：多出 `Endpoint` / `TargetCapability` / `ZoneLink` 三个概念；换载体时只需新增 adapter + capability，领域层不动。

### ADR-002 策略按「业务系统」划分，不按部门

- **状态**：已采纳（2026-11-26）
- **背景**：按部门划分会导致一个系统的规则散落在多个部门，无人对整体负责。
- **决策**：`AccessPolicy` 的聚合边界是 `ApplicationId`；部门 / 团队只作为审批与权限维度。
- **后果**：一个业务系统的访问矩阵是完整、可 review 的；跨系统调用必须显式声明，天然暴露隐式依赖。

### ADR-003 做临时放行与到期回收（L2）

- **状态**：已采纳（2026-11-26）
- **背景**：`supports_expiry=False` 是绝大多数载体的现实，靠云平台无法实现「临时」。
- **决策**：由 `ExpiryReaper` 定时扫描 + 主动撤销，配合 `Expiry` 值对象与 `ExemptionExpired` 事件。
- **后果**：需要常驻调度任务（GitHub Actions schedule 或独立 cron）；换来的是「临时规则真的会消失」，这是企业规则债的第一大来源。

---

## 10. 与现有 M0 代码的关系

| 现有 | 归属 | 处理 |
|---|---|---|
| `models.py` 的 `Rule` | `TargetConfig` 层值对象 | **保留**，是领域模型最底层的投影 |
| `models.py` 的 `SecurityGroup` | `TargetConfig` | 保留，后续可泛化为 `CarrierConfig` |
| `diff.py` / `risk.py` / `render.py` | 规则集层的纯函数 | 保留；`risk.py` 在 L1 升级为审批分级 |
| `providers/aliyun.py` | `infrastructure/adapters` | 迁入 `infrastructure/`，逻辑不变 |
| `main.py` | `application` 用例 | 保留 CLI，后续加 `compile` 子命令 |

**不需要推倒重来。** 现有的 `Rule` 恰好就是「载体层」的正确抽象，领域层是加在它上面的，不是替换它。

---

## 11. 还悬着的问题

1. **Zone 怎么划分**：按 VPC 划，还是按「环境 + 网络位置」划（office / dmz / prod / test / mgmt）？建议后者，因为跨 VPC 的业务系统很常见。
2. **`AssetSelector` 怎么解析**：标签体系由谁维护（云上 tag / 独立的资产台账 / 两者同步）？
3. **`ZoneLink` 谁维护**：网络团队手工录入，还是从云上拓扑自动发现？L0 建议手工，L1 再考虑自动。
4. **流量日志从哪来**：BL-02（零命中）依赖它；没有日志时基线要诚实标注「无法评估」，而不是假通过。
