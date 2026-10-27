# 第三轮：现存原件、回执与历史队列补证

2026-10-27。本轮仅查本机已有材料，没有网站请求、SSH、浏览器连接、服务操作或旧省份目录写入。第二阶段报告保留原样，新结果分别记在 `evidence/*_phase3_recovery.json`。新增 `pmos/recovery/` 负责只读查找、SHA 补链和 RAM 内身份对照，Engine、Ledger、decoder、线上门禁不变。

## 实际补回与剩余项

| 省份 | 本轮补回 | 仍保留的缺项 |
|---|---|---|
| 辽宁 | 原 94 个缺证/待审技术来源中，43 个找到 dataset→metadata→原件 SHA 链并通过样本结构回放；原 1,552 条未映射视图补回 1,499 条 | 51 个来源仍缺原件；42 条已有真实 Monday 查询证据，但当前身份协议不能无损表达；11 条缺精确旧 key→查询桥 |
| 安徽 | 原 1,696 条未映射视图补回 1,676 条；43 份早期可变 binding 中 40 份找到与旧 digest 相同的不可变副本；历史合同 3 页补到独立 UI 来源链 | 20 条公告缺原列表身份；3 份 binding 缺精确副本；52 个候选仍缺独立原件，后 7 组语义/容器限制保留 |
| 山西 | 原 49 项分为 4 项非空原件链、9 项仅空查询、2 项仅空 selector、1 项显式别名、14 项拒绝回执、19 项仍缺绑定原件 | 分层证据不改变原资格；历史 4 次 40302 保留，新的 14 条拒绝也保留；业务身份映射与完整范围/语义仍待审 |

“来源”是原技术 ID 口径，“记录视图”可能来自重叠快照；二者均不等于独立业务指标或当前服务器任务总数。发现原件不意味着完成全年、全部主体、分页、字段语义或线上验收。

辽宁 Monday 记录保留 `identity=None`，不猜 ISO 周、周结束日，也不改写成普通日或 snapshot。安徽公告按精确原列表行恢复稳定 record ID，不把发布时间当统计期。山西空查询、空 selector、拒绝和独立非空样本分别报告。

## 旧队列事实守恒

本轮使用同一 `LegacyTask` 和原兼容 decoder。独立校验器 `pmos/recovery/invariants.py` 对 before/after 逐项检查：source view、旧 key 集与数量、dataset、legacy_state、state、consumed、facts SHA、既有 identity 及所有保护 blocker。只允许填入原本缺失的身份并去掉相应映射 blocker，不导入 Ledger，不生成生产任务。

- 辽宁复核原 5 个来源视图、13,032 条记录，原 1,552 条未映射余 53 条。6 empty、12,951 pending、74 saved、1 unknown 以及 81 条 consumed 均保留。
- 安徽复核 62 个受影响来源视图（61 份实体队列及 1 个派生 daily 视图）、2,008 条记录，原 1,696 条未映射余 20 条。635 saved、1,351 pending、22 unknown 以及 657 条 consumed 均保留。原 114,599 条全量基线未改写，未重新把滚动新增记录计入恢复数。
- 山西补到 10 个 SQLite 历史文件，按整库 SHA 去重为 7 份，共 19,226 个快照记录视图、6,319 个跨快照旧身份。902 个状态变化集合及 1,878 个含 leased/failed/denied/denied_scope 的保护身份保留；较新 pending/complete 不能抹掉旧事实。
- 山西 47 个 journal 内容视图有 12,360 条任务视图、10,925 个唯一旧 key，保留 1,201 个 pending|saved 状态集合和 7 个 unknown。digest 清单不冒充 journal 内容，完整 Crawlee storage 导出仍未被证明。

本轮纠正了先前搜索范围的遗漏：辽宁两份旧快照在嵌套 state 目录，山西历史库和 journal 在 verification 目录。第二阶段“未发现”是当时限定路径的搜索结果，不应继续理解为本地绝对不存在。本轮仍未读取当前活动 SQLite；仅允许无 sidecar、rollback header、短时 `mode=ro` 的历史快照。10/21 四份库有独立 SHA 回执，10/25 的同目录 review 缺独立整库 SHA，明确保留较弱资格。

## 查找范围及一致性

每省文档列出实际查找目录与剩余技术 ID。跨省字段目录只按省份和技术 ID 精确关联：辽宁 86、山西 45 个原缺项找到目录项，36 个被引用文件的 SHA 核对通过。它们主要是派生目录/核验记录，资格提升数为 0。安徽无直接 dataset key 的 52 个候选不以相同标题匹配为原件。

只读并不冻结正在采集的文件。辽宁、安徽固定首次读取 SHA，重复读取变化立即阻断，结束时重核；文件改回也不抹掉已观察的扫描漂移。山西按回执预承诺 SHA 核对原件，对历史库另做读取前后整库 SHA 和 sidecar 检查。安徽相对第二阶段有 3 个滚动输入变更，单列原/现 SHA；它们没有替换目标旧队列 cohort。有一次扫描因真实中途变化停止，未写出混合报告，后续稳定扫描成功。

报告仅含技术 ID、路径、状态、计数、SHA 和证据资格。请求参数、主体值、真实 article ID、模型和原始业务行只在 RAM；原件不复制入 Git。山西 494 个 manifest 外工作簿只登记路径/字节 SHA，不计作已消费任务。

## 复核入口

在 PMOS 源码目录，用 Python 3.11+：

```sh
python3 tools/test_offline.py
python3 tools/recover_existing_evidence.py all
```

第二条默认不写文件；可选 `--output-dir /absolute/path/to/pmos/var/new-review` 仅允许不存在的新目录，拒绝 var 及其子路径的软链。它不经过任何采集/保活入口。现存输入若已滚动变化，报告会明确拒绝或列漂移，不能把新 cohort 当原缺项恢复。

- [辽宁证据与剩余项](LIAONING_RECOVERY.md)
- [安徽证据与剩余项](ANHUI_RECOVERY.md)
- [山西证据与历史快照](SHANXI_RECOVERY.md)
- [本轮独立验证及代码指纹](../evidence/phase_three_validation.json)

离线回归共 243 项，242 通过、1 项可选浏览器测试跳过。三省补证入口另在禁止网络、子进程和文件写入的审计钩子下成功运行；只放行历史 verification 库的 `mode=ro` 连接。辽宁、安徽的真实旧记录逐条守恒检查也通过。

线上验收仍 pending。没有切换 owner、暂停/重启采集、修改保活、解除拒绝或向网站补发请求。
