# 辽宁留存证据与旧队列只读补查

本轮没有发网站请求，没有连接服务器、浏览器或 SSH，没有读取凭据、profile、HAR 或认证日志，也没有改变辽宁队列、原件、采集、保活、hold 或运行服务。代码只读取明确的本地业务目录；参数留在 RAM，Git 报告仅保留旧 key/身份 hash、证据路径/SHA 和脱敏结果。

## 定稿结果

| 比较对象 | 第二阶段基线 | 本轮找回 | 仍缺或待补 |
|---|---:|---:|---:|
| 联合技术来源缺证/待审 | 94 | 43 个来源找到 metadata→原件 SHA 链，样本结构回放通过 | 51 个仍没有受控目录内可核验的原件链 |
| 未映射旧记录视图 | 1,552 | 1,499 条身份可由现协议表达 | 42 条真实 Monday/query 锚点待协议支持；11 条仍缺精确旧 key→query 桥 |

43 个样本结构通过不等于线上资格、字段语义、完整历史、全主体或全分页通过。185 是技术来源联合数，不能称为独立业务指标数。本轮新增留存原件也不会凭同名或相同响应 SHA 被当成旧记录的原件。

5 个基线 source view 的 SHA、records、states、mapped、unmapped 和 consumed 都与 phase2 相符。原 13,032 条记录视图逐项比较后，dataset、legacy_state、state、consumed、facts_sha256 均不改变；74 saved、6 empty、1 unknown、12,951 pending 保持原样。原有 11,480 个身份不变；新增 1,499 个身份后为 12,979 mapped、53 unmapped。来源视图可能重叠，这些数不能相加当唯一任务数。

| 基线 source view | 原 unmapped | 本轮可映射 | Monday 协议待补 | 缺精确桥 |
|---|---:|---:|---:|---:|
| verified expansion queue | 956 | 904 | 41 | 11 |
| active manifest prior_outcomes | 3 | 2 | 1 | 0 |
| nonmarket native engine queue | 2 | 2 | 0 | 0 |
| sitewide_0353290a57044c3e engine queue | 296 | 296 | 0 | 0 |
| sitewide_e38351fe661a034a engine queue | 295 | 295 | 0 | 0 |

`#prior_outcomes` 的 phase2 SHA 是过滤出 prior 对象后 canonical JSON 的 SHA，不是整个 manifest 文件的 SHA；报告另外保留 manifest 原文件 SHA。

## 身份桥的依据

- 885 条 verified pending 的精确 params 对应 `goal92_candidate_specs.py` 中既存 ReportAdapter 声明。014/007 的查询日期保持普通日语义，不因标题含“周”而变成周一或 ISO 周。
- 588 条原生引擎 pending/active 已留存精确查询参数；既存 source rule 可以表达身份。原 active 的 unknown、owner 未结算与 stage blocker 保留。
- 19 条旧列表 outcome 由原件 metadata 中完整 query 的旧 key、dataset、snapshot_date、page，以及 outcome/原件 body SHA 双重关联；不同日期共享相同响应 SHA 不构成身份桥。
- 5 条原生引擎 outcome 自身明确保存查询 date，并有原生引擎 writer 源码契约；只补身份，不从归档目录名或文件 SHA 推日期。
- 2 条 prior outcome 的原生回执保留 params、HTTP 200 和 body SHA；重新计算 expanded 旧 key，并与 prior 的 raw_sha256 一致。回执身份桥不会被算成找回原件。
- 42 条 025 保留真实 Monday 查询日期及 `week_start_monday` 声明，其中 41 pending、1 prior。当前 TaskIdentity 的 week 要求明确起止日，不能无损表达原生周一查询锚点。本轮保持 identity=None，未生成周末、ISO 周、snapshot 或 day 兜底身份。

## 查找范围与仍缺的桥

读取了以下明确业务材料，并逐文件登记 SHA、读取后复查是否改变：

- `verification/20261027/disclosure_timing_full_rows/retained_remote/data/2026/original/` 的 advanced_reports、power_reports、native_lists、report_engine 元数据及响应；这是已经留存的展开目录，没有解压或改动压缩包。
- `data/2026/original/` 上述四类原件目录。
- `state/quality_repair_20261021/recovered_originals` 的 provenance→metadata→response；`continuous_expansion_20261019/samples` 与 `sitewide_expansion_20261016/after_document_20261019/samples` 的 observation→response。
- 5 个 phase2 基线队列视图、manifest 中原生回执与 source rule 源码。源码仅 AST 读取，不导入或运行旧采集器。
- 原来列为缺失的 advanced/sitewide 旧快照实际留在 `state/server_verified_expansion_20261023/` 子目录，本轮找到 3/49 个 dataset group；另核对 `dimension_coverage_20261024/advanced_before.json`、`sitewide_before.json`（3/49 group），及 `reporting/diagnostics_20261024/retained_for_attachment/ordinary_queue.json`（121 group）。这些额外快照单列，不增加 1,552 基线 cohort 的分母或恢复计数。

还针对缺桥对象只读核对了 quality_supplement 的业务 queue、dimension_coverage 的 reused_sample_outcomes、field_audit data_repair 的业务 receipt，以及本地来源/字段 catalog 的辽宁证据引用。它们没有为下面 11 条记录提供同时吻合旧 key 和响应 SHA 的精确查询身份；派生 JSON、响应存在、同名、相同空响应 hash、归档路径日期均未被用来代替这条桥。

| 仍缺桥的 dataset | 记录视图数 | 缺失事实 |
|---|---:|---|
| realtime_node_price | 1 | 旧 empty outcome 缺精确查询身份与相应旧原件/回执关联 |
| dayahead_power_and_average_price | 2 | 旧 saved outcome 缺精确查询周期/选择项的 key 桥 |
| sitewide_2010f5bbae811586 | 2 | 旧 empty/saved outcome 缺精确查询身份桥 |
| sitewide_90426958a40aec46 | 3 | 旧 empty/saved outcome 缺精确查询身份桥 |
| sitewide_c1a062960820eff8 | 2 | 旧 empty/saved outcome 缺精确查询身份桥 |
| sitewide_e814f8cf448bfafb | 1 | 旧 empty outcome 缺精确查询身份桥 |

6 条候选原件链未采用：1 条 metadata 的 dataset 与归档目录不符；3 条 provenance 内层/外层身份或 SHA 不一致；2 条 observation 没有查询身份。具体路径、SHA 和缺因保留在报告；没有通过别名或文件名纠正这些冲突。

## 代码与复核

`pmos/recovery/liaoning.py` 提供三个纯只读 API，root 可为工作区或辽宁目录：

```python
from pmos.recovery.liaoning import baseline_queue_records, resolve_queue_records, recover
before = baseline_queue_records(root)  # phase2 decoder，按 source_ref 分组
after = resolve_queue_records(root)   # LegacyTask，真实身份只在 RAM
report = recover(root)                # 脱敏报告，不自行写盘
```

`before/after` 使用相同账号别名 `offline-shadow-review`；只解除 identity_mapping_required 与已证实的 source_parameter_identity_mapping_required，其他 blocker 保留。所有记录仍禁止派发。发生源 SHA 漂移时不把新采样计为旧 cohort 恢复；读取过程中漂移时 RAM resolver 拒绝给出完成结果。重复读取发现改变会立即拒绝并持久登记本次扫描的 changed_paths，即使文件随后恢复原样也不会漏报。

`evidence/liaoning_phase3_recovery.json` 小于 1 MB。`evidence_refs` 是路径/SHA 共用表，`queue_evidence.record_kinds` 保存重复状态、缺因、证据方法和 blocker；每条旧记录通过旧 key hash、身份 hash 与 evidence id 可复核。原始参数、选择项和业务响应不写入报告。

本轮 12 项合成回归通过，覆盖原件 SHA/dataset 冲突、同 SHA 跨日期与类型选择项、Monday 语义、显式引擎 outcome date、回执桥、路径越界/symlink/private、读取漂移及改后恢复、基线计数状态不符、unknown/stage/hold 与 denied/failed/saved 保留。真实只读 before/after 另核对全部 13,032 条记录，状态与 facts 守恒。线上资格、owner 交接和完整历史仍未由本轮验证。
