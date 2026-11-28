# infra-live · v0

**一句话：改 YAML → 跑一条命令 → 看到「云端还差什么」和「这事危险吗」。**

这就是 GitOps 最小闭环里，唯一属于「业务」的那一段。后面的 CI、PR、AI 都是往这个结论上加壳。

## 跑起来

```bash
python3 -m venv .venv && .venv/bin/pip install pyyaml

.venv/bin/python fmt.py                           # 把 envs/*.yaml 写成规范形
.venv/bin/python fmt.py --check                   # 只检查（CI 用，不规范就返回 1）

.venv/bin/python plan.py                          # 正常场景，退出码 0
.venv/bin/python plan.py envs/web-sg-redis.yaml   # 反面场景，退出码 2

.venv/bin/python tests.py                         # 7 条行为测试
```

## 文件

| 文件 | 是什么 |
|---|---|
| `envs/*.yaml` | 期望状态。人写的、Git 存的、以后 AI 也只写这个 |
| `current.json` | 云端现状。用一份快照假装，还不真连云 |
| `model.py` | 规范形 + 比对：`parse` / `dump` / `diff` |
| `plan.py` | 判风险 + 打印 |
| `fmt.py` | 把 YAML 写成规范形 |
| `tests.py` | 7 条行为测试，不依赖 pytest |

## 一条规则长什么样

五个身份字段，多一个都不算同一条规则：

```yaml
- direction: ingress
  protocol: tcp
  ports: 22/22        # 或 any
  peer: 198.51.100.7/32
  action: accept
  note: "SSH，仅固定出口 IP"   # 只给人看，不参与比对
```

**没有 priority 字段——列表顺序就是优先级**。插入一条不会让后面的规则都算"变了"，
只有真的调换先后，才会出现一处「顺序变化」。

## 归一化 = 解析 + 按规范打印

- `parse` 宽松：`22`、裸 IP、`-1/-1`、`1/65535` 都认
- `dump` 严格：只输出规范形
- `fmt` = `dump(parse(文件))`，写回文件；`--check` 只比对不写

代价：`fmt` 会重排文件并**丢掉 `#` 注释**。要保留注释得换按位置解析的 YAML 库。

## 变更只有四种

| 符号 | 含义 | 怎么来的 |
|---|---|---|
| `~` | 修改 | 同方向 + 同协议 + 同端口 + 同地址族的两条规则，一条要删、一条要加，配对成一处修改 |
| `+` | 新增 | 期望里有、现状里没有 |
| `-` | 删除 | 现状里有、期望里没有 |
| `↕` | 顺序变化 | 两边共有规则的先后不一样了 |

`修改` 是配对出来的，云上并没有"改一条规则"这个动作——它永远是删一条加一条。
配成一行只是为了让人一眼看懂：`443 ← 0.0.0.0/0 → 203.0.113.0/24`。

## 现在**不**做的事

- 不连云、不写云、不调模型
- 不拆模块、不建目录树
- 没有"不同比对策略"这种东西，`diff` 只有一个参数 `order_matters`
