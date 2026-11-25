# infra-live

用自然语言驱动的云资源运维：**一句话 → 提案 → PR → CI 执行 → 回写**。

- 对话是输入端
- Git 是审批与审计载体
- CI 是唯一有执行权的一方
- AI 永远只能「提议」，不能「执行」

## 当前阶段

`v0.1` — 设计定稿中，尚未进入实现。设计文档见 [docs/DESIGN.md](docs/DESIGN.md)。

## 首发范围（dev）

- 云：阿里云（ECS / 安全组）
- 资源类型：`security_group`（读写），`instance`（只读）
- 「防火墙策略」= 安全组规则（ingress tcp），暂不涉及 WAF 实例

## 结构

```
envs/dev/            期望状态（YAML，人工与 AI 共同维护）
reconciler/          无状态收敛器：读云端 → 算 diff → 执行差异
proposals/           每次提案的原话 + 意图 + diff + 风险留档
.github/workflows/   plan（PR，只读） / apply（main，可写）
docs/                设计文档
```

## 设计要点

1. **读写分流**：只读查询直接答，变更一律走提案。
2. **模型只产意图**：`from` 由系统读云端填入，diff 由系统计算，风险由规则引擎判定。
3. **执行权在 CI**：Agent 进程不持有写凭证；plan 只跑 `main` 的可信代码。
4. **回滚即 revert**：Git 历史就是变更历史。

## 里程碑

- M0：只读 + diff（不接模型，纯 CLI 可验证）
- M1：接 Agent + GitHub PR + apply
- M2：React 前端 + 收敛校验
