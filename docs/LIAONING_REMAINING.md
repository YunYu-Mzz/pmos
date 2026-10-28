# 辽宁剩余证据补查（2026-10-28）

本轮固定 `evidence/liaoning_phase3_recovery.json` 的 SHA `433ec2090a1741bda88534b12212db0f89d79f065831b1485aa0c61bef0c5eaf`，只比较其中 51 个技术来源和 11 条缺桥旧记录。Monday 身份补齐由独立模块处理，不重新选取本轮 cohort。旧 source snapshot SHA、source_ref、旧 key hash 和 facts hash 均核对；同一个旧 key 出现在其他快照时不能加入 cohort。

## 结果分层

| 原 51 个技术来源 | 数量 | 实际找到的内容 |
|---|---:|---|
| 精确 same-request 别名及 target 原件链 | 17 | 既有 catalog/coverage 的 source-ID、route、原生 report number、prior_dataset，与 target metadata 的精确 endpoint 一致；target 原件 SHA 和样本结构回放通过 |
| 仍没有 own/alias 精确原件链 | 34 | 缺原件或只有拒绝、字典、过滤项、历史状态等材料 |
| source 自身目录新增业务原件 | 0 | 51 个 source 目录自身仍缺可核验业务原件；上面的 17 条是重用 target 原件 |

17 条别名链没有增加独立原件、独立指标、网站请求或线上资格；没有继承 target 的权限、终态、字段语义或完整历史接受。报告保留原 source 状态，所有业务接受仍 pending。

原 11 条旧记录本轮新增精确桥 0、仍缺 11。5 empty、6 saved 的状态、consumed、facts_sha256 和 blocker 逐项保持。新的本地 checkpoint 只能重复证明旧 outcome 存在，未保存其精确 query/job，不能据此恢复身份。旧响应 SHA 相同、归档目录日期、同名或上述 source 别名均没有用作旧任务身份桥。

## 17 条别名链

| 技术来源 | 明确 prior_dataset | 原生 report number |
|---|---|---|
| sitewide_150e628bb9f9b03a | nuclear_total | 021 |
| sitewide_15eb4fa6873fb948 | tie_line_schedule | 062 |
| sitewide_29c2411e94c469d0 | tie_line_dayahead | 008 |
| sitewide_2c25420ab856210f | sitewide_80ece90b472eddab | 031 |
| sitewide_3356cf7fb2c14651 | generation_total_forecast | 034 |
| sitewide_3b8e6b1c672f10d0 | hydro_daily | 070 |
| sitewide_4c35e252e0770c53 | dayahead_power_and_average_price | 012 |
| sitewide_5160397d5d7e23c5 | distributed_solar_actual | 037 |
| sitewide_6108804d03af448c | generator_maintenance_capacity | 040 |
| sitewide_7089cafdcefe3c45 | distributed_wind_actual | 035 |
| sitewide_72b7a2340c3d2fde | local_hydro_plan | 047 |
| sitewide_b2cee9801bac0f53 | local_hydro_actual | 057 |
| sitewide_b422b2f3c09fbc93 | distributed_solar | 027 |
| sitewide_bf835d54e880daf6 | realtime_node_price | 005 |
| sitewide_da32804173198088 | distributed_wind | 036 |
| sitewide_daf0739f06cba7ca | local_coal_forecast | 020 |
| sitewide_fca714454cbc8832 | must_run_stop_units | 056 |

每条链要求 catalog 的 dataset/number/source-ID/route 与 native_coverage 明确 prior_dataset 绑定一致；只有 `existing_workflow` 或 `alias_of_same_request` 且符合既存 reuse_basis 的条目可用。target metadata.dataset 和完整 endpoint 必须一致，原件 SHA 与结构回放必须通过。任何同标题、近似 endpoint、错 number、错 route、错 dataset、denied 状态或普通别名文字都不足以形成这条链。报告登记绑定 hash、endpoint hash、catalog/coverage 文件 SHA、metadata/原件 SHA；不写原查询参数和选择项。

## 34 个仍无精确链的来源

| 本地既有状态 | 数量 | 保留的边界 |
|---|---:|---|
| excluded_prior_decision | 7 | 不替代原排除决定 |
| denied_no_retry | 2 | 不因过滤项成功而解除数据端点拒绝 |
| known_failed_no_retry | 1 | 不返还失败任务 |
| held_after_shared_endpoint_denial | 8 | 不继承共享端点的权限，不解除 hold |
| known_empty_no_retry | 3 | 空材料或字典成功不等于业务数据资格 |
| 不在该 native_coverage 快照 | 13 | 无对应精确原件链，不从其他来源名称推断 |

具体 34 个技术 ID、每项状态和缺因列于 `evidence/liaoning_phase4_remaining.json`。其中 `sitewide_7a300d985f7a8cfc` 的已存 observation 明确为 dictionary，且 data_report_permission_verified=false；空原始 JSON 的 SHA 虽然吻合，仍只能作为字典证据。`sitewide_a64ef9a87d10919c` 的 table filter/period 成功不能替代 table data 拒绝。`sitewide_6c6bd17628fc29f7` 的 mapping_proof 是证明容器，其文件 SHA 不等于所记响应 body SHA，不作为业务原件；原拒绝保留。

## 11 条仍缺身份桥的旧记录

| 技术 dataset | 旧记录数 | 仍缺的事实 |
|---|---:|---|
| realtime_node_price | 1 | old key 对应的精确原生 query 与同一响应 body SHA |
| dayahead_power_and_average_price | 2 | 同上，包括选择项类型和精确查询周期 |
| sitewide_2010f5bbae811586 | 2 | old key→query→body SHA 三者一致的业务回执 |
| sitewide_90426958a40aec46 | 3 | 同上 |
| sitewide_c1a062960820eff8 | 2 | 同上 |
| sitewide_e814f8cf448bfafb | 1 | 同上 |

旧 key、query 和 body SHA 必须同时吻合，参数额外字段不能被丢弃，选择项数字与字符串不能混同；原已消费状态和 blocker 仍保留。本轮没有为这些缺项产生新网站请求。

逐条核对确认，这 11 个真实旧 key（包括 realtime_node_price）都是 expanded 协议，并且都有明确 ReportAdapter 参数规则。检索同时支持 native_lists 的原生 snapshot 协议：必须由原 query.key、dataset、snapshot_date、page、精确 params 重新计算同一旧 key，且 body SHA 与旧 outcome 一致；不要求不存在的 ReportAdapter rule。本轮 cohort dataset 的 metadata 没有 native snapshot job 命中，不能用其他列表的 metadata 替换这些 expanded 旧任务。

## 已读范围与复核

- 在 8 个明确业务原件目录列出 metadata 文件，只读取 cohort 或精确 target dataset 的 21 份 metadata 及相应原件。未整树读取 JSON；51 个技术 source ID 在这些原件目录没有自身 metadata 命中。
- 读取 catalog、native_coverage、source rule 源码和针对三项负面材料的业务 result/observation；旧采集模块只作源码读取，不导入或运行。
- 核对 6 份历史业务队列快照，包括嵌套 advanced/sitewide、dimension before、10/24 retained ordinary 和 10/28 resource_wait/checkpoints_before ordinary。最后两份均只再次保留 11 个旧 outcome，没有对应旧 job/query；新增快照条目不加入原 cohort 分母。
- 核对 quality_supplement 的 1,324 条业务记录及 dimension_coverage 的 107 条 reused outcome；没有找到对应的精确 old key/query/body SHA 桥。
- 仅列出 verified_adapters、release、retained_export 三个归档的 175/325/882 个成员；不读取私密/越界/链接成员，不解压、不读取成员 payload。51 个 source ID 没有归档成员名命中，不能将归档存在当成原件恢复。

实现位于 `pmos/recovery/liaoning_remaining.py`。`recover(root)` 返回脱敏报告和顶层 summary；`resolve_queue_records(root)` 仅在 RAM 返回原 11 条 LegacyTask 视图。本模块不改 phase2/3 证据，不改现有恢复模块，不写原省任何文件。Reader 的 symlink/private/越界和 sticky drift 检查继续生效；任何输入漂移都会使 recover/resolve 直接拒绝，不返回混合快照的别名恢复计数。

13 项合成回归通过，覆盖 alias source/route/number/endpoint/dataset、拒绝状态、旧 key 与类型/周期/额外参数、body SHA、native list snapshot 的无 ReportAdapter 精确桥、outcome-only 快照、active 保留、phase3 SHA/cohort 尺寸、同 key 跨快照隔离、归档只列成员、字典证据不升级，以及找到别名原件后发生漂移仍拒绝报告。真实 11 条旧记录另逐项核对状态、消费事实、facts hash 和 blocker 守恒。零网站请求、零生产写、零 SQLite 连接；原采集和保活未操作。
