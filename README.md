# infra-live

用自然语言驱动的云资源运维：**一句话 → 提案 → PR → CI 执行 → 回写**。

- 对话是输入端
- Git 是审批与审计载体
- CI 是唯一有执行权的一方
- AI 永远只能「提议」，不能「执行」

两份文档分工：

- [docs/DESIGN.md](docs/DESIGN.md) —— 变更怎么安全落下去（提案 / PR / CI / 回滚）
- [docs/DOMAIN.md](docs/DOMAIN.md) —— 落下去的到底是什么（防火墙五层载体 / 六个限界上下文 / 22 条业务规则 / DDD 建模）

## 当前阶段

`v0.2` — **M0 已完成**：只读 + diff 链路可跑通，有单测，不依赖任何云凭证。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 正常场景：两条规则收窄 → 🟢 LOW，退出码 0
python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml --current fixtures/web-sg.current.json

# 风险场景：Redis 对全网开放 → ⛔ BLOCKED，退出码 2
python -m reconciler.main plan --file envs/dev/security-groups/web-sg-risk-demo.yaml --current fixtures/web-sg.current.json

# 机器可读
python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml --current fixtures/web-sg.current.json --format json

# 直连阿里云（需要 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET / ALIYUN_REGION_ID）
pip install -e ".[aliyun]"
python -m reconciler.main plan --file envs/dev/security-groups/web-sg.yaml --live
```

`--current <json>` 是离线模式：把一份快照当「云端现状」，用来在没有云凭证时开发和写测试。
JSON 与 YAML 期望状态用同一个 `SecurityGroup` 模型，字段完全一致。

## 首发范围（dev）

- 云：阿里云（ECS / 安全组）
- 资源类型：`security_group`（读写），`instance`（只读）
- 「防火墙策略」= 安全组规则（ingress tcp），暂不涉及 WAF 实例

## 结构

```
envs/dev/            期望状态（YAML，人工与 AI 共同维护）
fixtures/            云端快照（离线当「云端」用，仅开发/测试）
reconciler/          无状态收敛器：读云端 → 算 diff → 执行差异
  models.py          强类型 + 字段白名单 + 归一化（不碰网络）
  diff.py            纯函数：期望 vs 实际 → Diff
  risk.py            纯函数：Diff → 风险等级（规则引擎，不调模型）
  render.py          纯函数：Diff → 人话 Markdown
  providers/         唯一与云通信的地方（aliyun.py / fixture.py）
  main.py            CLI：plan（apply 待 M1）
tests/               纯函数单测，48 条
proposals/           每次提案的原话 + 意图 + diff + 风险留档
.github/workflows/   plan（PR，只读） / apply（main，可写）
docs/                设计文档（DESIGN.md）+ 领域模型（DOMAIN.md）
```

## 设计要点

1. **读写分流**：只读查询直接答，变更一律走提案。
2. **模型只产意图**：`from` 由系统读云端填入，diff 由系统计算，风险由规则引擎判定。
3. **执行权在 CI**：Agent 进程不持有写凭证；plan 只跑 `main` 的可信代码。
4. **回滚即 revert**：Git 历史就是变更历史。
5. **归一化先于比较**：`443` 与 `443/443`、`203.0.113.7/24` 与 `203.0.113.0/24` 必须视为同一条规则，否则会产出假变更。

## 退出码（给 CI 当门禁）

| 码 | 含义 |
|---|---|
| 0 | 无变更，或只有 low/medium |
| 1 | 存在 high 风险，需要人工逐条确认 |
| 2 | 存在 blocked（如管理端口对全网开放），禁止合并 |

## 里程碑

- **M0 ✅**：只读 + diff（不接模型，纯 CLI 可验证）
- M1：接 Agent + GitHub PR + apply
- M1.5：RAG（资源卡片 + 知识库 + 检索路由 + 评测集）
- M2：React 前端 + 收敛校验
