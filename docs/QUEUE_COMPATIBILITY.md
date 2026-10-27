# 第二阶段旧队列兼容：只读规范化与离线影子索引

本阶段实现真实旧格式的兼容读取、身份关联、防重放及新库影子索引。没有运行旧队列的open/reserve/reclaim/confirm/acknowledge，没有冻结或导入生产库，没有改变旧pending、unknown、denied或任何保活状态。

## 支持格式和证据来源

| 格式 | 读取内容 | 必须保留 |
|---|---|---|
| 三省Crawlee batch_journal v1 | dates/outcomes/batch.confirmed、reserved/interrupted/acknowledged、day/month/year子类 | 无outcome的已预留日期即使acknowledged也不能变成未尝试；已存saved/empty冲突转unknown |
| 辽宁advanced tasks | datasets.tasks/outcomes、active、suspended | 包括不在tasks中的旧outcome、孤立active，内层failed/attempted/issued/sent不能丢 |
| 辽宁verified-source frontier | sources.pending/outcomes/hold、helper_hold、先前样本prior_outcomes | 列表snapshot_date与统计期分开；source hold和全局hold保留，不能只取待办列表 |
| 辽宁原生引擎frontier | pending/active.job/active.stage/outcomes/hold | 未结束parameters/load_content/load_report_content阶段作为未结事实保留，不从中间阶段重发 |
| 安徽jobs + native_attempts | 原jobs与独立attempting/captured/pending_review/failed索引联合 | attempted/receipt/原件/错误reason优先于表面pending；orphan attempts仍保留；缺attempt索引必须标记 |
| 山西native requests SQLite | identity/body/contract/date_fields/page/status | 页码须与body一致；pageSize有已审阅契约，排除UI total/pages记账字段；sent/leased/failed/denied_scope不返还 |
| 山西publication work SQLite | kind/scope/command/status | 列表scope是menu，不是日期；附件按fileId；缺发布查询capture anchor时列表不映射成伪统计日 |

只读代码位于 `queue_compat.py`，本地受控快照审计位于 `queue_audit.py`。不导入三省生产模块或Crawlee服务。

## 身份和防重放

TaskIdentity统一包含省份、账号别名、dataset、规范周期、类型保真的维度、页码和operation。等价的日/月/年表示规范成同一key；快照时间可用有时区ISO瞬间规范成UTC，或保留明确的opaque锚点；混合冲突字段拒绝。

- 辽宁选择项的name/type/mtype等只按实际源码中声明的period_field/selection_field映射，不把字段标签猜成相同含义。缺source rule时保持unmapped。真实类型1与字符串"1"始终不同。
- Engine支持经过审阅的 `spec.source.identity_contract`，从实际request_data_path/date_fields构造同样的业务维度，分页包含_page_size；规则与实际pagination大小冲突即拒绝。测试验证旧decoder与同一Engine key一致。
- 已派发业务请求的identity在发送前与attempt同事务落盘。即使归档/确认崩溃、人工将attempt结为unknown或改scope别名，都不能把它当新任务重发。
- 真正按fileId去重的附件使用稳定undated/file_id身份；捕获时间、文章标题和推断周期不参与重下载身份。按省份/账号建立file_tombstone，可阻止换dataset别名重发未知/拒绝文件，账号间隔离。
- 旧UI record_only一次动作可能返回多个附件且缺稳定子fileId。这条实验传输只保留article/record_id身份，**不能声称所有UI子附件全局去重已闭环**；上线前必须补原生子ID或保持禁止派发，不以文件名代替ID。

本阶段对第一阶段尚未生产使用的v1草案增加了周期规范化和执行台账绑定；第一阶段evidence中的演示key是历史测试值，不能拿它当线上迁移后的旧key。生产旧ID始终保存在legacy_records，不按新hash覆盖旧事实。

## 影子索引的强制边界

`import_shadow`仅向新PMOS本地Ledger写migration_sources、legacy_records和已消费文件墓碑。它不seed可执行tasks，不将pending设置为可运行，不取得旧owner。

- 同一snapshot重复导入幂等；相同snapshot下擅改映射拒绝。新的snapshot只追加，不覆盖旧unknown/failed/denied。
- 省份/账号在一个Ledger中固定，跨省跨账号混写拒绝；旧三省目录在mkdir/SQLite打开前就被路径门禁拒绝，symlink解析后也不能绕过。
- Engine的tick/seed/request，以及Ledger.begin同一事务内都检查shadow门禁。没有正式owner交接的影子库连keepalive/probe也不能派发，防止检查与导入之间竞态。
- 记录缺失周期、主体映射、页大小、旧原件路径等证据时保留unmapped/held。**mapped只表示离线身份可表达，不是上线许可。**

## 只读与锁

JSON按明确已知路径读取，核对SHA和受控大小，读取后检查是否变化。源采集仍可正常更新原队列，所以审计是滚动视图，不是原子冻结。

SQLite只读取明确requests/work表，最多128行一批，立即关闭连接，再解析JSON或做文件I/O，不改变journal模式、不checkpoint、不运行backup/immutable。WAL或未知格式拒绝并要求另行提供已冻结的rollback格式导出，避免mode=ro仍创建/更新共享内存sidecar；不为证明兼容而连接当前生产库。

## 本地真实快照结果

精确计数见各省`evidence/*_queue_phase2.json`：

- 辽宁13,032条旧记录视图已规范化并在临时影子库核验；11,480条可映射，1,552条因旧outcome缺业务身份/本地原件关联或未登记参数规则仍保持unmapped。含74 saved、6 empty、1 unknown及12,951 pending；这不是当前服务器实时任务总量。
- 安徽114,599条记录视图，112,903可映射、1,696仍缺明确周期/快照/范围关联；保留14,170 saved、131 unknown、100,298 pending。各group/快照可重叠，不能当唯一任务数或新请求数。
- 山西本地未留存native requests/work库，未访问服务器补取；相应缺证列入报告。格式与状态兼容已用脱敏SQLite夹具验证，不能称真实生产队列迁移完成。

上述索引均在临时目录验证后删除；真实旧队列未改。缺失的advanced/sitewide快照、Crawlee存储及山西库没有被伪造。后续需原owner在安全边界导出非敏感身份/终态/分页frontier及别名关联，才可完成这些具体缺项。

## 可运行命令

在PMOS源码目录，用Python3.11+：

```sh
python3 -m pmos legacy-audit liaoning
python3 -m pmos legacy-audit anhui
python3 -m pmos legacy-audit shanxi
python3 -m pmos legacy-review shanxi journal /controlled/batch_journal.json \
  --sha256 EXPECTED_SHA256 --account-alias offline-review --period-kind day --operation export
```

指定`--report var/review.json`可保存只读审计结果，必须是新文件且只能写PMOS var目录。`legacy-review --shadow-dir var/new-shadow`建立新的非派发影子库；目录必须不存在。operation必须按原实际query/export/原生动作审阅，不能由队列文件名猜；可通过`--operation`或mapping指定。`mapped`只表达该声明，不能替代调用链证明。jobs格式应提供`--attempts`及`--attempts-sha256`；参数别名映射使用`--mapping`及对应SHA，例子见examples/queue_mapping.example.json。山西SQLite格式分别为sqlite-requests/sqlite-publication。

线上交接仍须另行批准：先固定并核对旧未消费集合，补齐unmapped，持原owner锁交接；有未知请求时保持hold。回滚也必须保留新旧全部消费事实，不能恢复旧代码后重新seed。
