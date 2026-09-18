# Track 3 candidate acceptance

Establish a reliable baseline before promoting a faster simulator. Delivery,
complete public validation and measured improvement are separate gates. Local
results are non-rankable; passing tests of the tools does not qualify an image.
Commands and evidence formats live in [CANDIDATE-RUNBOOK.md](CANDIDATE-RUNBOOK.md).

## 当前范围

已授权在 Mac 的原生 Linux arm64 独立实例先建立本地 B0/B1：4 CPU、16 GiB
容器内存、64 GiB 有界输出盘、离线执行。证据标记 `rankable=false`、
`qualification_scope=local-arm64`，不同架构或资源口径不得共用晋级历史。
正式 linux/amd64 与官方 10G 适配单独验收，硬件以 [baselines 表](../baselines/README.md)
为准。测试主机还须给控制器及校验进程预留内存，容器限额不等于主机总内存。

## 目标与门禁

B0 是首个 G1/G2 全过的固定镜像；B1 是相对 B0 首次通过 G1/G2/G3 的挑战候选。
以后每次仅与当前稳定候选比较。预算耗尽、停止实验或工具测试通过均不算目标完成。

| 门禁 | 必须同时满足 |
| --- | --- |
| G1：可交付 | 固定 digest 的目标架构镜像可匿名拉取；双 verb 离线执行；官方 C5 parser、真实团队 team-claim 2.0 与两文件 ZIP 绑定通过；密钥不入包 |
| G2：可靠运行 | 全部 72 公开单元完整执行；有参考的单元通过公开语义门禁；同 seed 重复 parquet 字节一致；独立参数与异常回退比较通过；容器内存和运行盘的实测及限额证据齐全 |
| G3：可信增益 | 全 roster 同机配对测量；总分增量的 95% 区间下界大于零；没有明确的 family 退化；G1/G2 仍有效 |

exemplar 没有公开参考：完整核实 schema、解码行数、事件计数、排序、重复性与资源，
语义比较仍为 `unknown`。这不能证明 Final 隐藏参考等价。

本地协议为每镜像每单元一次预热、五次正式测量，顺序交错；逐单元取宿主测量
`n_events / wall_clock` 的中位数，再对完整 roster 求均值。配对区间以完整运行组
同步重采样 A/B，重新计算分数差；不用自报速度、最佳单次值或成功子集均值晋级。
不同 seed 的泛化比较与同 seed 重复性分开验证。

## 迭代与证据

按“证据缺失或过期 → 交付/正确性失败 → 资源缺口 → 性能机会”的顺序推进。
每轮只检验一个假设，开跑前固定镜像、roster、协议、独立参数、资源和时间预算。
筛查通过后另做一次完整确认；筛查数据不证明收益，也不反复确认直到显著。
失败后修复实现，再用新参数集确认；旧参数集保留为回归证据。

| 结果 | 自动动作 |
| --- | --- |
| 缺证据、环境失败、过期 | 保留稳定指针和失败记录，补证据或修复环境 |
| G1/G2 失败 | 修复最小复现，重新完整验收 |
| G3 失败或区间跨零 | 保留当前稳定候选，记录拒绝或 inconclusive |
| 全门禁通过 | 比较稳定指针未变后晋级，保留旧镜像和原始证据 |
| 已晋级版本出现问题 | 回退到仍满足当前验收的历史候选，追加原因 |

计划、测量、校验结果和晋级历史由工具生成，状态从证据派生，不在文档手抄
速度排行榜、镜像 digest 或动态通过数。同一问题连续三次失败停止自动重试并提交诊断。
镜像、输入或验证器变化使相应证据失效；文档变化不强制重跑仿真。

## 正式提交前

准备好候选后及时取得 Development 反馈，不等待下一轮优化。上传另按当时
平台入口及正式截止时间执行；保存提交回执、镜像/ZIP 摘要和逐门禁结果。
Final 使用对应 phase 重新封包，不能沿用 Dev descriptor 或 claim。

需重新核实的外部条件：T3 提交入口、实际 C1/C2/C7 测量协议、输出接收限额，
以及 [生产重复摘要缺陷](https://github.com/Agenthon-2026/track3-simulation-public/issues/5)
的部署修复。stable/volatile 名单本身不证明生产修复已上线；不得伪造固定计时规避。
新增付费资源及对外沟通须用户授权，PR 合并另待明确指令。

规则入口：[Hub v2.4.3](https://github.com/Agenthon-2026/Agenthon2026-public/tree/v2.4.3)、
[TEAM-CLAIM](https://github.com/Agenthon-2026/Agenthon2026-public/blob/v2.4.3/starter-packs/track3/TEAM-CLAIM.md)、
[Track 3 容量与排名答复](https://github.com/Agenthon-2026/track3-simulation-public/issues/1#issuecomment-5534948011)、
[官方赛期](https://www.agenthon.net/rules/)。Development 至 2026-10-12 AoE；Final+Verification 为
10-13–10-25。BYO 已出局（裁决 2026-09-18）；`category` 必须为 `simulator`。动态公告在提交前复核，
不把旧网页观察当作当前状态。
