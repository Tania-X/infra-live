# infra-live 设计草案 v0.1

> 一个「用自然语言驱动、经 Git 审批、由 CI 执行」的云资源运维系统。
> 首发环境：dev（阿里云 ECS / 安全组）。技术栈：Python + React。

---

## 0. 一句话定位

**把「一句话」编译成「一份可 review、可回滚的云资源变更」。**

对话是输入端，Git 是审批与审计的载体，CI 是唯一有执行权的一方，AI 永远只能「提议」。

---

## 1. 目标与非目标

### 目标（M1 范围内）

1. 用户用自然语言提问，系统能**只读**地回答云资源现状（安全组规则、ECS 概况）。
2. 用户提出变更，系统生成**结构化提案**（意图 JSON + 机器算出的 diff + 风险等级），而不是直接执行。
3. 提案以 **PR** 形式落到 Git 仓库，PR 里能看到「改什么、从什么改成什么、风险多高」。
4. 只有 **merge 到 main** 才触发执行；执行结果回写到 PR。
5. 回滚 = revert commit + 重新执行。

### 非目标（这一版明确不做）

- 不做任意云资源类型（先只做安全组，最多加 1~2 个只读查询）。
- 不做「聊天里点一下就执行」的按钮（会绕开审批，等于废掉 GitOps）。
- 不做删除实例、改 IAM、改 VPC 这类高爆破坏力操作。
- 不做多环境（dev/prod 双套）与多租户。
- 不引入 Terraform / Pulumi（见 §6.1）。

---

## 2. 三层模型：为什么必须分开

```
┌─────────────────────────────────────────────────────────┐
│  意图层（LLM 产出，可错，可重来）                          │
│  { resource, field, from, to, reason }                   │
└───────────────────────┬─────────────────────────────────┘
                        │ 确定性代码渲染 + schema 校验
┌───────────────────────▼─────────────────────────────────┐
│  提案层（Git 承载，人审批）                                │
│  YAML 期望状态 + 系统算出的 diff + 风险等级 + PR          │
└───────────────────────┬─────────────────────────────────┘
                        │ merge 触发
┌───────────────────────▼─────────────────────────────────┐
│  执行层（CI 运行，无 AI，确定性）                          │
│  reconciler: 读云端 → 对比 → 只改差异 → 回写              │
└─────────────────────────────────────────────────────────┘
```

三层的职责边界是这个项目成立的前提：

- **LLM 只填字段，不写代码、不执行。** 它的输出被 JSON Schema 约束，出错就渲染失败、直接拒绝。
- **`from` 值不由模型给，由系统实时读取云端填入。** 模型编造「当前值」是最常见的幻觉来源。
- **diff 由系统算，不是模型写。** 模型给的是「想要什么」，系统算的是「实际差多少」，两者不能混。
- **执行权只在 CI。** Agent 进程里不持有任何写权限的云凭证。

---

## 3. 三条链路

### 3.1 只读问答（不进 Git）

```
用户: "web-sg 这个安全组现在开了哪些端口？"
  → Agent 选工具 describe_security_group(sg_id)   [mode=read]
  → 调阿里云 ECS OpenAPI
  → 结果回灌 → 模型组织成人话
```

只读工具可自由调用，无需审批，因为**没有副作用**。这是分流标准：有没有写操作。

### 3.2 变更提案（进 Git）

```
用户: "把 443 只允许公司网段 203.0.113.0/24 访问"
  → Agent 产出意图 JSON                    [mode=propose]
  → 系统读云端当前规则，填 from
  → 渲染成 envs/dev/security-groups/web-sg.yaml 的新版本
  → 算 diff + 判风险
  → 建分支 + commit + 开 PR
  → Agent 回复用户: "已生成提案 #12，风险：中，[查看]"
```

**关键：`mode=propose` 的工具不执行任何写操作，只返回一个 ProposalDraft。** Agent 在结构上就没有执行能力。

### 3.3 执行与回写

```
PR 打开 → Actions 跑 plan（只读凭证）→ 贴 diff 评论
  → 人 review → merge 到 main
  → Actions 跑 apply（写凭证）→ 执行差异 → 贴结果评论 → 更新 Proposal 状态
  → 校验：执行后再读一次云端，确认已收敛
```

回滚：`git revert <commit>` → 新的 PR → merge → apply 把状态改回去。**Git 历史就是变更历史，不需要自建回滚机制。**

---

## 4. 数据模型

### 4.1 期望状态 YAML（人对齐的「唯一事实」）

```yaml
# envs/dev/security-groups/web-sg.yaml
kind: security_group
id: sg-bp1xxxxxxxx
name: web-sg
region: cn-hangzhou
rules:
  - direction: ingress
    protocol: tcp
    port_range: "443/443"
    source_cidr: 203.0.113.0/24
    action: accept
    priority: 1
    description: "HTTPS，仅公司出口"
  - direction: ingress
    protocol: tcp
    port_range: "22/22"
    source_cidr: 198.51.100.7/32
    action: accept
    priority: 2
    description: "SSH，仅固定 IP"
```

约束：

- 字段白名单固定为 8 个：`direction` / `protocol` / `port_range` / `source_cidr` / `action` / `priority` / `description` / `ip_version`（默认 `ipv4`，YAML 里通常省略）。未知字段直接校验失败。
- `description` 限长 64，禁止换行（防 PR 评论 Markdown 注入）。
- `port_range`、`source_cidr`、`direction`、`protocol`、`action` 全部用枚举 / 正则校验。
- **写进模型前先归一化**：`443` → `443/443`；`198.51.100.7` → `198.51.100.7/32`；`203.0.113.7/24` → `203.0.113.0/24`；`-1/-1` 表示全部端口。不做归一化会让同一语义产生假变更。
- 同一 `(direction, protocol, port_range, source_cidr, ip_version)` 视为同一条规则的唯一键，用于 diff 对齐；重复键直接校验失败。

### 4.2 意图 Intent（LLM 的唯一合法输出）

```json
{
  "resource_type": "security_group",
  "resource_id": "sg-bp1xxxxxxxx",
  "changes": [
    {
      "field": "rules",
      "op": "add",
      "value": {
        "direction": "ingress",
        "protocol": "tcp",
        "port_range": "443/443",
        "source_cidr": "203.0.113.0/24",
        "action": "accept",
        "description": "HTTPS，仅公司出口"
      }
    },
    {
      "field": "rules",
      "op": "remove",
      "match": { "port_range": "443/443", "source_cidr": "0.0.0.0/0" }
    }
  ],
  "reason": "用户要求将 443 访问范围收窄到公司网段"
}
```

校验失败（字段不认识、枚举越界、资源 ID 不存在）→ 直接告诉用户「无法生成提案」并说明原因，**不重试、不猜**。

### 4.3 Diff（系统算出来的，不是模型写的）

```json
{
  "resource": "security_group:sg-bp1xxxxxxxx",
  "changes": [
    {
      "op": "replace",
      "key": "ingress/tcp/443/203.0.113.0/24",
      "before": { "source_cidr": "0.0.0.0/0", "...": "..." },
      "after": { "source_cidr": "203.0.113.0/24", "...": "..." }
    },
    {
      "op": "add",
      "key": "ingress/tcp/8080/10.0.0.0/8",
      "before": null,
      "after": { "...": "..." }
    }
  ],
  "summary": "1 处修改，1 处新增"
}
```

`op` 只有三种：`add` / `remove` / `replace`。

- 安全组规则本质是集合，所谓「修改」其实是删一条加一条。
- 但给人看的时候 `443 从 0.0.0.0/0 改成 203.0.113.0/24` 比「一删一加」清楚得多，
  所以 `diff.py` 会把**同方向 + 同协议 + 同端口**的删除与新增配对成一条 `replace`。
- `risk` 不写在 Diff 里，由 `risk.py` 单独产出（见 4.5）。拆开是为了让 diff 保持「事实」，
  风险是「判断」，两者混在一起以后改规则就要重算 diff。

### 4.4 Proposal 状态机

```
draft ──► pr_open ──► planned ──► approved ──► applying ──► applied
             │            │                       │
             └────────────┴───────────────────────┴──► rejected / failed
```

- `rejected`：人关闭 PR
- `failed`：apply 报错，需人工处理后重试
- `applied` 之后只能通过新提案（revert）修改，**不允许原地改已应用的提案**

---

## 5. 目录结构

```
infra-live/
├── envs/
│   └── dev/
│       ├── security-groups/
│       │   ├── web-sg.yaml          # 安全组期望状态（正常场景）
│       │   └── web-sg-risk-demo.yaml # 反面样本：触发 blocked
│       └── inventory.yaml           # 只读盘点（实例列表快照，不参与执行）
├── fixtures/
│   └── web-sg.current.json          # 云端快照，离线当「云端」用
├── reconciler/
│   ├── main.py                      # CLI: plan（apply 待 M1）
│   ├── models.py                    # Pydantic: schema + 白名单 + 归一化
│   ├── diff.py                      # 纯函数：期望 vs 实际 → Diff
│   ├── risk.py                      # 纯函数：Diff → 风险等级
│   ├── render.py                    # 纯函数：Diff → Markdown
│   └── providers/
│       ├── base.py                  # Provider 协议（只有 describe 一个方法）
│       ├── fixture.py               # 离线：读 JSON 快照
│       └── aliyun.py                # 在线：ECS OpenAPI（只读已写，写待 M1）
├── tests/                           # 纯函数单测（48 条，无网络依赖）
├── proposals/                       # 每次提案的留档（JSON + 原话）
│   └── .gitkeep
├── .github/
│   └── workflows/
│       ├── plan.yml                 # pull_request：只算 diff
│       └── apply.yml                # push main：算 diff 并执行
├── docs/
│   └── DESIGN.md                    # 本文档
├── pyproject.toml
├── .gitignore
└── README.md
```

职责说明：

| 模块 | 职责 | 不该做的事 |
|---|---|---|
| `models.py` | 把 YAML/JSON 校验成强类型对象、归一化 | 不碰云 API |
| `providers/` | 唯一与云通信的地方 | 不做业务判断 |
| `diff.py` | 纯函数：期望 + 实际 → Diff | 不发请求 |
| `risk.py` | 纯函数：Diff → 风险等级 | 不调用模型 |
| `render.py` | 纯函数：Diff → 人话 Markdown | 不做决策 |
| `main.py` | 编排：读 → 校验 → diff → 输出/执行 | 不内联业务规则 |

**`diff.py` / `risk.py` / `render.py` 都是纯函数**，意味着可以单测覆盖，不需要连云端。
这是整个项目最值得写好测试的部分，M0 的 48 条单测全部落在这三个模块和 `models.py` 上。

---

## 6. 关键决策与取舍

### 6.1 为什么 MVP 不用 Terraform

| 维度 | Terraform | 自写 reconciler |
|---|---|---|
| 学习成本 | 要学 HCL + state 概念 | Python，已有能力 |
| state 管理 | CI 里要持久化（OSS/S3），泄露有风险 | **无状态**，天然幂等 |
| 适配阿里云 | `alicloud` provider 成熟 | 直接用官方 SDK |
| diff 呈现 | 现成、规范 | 要自己写（但可完全定制成人话） |
| 扩展性 | 强，生态大 | 每加资源类型要自己写逻辑 |

选择后者的理由：**只有 1 个资源类型**，state 管理成本 > 收益；而且我们要的 diff 是「给人看的风险摘要」，不是 Terraform 那种资源级 JSON。

**何时切换**：资源类型 > 5 种、需要多环境多账号、或有第二个人参与维护。届时 YAML 意图层不变，只换执行器。

### 6.2 为什么 apply 只能从 main 触发

- `pull_request` 事件的 workflow **能拿到 secrets**（同仓库分支 PR），意味着 PR 作者可以通过修改 `reconciler/` 代码来窃取云凭证。
- 因此 **plan 阶段只 checkout `main` 的代码**，PR 分支只提供 YAML 数据：

```yaml
- uses: actions/checkout@v4
  with: { ref: main }                       # 可信代码
- run: |
    git fetch origin pull/${{ github.event.number }}/head:pr
    git show pr:envs/dev/security-groups/web-sg.yaml > /tmp/target.yaml
- run: python -m reconciler.main plan --file /tmp/target.yaml   # 只读凭证
```

- `apply` 只在 `push: branches: [main]` 时运行，使用写凭证。
- `.github/` 与 `reconciler/` 纳入 CODEOWNERS，改这两处的 PR 必须你本人批。

### 6.3 为什么风险等级由规则引擎判，不由模型判

模型说「这是低风险」并不可靠。风险来自**变更的语义**，规则引擎能确定性判定：

| 变更 | 风险 |
|---|---|
| 新增规则，源网段比原来更窄 | low |
| 新增放行规则（新开端口 / 新网段） | medium |
| 删除规则 | medium（可能中断访问） |
| 放行 `0.0.0.0/0` 到管理端口（22/3389/6379…） | **high，禁止** |
| 修改 SSH / 数据库端口规则 | high，需二次确认 |

模型只能在提案里写 `reason`，**不得输出 `risk` 字段**（schema 里直接不给这个字段）。

### 6.4 为什么要有 `proposals/` 留档

Git 记的是「结果」，但排查时要看「当初为什么这么改」。每次提案落一个 JSON：

```json
{
  "id": "p-20261124-001",
  "requester": "max",
  "utterance": "把 443 只允许公司网段访问",
  "intent": { "...": "..." },
  "diff": { "...": "..." },
  "risk": "high",
  "pr": 12,
  "created_at": "2026-11-24T10:00:00+08:00"
}
```

这样「用户原话 → 模型意图 → 机器 diff → 实际执行」四者可以逐层对照，出问题能定位到哪一层。

---

## 7. 安全设计（六道闸）

1. **最小权限**：RAM 子账号，只授 ECS 安全组读 + 改；主账号 AK 永不出现在 CI。
2. **凭证隔离**：只读凭证给 plan，写凭证只给 apply（不同 Secrets、不同 workflow）。
3. **代码与数据分离**：PR 只能改 `envs/**/*.yaml`，改不了 `reconciler/`（CODEOWNERS + plan 只跑 main 代码）。
4. **字段白名单**：YAML 与 Intent 都过 schema，未知字段拒绝；`description` 禁换行防注入。
5. **爆炸半径**：禁止 delete / IAM / VPC；`0.0.0.0/0` 到管理端口直接拒绝，不是"警告"而是"拒绝"。
6. **审计闭环**：PR 评论 = 意图 + diff + apply 日志；`proposals/` 留档；执行后再读一次云端做收敛校验。

---

## 8. 前端（React）

三个视图，不做第四个：

| 视图 | 内容 | 数据来源 |
|---|---|---|
| **对话** | 流式回答；变更时内嵌「提案卡片」（改什么/风险/[查看 PR]） | `POST /api/chat` (SSE) |
| **提案详情** | 时间线：draft → PR → plan diff → merge → apply → verified | `GET /api/proposals/{id}` |
| **资源浏览** | 安全组列表 + 规则只读表格 | `GET /api/resources/security-groups` |

技术：Vite + TS + TanStack Query + Tailwind + shadcn/ui。
状态：对话流式用 SSE；提案状态由后端轮询 GitHub（或 webhook）。

**UI 上不提供「直接执行」按钮**，只有「查看 PR」——审批动作必须在 GitHub 完成，避免两套审批入口。

---

## 9. 里程碑

### M0 ✅ 已完成（先跑通「只读 + diff」，不接模型）

- [x] `models.py` + `diff.py` + `risk.py` + `render.py`（纯函数）
- [x] 单测 48 条，无网络依赖（`pytest` 0.4s 跑完）
- [x] `providers/fixture.py`：离线快照 provider
- [x] `providers/aliyun.py`：`describe_security_group` 只读（**已写，未联调**）
- [x] CLI：`python -m reconciler.main plan --file ... --current ... | --live`
- [x] 退出码 0 / 1 / 2 供 CI 当门禁
- **验收（离线部分）**：手改 YAML → 跑 plan → 看到人话 diff，能区分「新增 / 删除 / 修改」三种形态；对 Redis 全网开放能判 blocked 并 exit 2
- **验收（在线部分）**：待用户提供安全组 ID 与凭证后联调

### M1（接 Agent + GitHub）

- [ ] `aiforge` 增加 `mode="propose"` 与 `ProposalDraft`
- [ ] 只读工具：`list_security_groups` / `describe_security_group` / `list_instances`
- [ ] 提案工具：`propose_security_group_rules`
- [ ] GitHub：建分支 + commit + 开 PR + 贴 diff 评论
- [ ] `plan.yml` / `apply.yml`
- **验收**：说一句话 → 生成 PR → merge → 安全组真的变了 → PR 有执行回写

### M1.5（RAG：把「模糊指代」落到确定资源）

- [ ] 资源卡片入库（安全组 / 实例的 id、名称、标签、用途摘要）
- [ ] 知识库入库（安全基线、变更 SOP、故障手册），带 `effective_from` / `status`
- [ ] 变更历史自动入库（每次 merge 后写一条「问题 → 意图 → 结果」）
- [ ] 检索路由：精确 ID → 结构化查询；模糊指代 → 卡片向量 + 用户确认；规范 → 文档 RAG
- [ ] 评测集 30~50 条，指标只看「资源命中率」和「文档 Recall@3」
- **前置条件**：M1 已跑通，且至少有一批真实变更历史

### M2（前端 + 收敛校验）

- [ ] FastAPI：`/api/chat`(SSE) / `/api/proposals` / `/api/resources`
- [ ] React 三视图
- [ ] apply 后自动 re-describe 校验，写回 `verified` 状态

---

## 10. 待定（需要一起拍板）

1. **写操作边界**：M1 只允许「新增/修改规则」，还是也允许「删除规则」？（建议：允许删除，但删除走 high 风险 + 人工 merge）
2. **管理端口清单**：22/3389/6379/3306/27017 是否都算「禁止对 0.0.0.0/0 放行」？
3. **仓库位置**：当前在 `C:\Users\Administrator\Projects\infra-live`，是否合适。
4. **前端时机**：M2 再动，还是先把 API + CLI 打磨好？
5. **ECS 现状**：拿到安全组后，`web-sg.yaml` 以现状为初始值（即「零 diff」起步）。

---

## 11. Agent 平台与 RAG（参考公司技术栈）

### 11.1 那套栈对应到本项目的位置

| 技术 | 是什么 | 对应本项目 |
|---|---|---|
| **Agno** | Python Agent 框架（Agent / Team / Workflow，Apache 2.0） | `aiforge` 的 `loop.py` + `registry.py` + `memory.py` |
| **AgentOS** | Agno 的 FastAPI 运行时 + Control Plane UI + session 存储 + HITL | FastAPI + 会话存储 |
| **Langfuse** | LLM 可观测：trace / 成本 / prompt 版本 / 数据集 / 评测，可自托管 | 目前**完全缺失**的一层 |
| **Next.js** | React 框架（SSR / 路由） | §8 的前端 |
| **Docker** | 部署形态 | CI runner 的固定环境 |

**关键判断：Agno + AgentOS 是「Agent 平台」，不是「GitOps 执行器」。**

它的 approval / pause 发生在应用内，确认之后由服务端自己拿凭证执行。
本项目要求的是：审批 = merge，执行权只在 CI，回滚 = revert。
两者不冲突，但不是一回事。

### 11.2 接入方式

```
Agno / AgentOS   →  对话 + 工具调用 + 会话 + 观测
GitOps 层（本仓库） →  提案 + PR + CI 执行 + 回滚
连接点：propose 工具，Agent 只拿到 proposal_id，拿不到执行权
```

1. **先单 Agent，不用 Team / Workflow。** 只有一个 Agent、几个工具时，引入 Team 只会拉长链路、增加调试难度。等真出现「一句话要同时查安全组和 ECS 并交叉判断」再上 Workflow。
2. **两级确认**：Agno 的 pause 只用于「轻确认」（参数歧义、目标不明确，如「改 web-sg 还是 app-sg」）；真正的授权走 Git PR。「轻确认解决歧义，重审批解决授权」，不要用同一套机制承担两件事。
3. **Langfuse 必加，优先级高于前端。** 每次对话一个 trace，每个工具调用一个 span，metadata 带 `proposal_id` / `pr_number`，可以从 Langfuse 直接跳 PR。
   - **必须定脱敏策略**：trace 里会有实例 ID、内网 IP、安全组规则。要么自托管，要么写入前打码。
4. **存储分离**：`agentos.*`（session / memory，可丢）与 `infra.*`（proposal / 审计，不可丢）分库或分 schema。混在一起后换框架或清会话会连带搞乱审计记录。
5. **锁定版本**：Agno 迭代快（v2 有破坏性改动），`pyproject.toml` 用精确版本，不用 `>=`。
6. **Docker 只用在 CI runner**：本地开发不需要。真正需要的是让 `reconciler` 跑在固定镜像里，避免「本地 py3.12 正常、runner 上 py3.13 挂了」。

### 11.3 RAG：先分清它解决哪个问题

四类用途，优先级不同：

| # | 用途 | 解决什么 | 优先级 |
|---|---|---|---|
| 1 | **资源语义检索**（实体链接） | 「跑官网的那台」「昨天新建的安全组」→ 精确 `resource_id` | **最高** |
| 2 | **运维知识库** | 「我们的安全基线是什么」「变更走什么流程」 | 高 |
| 3 | **变更历史检索** | 「上次类似的改动怎么做的」「这个端口为什么开着」 | 中 |
| 4 | 云 API / 错误码检索 | Agent 自己排错 | 低（可后置） |

**用途 1 才是真痛点。** GitOps 要求精确 ID，而用户永远说「官网那个安全组」。
这一步「模糊描述 → 确定资源」是 RAG 该干的活，也是本项目跟「文档问答机」的分界线。

做法：每个资源一张「资源卡片」，同时保留结构化字段和一段语义摘要：

```yaml
id: sg-bp1xxxxxxxx
type: security_group
name: web-sg
region: cn-hangzhou
tags: {env: dev, app: official-site, owner: max}
summary: "官网对外服务的安全组，开放 80/443，绑定 2 台 ECS"
```

检索时**双路**：

- 用户给 **ID / 精确名称** → 结构化精确查询，**不要走向量**；
- 用户给 **语义描述** → 卡片向量检索，取 top-3，**回显「ID + 名称 + 用途」让用户确认**。

> 选错资源比改错配置更可怕，所以候选必须人工确认，不能让模型自己挑一个就往下走。

**知识库要带时间维度。** 运维规范会变，2023 版说允许、2025 版说禁止，两版都在库里就是随机答案。文档 metadata 必须有 `effective_from` / `supersedes` / `status: active|deprecated`，检索时过滤 `active`，回答强制带出处。

**变更历史可以自动构建。** 每次 PR merge 后写一条「问题 → 意图 → 结果 → PR 号」进库。天然增量、天然有版本、天然可信，还正好回答最高频的「上次这事怎么弄的」。

### 11.4 检索路由（不要什么都向量检索）

```
用户 query
   ↓ 意图分类（规则优先，别用大模型）
├─ 含 ID / 精确名称 ──────► 结构化精确查询
├─ 模糊指代资源 ──────────► 资源卡片向量检索 + 用户确认
├─ 问规范 / 流程 ─────────► 文档 RAG（active 过滤 + 强制引用）
├─ 问历史 ────────────────► PR / 提案历史 RAG
└─ 要变更 ────────────────► 走 propose 链路
```

**红线：RAG 结果只能作依据，绝不能直接生成变更意图。** 否则文档里一句「临时可以放开 0.0.0.0/0」就可能被模型当成授权，绕过 `risk.py`。

### 11.5 选型（尽量少加组件）

| 环节 | 选择 | 理由 |
|---|---|---|
| 向量库 | **pgvector** | 本来就要 Postgres 存 proposal，一个库解决 |
| Embedding | **bge-m3** 或阿里云百炼 | 中文好；bge-m3 同时出稠密 + 稀疏向量 |
| 切分 | Markdown 按标题切，300~600 字，overlap 10% | 手册类文档结构清晰 |
| 混合检索 | pgvector + PG 全文检索（`tsvector`），RRF 融合 | 资源名 / 错误码需要精确匹配 |
| Rerank | 先不做 | 库小的时候收益不明显 |
| 框架 | 先用 **Agno 内置 Knowledge** | 已封装 reader + vector store + search，接口不变，不够用再换 |

**评测集**（30~50 条）是唯一能证明 RAG 变好还是变坏的东西：

```yaml
- question: "官网那个安全组开了哪些端口"
  expect_resource: sg-bp1xxxxxxxx
  expect_docs: [security-baseline-v2.md#3.2]
```

指标只看两个：**资源命中率**、**文档 Recall@3**，再加一个「最终答案是否引用了正确来源」。
没有评测集时，改一次 chunk 大小、换一次 embedding 完全无法判断好坏。
