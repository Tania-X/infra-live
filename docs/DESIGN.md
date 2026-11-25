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

- 字段白名单固定（上表 8 个字段），未知字段直接校验失败。
- `description` 限长 64，禁止换行。
- `port_range`、`source_cidr`、`direction`、`protocol`、`action` 全部用枚举 / 正则校验。
- 同一 `(direction, protocol, port_range, source_cidr)` 视为同一条规则的唯一键，用于 diff 对齐。

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
      "op": "add",
      "key": "ingress/tcp/443/203.0.113.0/24",
      "after": { "...": "..." },
      "risk": "medium"
    },
    {
      "op": "remove",
      "key": "ingress/tcp/443/0.0.0.0/0",
      "before": { "...": "..." },
      "risk": "high"
    }
  ],
  "summary": "1 条新增，1 条删除",
  "risk": "high",
  "reversible": true
}
```

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
│       │   └── web-sg.yaml          # 安全组期望状态
│       └── inventory.yaml           # 只读盘点（实例列表快照，不参与执行）
├── reconciler/
│   ├── main.py                      # CLI: plan / apply
│   ├── models.py                    # Pydantic: schema + 校验
│   ├── providers/
│   │   └── aliyun.py                # ECS OpenAPI 封装（describe / mutate）
│   ├── diff.py                      # 期望 vs 实际 → Diff
│   ├── risk.py                      # 规则引擎判风险（不依赖模型）
│   └── render.py                    # Diff → Markdown（贴 PR 评论）
├── proposals/                       # 每次提案的留档（JSON + 原话）
│   └── .gitkeep
├── .github/
│   └── workflows/
│       ├── plan.yml                 # pull_request：只算 diff
│       └── apply.yml                # push main：算 diff 并执行
├── docs/
│   └── DESIGN.md                    # 本文档
├── .gitignore
└── README.md
```

职责说明：

| 模块 | 职责 | 不该做的事 |
|---|---|---|
| `models.py` | 把 YAML/JSON 校验成强类型对象 | 不碰云 API |
| `providers/aliyun.py` | 唯一与阿里云通信的地方 | 不做业务判断 |
| `diff.py` | 纯函数：期望 + 实际 → Diff | 不发请求 |
| `risk.py` | 纯函数：Diff → 风险等级 | 不调用模型 |
| `render.py` | Diff → 人话 Markdown | 不做决策 |
| `main.py` | 编排：读 → 校验 → diff → 输出/执行 | 不内联业务规则 |

**`diff.py` / `risk.py` / `render.py` 都是纯函数**，意味着可以单测覆盖，不需要连云端。这是整个项目最值得写好测试的部分。

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

### M0（先跑通「只读 + diff」，不接模型）

- [ ] `models.py` + `diff.py` + `risk.py` + `render.py` + 单测
- [ ] `providers/aliyun.py`：`describe_security_group` 只读
- [ ] CLI：`python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml`
- [ ] 用手改 YAML → 跑 plan → 看到人话 diff（这一步不依赖 GitHub）
- **验收**：能对着真实安全组，算出「多一条 / 少一条 / 改一条」

### M1（接 Agent + GitHub）

- [ ] `aiforge` 增加 `mode="propose"` 与 `ProposalDraft`
- [ ] 只读工具：`list_security_groups` / `describe_security_group` / `list_instances`
- [ ] 提案工具：`propose_security_group_rules`
- [ ] GitHub：建分支 + commit + 开 PR + 贴 diff 评论
- [ ] `plan.yml` / `apply.yml`
- **验收**：说一句话 → 生成 PR → merge → 安全组真的变了 → PR 有执行回写

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
