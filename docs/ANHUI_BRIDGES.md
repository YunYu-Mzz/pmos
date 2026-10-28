# 安徽最后 10 条固定旧身份桥（2026-10-28）

本轮固定第四阶段报告中的 10 条剩余记录，补回 **10 条身份，剩余 0 条**。原 namespace、旧 key、状态、consumed、facts SHA 和保护项逐条不变。只有本地只读查找与 RAM 身份补充，没有网站请求、浏览器/SSH、数据库访问、生产写入、队列导入或采集/保活操作。第二至四阶段报告及旧模块均未改。

结果见 [anhui_phase5_bridges.json](../evidence/anhui_phase5_bridges.json)。固定第四阶段报告 SHA 为 `4c77082676f5e69300229f9918a83ed81125428133ecca3c8a430191aedd3b2f`；第三阶段 namespace 来源也按第四阶段承诺 SHA 读取。

## 来源和身份

| 原剩余来源 | 固定旧记录 | 本轮身份桥 |
|---|---:|---:|
| `interregional_exchange_monthly` | 7 | 7 |
| `market_disclosure_monthly` | 2 | 2 |
| `publication_4ab104c9f4b8bf37` | 1 | 1 |

三个来源目前均有已留存的 3 页原生 listing 原件与 parsed。本轮按精确旧题名/日期算法复算原 key，核验同菜单的 source/parsed 双 SHA，再回联唯一 `(guid, informationId)`。不根据相同题名、下载文件或附件猜 ID。

原生 listing 读取复用第四阶段已审实现：共同 `evaluate` 先拒绝直接/嵌套业务错误；UI 证明必须为真正的 `True`；行数、总数、页码和页大小严格区分整数与 bool，并核对分页计数。若 ID 缺失、冲突、跨菜单或 SHA 不符，桥不成立。

新身份遵循共同核心：`operation=article`、`snapshot=undated`、真实 `record_id` 仅在 RAM。发布日期只用于核对旧公告身份，不变成统计期。报告保存 ID SHA、技术 ID、资格、路径及 SHA，不复制题名、真实 ID、选择项或业务值。

## 旧快照、漂移和状态守恒

10 条旧记录均由完整 raw record 精确重建，没有根据当前成功状态反向猜旧记录：

- 9 条来自第四阶段已登记整文件 SHA 的迁移旧 snapshot，旧 key/facts/state/consumed/protection 均完全相同。
- 1 条来自整份已漂移队列中事实完全未变的旧记录；其旧菜单仍由第四阶段承诺整文件 SHA 的旧 snapshot 锚定。

旧菜单需要同时满足已登记文件 SHA 和第四阶段 `menu_scope_sha256`，不能由当前已漂移队列单独提供。原 source namespace 另按第四阶段 `source_ref_sha256` 核验。

全部 10 条的原状态为 **pending 5、saved 5，consumed 5**。现有采集器已把其中 5 个旧 pending 变成当前 saved，本轮仍保留原审计 cohort 的 pending；其他 blocker（包括 `attempt_index_required`）不清除，只在补身份成功时移除 `identity_mapping_required`。原事实与保护集合摘要为 `dfada8b5dac82f460c6ef9f8ecb461474cfba6aaae71cd50c233ba17d77fc72e`。

保留旧 pending 是固定审计口径，不表示这些任务现在仍未采。报告不可派发，`queue_import_ready=false`；未来交接必须另外纳入当时最新的已消费事实。

本轮读取 32 份受控引用：12 份 SHA 与第四阶段登记相同、4 份已变化（3 份 queue、1 份 listing index）、16 份本轮新登记。新登记表示此前报告未登记该引用，不当作新增采集数或新样本验收数。

所有读取保留首次 SHA；重复读取发生变化立即拒绝，文件改回也不清漂移。末尾重核省份文件及两个基线文件，并拒绝基线父目录软链与超限文件。本次扫描期间无漂移；与旧阶段的已知变化另行分组报告。

## 资格和后续缺证

第五轮只补映射，**新样本资格 0、业务语义升级 0、线上资格未变**。加上第三、四阶段，原第二阶段安徽 1,696 条未映射记录已全部补身份；这属于原 114,599 条记录视图的固定基线，不代表当前滚动队列、唯一业务任务数或生产迁移完成。

3 份旧 binding 精确副本和 52 个候选继续作为缺证带入独立 followup。它们未因公告身份桥完成而准入。PDF 容器、原单元格、旧 UI 补链和第四阶段保留的 24 份 `UI crosscheck=false` 负证均保持原资格，不在本模块重复推断业务语义。

## 复核入口

`pmos.recovery.anhui_bridges.recover(legacy_root)` 返回脱敏报告与顶层 `summary`。`baseline_queue_records()` / `resolve_queue_records()` 返回相同原 namespace 和旧 key 集的 before/after RAM 视图，供 `validate_queue_recovery` 独立核验；真实选择项和 ID 不可直接序列化。

13 项脱敏专项测试通过，覆盖 9 条旧快照/1 条漂移队列记录、当前 saved 不覆盖旧 pending、错误 scope/SHA/namespace、零/多 ID、业务拒绝、旧 raw 无法重建时 fail closed、重复旧承诺及末尾基线变化。真实完整 `recover()` 和 before/after 核验另在禁止网络、子进程、SQLite、所有文件写入及目录变更的 audit hook 下通过。
