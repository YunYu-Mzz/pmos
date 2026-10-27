# 安徽既有证据补链（2026-10-27）

本轮只读现存业务原件、回执、旧队列和本地目录。没有网站请求、浏览器/SSH连接、生产写入、队列导入、服务操作或保活操作。仍使用同一 PMOS 核心；新增代码只负责离线证据查找和 RAM 内身份映射。

结果见 [anhui_phase3_recovery.json](../evidence/anhui_phase3_recovery.json)。其基线是第二阶段两个已保存报告，不以当前新增采集数替代旧缺证恢复数。

## 旧身份映射

第二阶段 114,599 条记录视图中的 1,696 条未映射，本轮补 1,676 条，余 20 条；按原基线投影，mapped 从 112,903 增至 114,579。记录视图可能重叠，不能当作唯一业务任务总数，也不代表当前滚动队列已经迁移。

实际复核 62 个受影响 source views（61 份实体队列和 1 个由计划形成的 daily 视图），共 2,008 条旧记录。61 份实体队列及这些视图对应的尝试索引均匹配旧队列基线 SHA；daily 派生视图也匹配原 canonical SHA。

| 补链规则 | 原未映射记录数 | 身份及证据边界 |
|---|---:|---|
| inline 查询日期 | 1,170 | 旧 seed 代码和 inline UI 唯一日期输入证明 day 查询期；不推统计期 |
| publication 原列表行 | 424 | 验证 listing 原件/parsed SHA，按旧标题与发布时间算法复算原 key，并回联唯一 guid/informationId；operation=article、snapshot=undated、真实 record_id 仅留 RAM |
| 旧月度队列 | 30 | 同队列原件 provenance 的月份控件、同菜单和旧月度 seed schema 明确 month 查询期；不根据日期形状或标题猜周期 |
| portal 页快照 | 50 | 原队列显式 version、筛选范围、页码及原 source/parsed 回执绑定，按 snapshot 保存；不当作统计日 |
| daily orphan receipt | 2 | 旧尝试只有 phase+receipt，按精确原件/parsed 和旧 key 找回查询期；orphan blocker 保留 |

1,676 条的原状态为 saved 319、pending 1,342、unknown 15。逐条旧 key 集、`state`、`legacy_state`、`consumed`、`facts_sha256` 均不变；只补身份并去掉相应 `identity_mapping_required`，其他 blocker 保留。已知列表条目不等于正文或附件已采，原 pending/unknown/failed 不会因找到文件变成 saved。

余 20 条仍缺精确原列表身份链：`publication_eda606edc1c9d156` 1、`publication_86e8adf7fd232a7d` 2、`publication_4ab104c9f4b8bf37` 1、`interregional_exchange_monthly` 8、`market_disclosure_monthly` 8。已有正文或 PDF 容器不能代替尚未证明的原 article 身份。

## 43 份早期可变 binding

第二阶段 examples 精确列出的 43 份旧 parsed 均有预先写入的 `binding_sha256`，但没有 `binding_path`。现存 `_bindings/<sha>.json` 中找到 20 份内容寻址文件，精确补回其中 40 份捕获的 binding 链：文件 SHA 等于旧 parsed 预承诺的 digest，dataset/非日期 scope 完全一致，源与 parsed SHA 通过，整表投影重建相等。不是依据同名、相似 model 或新采集结果升级。

此结果保留“捕获时来自可变 state”的历史，只确认如今留存了与旧 digest 完全相同的不可变副本；不替代线上资格或业务语义验收。

仍缺 3 份：`day_ahead_average_offer_price` 两份旧捕获共用 digest `c59bab83929941b8428ec11ec5d4f3b9d7922eaf7d3b2a9bbc3f19564c186b47`；`publication_66d506c8a46471d9` 一份使用 digest `03b8cf4df54134cdbc8d60a4d7a73e00227b179a7c89fdc4106d2d067a1f9d9a`。不把当前可变 binding 的近似内容当精确副本。

已定向列名检索 `state/mz_migration_20261022_093815`、`coverage_recovery_20261023`、`mac_connection_recovery_20261027_backup`、`word_alignment_20261022` 和 `verification/20261027`，排除 profile/private/config/auth/cookie/token/browser/session/log 等目录；没有找到这两个旧 binding basename。只列出的压缩包均是其他公告附件，没有批量提取或读取敏感成员。这只是这些已查本地位置的缺证结论。

## 52 候选与后 7 组

52 个候选均在当前公开 coverage inventory 找到同 surface/source_index 的元数据，但其 evidence 列表均空、sampled_dataset 均缺。既有字段目录也为 52 项提供了登记桥，75 条引用都属于“核验记录”，没有新的独立原件资格。其中两个候选条目与已选 publication 技术 ID 共用目录归并关系；这种别名不证明候选菜单的独立范围或权限。52 项继续保持缺证/未准入，未改旧状态或授权范围。

后 7 组的 42 份旧载体全部按既有 SHA 重核：四个 XLSX 组共 27 份、公告正文 4 份、PDF 容器 8 份、历史合同派生记录 3 份。四个 XLSX 组的源单元格、正文源和 PDF 容器原资格保持，未新增字段语义或 PDF 全文验收。

`portal_historical_contracts` 的 3 个页面另外找到了独立 UI source、parsed、截图及原回执的完整 SHA 链，补足原来只登记 derived 的来源查找。此次仅补原件链，不宣称合同字段解析已重新完成语义验收。全部 50 个 portal 身份中，46 个有既有 UI source，3 个有原生 source，1 个仍明确标为 `retained_hidden_dom_three_identical_copies`，没有伪造该页独立截图。

## 漂移与输出格式

相对第二阶段原件报告的 public inputs，发现 3 个已变化的滚动文件：production plan、native rollout registry、`publication_22c779b5bdc2edc8` 尝试索引。报告登记原/现 SHA；这些新产出没有计入旧 cohort 恢复。上述 62 个目标旧队列视图及其尝试索引不受该漂移影响。检查期间首次 SHA 固定；二读变化立即拒绝，文件后来改回也保留漂移，结尾重新核验全部读过的文件。

报告采用共享 `references`、`queue_views` 和 `mapping_rules` 表；每个旧 key SHA 对应 identity SHA、原 state/consumed/facts SHA 以及 evidence IDs。由 evidence ID 可查完整路径和 SHA。真实标题、模型、guid、informationId 和 selectors 不序列化入 Git。文件约 979 KB，以紧凑 JSON 保存，未写第二阶段报告或旧目录。

辅助查找还只读核对了以下本地目录快照；它们只作查找线索，不自行授予资格：

| 路径 | SHA256 |
|---|---|
| `artifacts/three_province_data_volume_20261021/anhui_manifest_audit.json` | `eb48d16c5452a45387d365b56c6a9523e117da2e7d370b1265c2f366f24df503` |
| `artifacts/three_province_data_volume_20261021/anhui_supplement_audit.json` | `f6dbec4cf114ac99fdd9adc9f4da2e6848561c645e3437de316effd8f93674e3` |
| `artifacts/three_province_progress_20261021/anhui_indicator_catalog.json` | `2b07174851fb41bdb2e19f0c81cb800225d5ade2b55b473b413a84ba0c3bcbe9` |
| `artifacts/three_province_progress_20261021/anhui_frequency_catalog.json` | `cf62eca52c93db556f24f56aa33ceabac7b50d8e6b6624a717a913e41da26c9e` |
| `artifacts/three_province_fields_20261026/catalog.json` | `09e8ac1a4ce90d14b8ee2687c5375418faa024e6be7ff2b74c89418499d17d39` |

## 复核入口

`pmos.recovery.anhui.recover(legacy_root)` 纯只读返回摘要报告；`baseline_queue_records(legacy_root)` 与 `resolve_queue_records(legacy_root)` 分别返回相同旧视图的 before/after `dict[source_ref, list[LegacyTask]]`，供核心兼容代码独立核验。账号别名都为 `offline-shadow-review`，后者真实参数仅在 RAM，不能直接序列化。三者均不导入旧生产模块、不创建数据库、不写任何文件，不派发任务。

新增 13 个脱敏测试覆盖旧状态守恒、source/attempt/code SHA 漂移、原 article 身份和发表日期区分、月份原件控件、portal 版本及 hidden DOM 资格、orphan blocker、精确 binding digest、私密路径/符号链接和二读漂移。Python 3.12 专项测试通过；全项目离线测试仍使用 `tools/test_offline.py` 的断网入口。
