# 山西第三轮本地只读补证

本轮只查已有原件、业务回执和历史快照，网站请求为0，生产目录写入为0。没有连接SSH、浏览器或服务器，没有操作采集、保活、owner、队列或服务。新报告是`evidence/shanxi_phase3_recovery.json`，第二阶段报告保持原样。报告只含路径、技术ID、大小、SHA和计数，不复制业务原文、主体选择值或认证材料。

## 49项原缺证来源的具体结果

基线仍为270个技术来源、221个来源有样本、49项缺证或依赖，历史4次40302继续保留。此次为原49项新增分层证据，**没有改动第二阶段资格，没有增加可调度任务**。

| 本轮发现 | 来源数 | 含义 |
|---|---:|---|
| 非空原件链 | 4 | 3个特殊报表及1个原生menu查询；仍待范围、分页和语义审阅 |
| 仅空查询证据 | 9 | 8个候选旧查询和月度系统负荷预测的两次空查询；不能推断全年无数据 |
| 仅空selector | 2 | 该次日期无可选范围，不能确认业务数据任务 |
| 显式引用既有样本 | 1 | `queryMarketClearing`目录明确引用DA/RT两个来源，不计为新独立业务指标或新原件 |
| 拒绝回执 | 14 | 12个候选查询、2个menu selector；保留旧空响应和新拒绝各自的证据时点 |
| 仍缺绑定原件 | 19 | 不以标题相同、文件存在或目录状态代替证明 |

三个特殊来源为`candidate_76eab00603c1`、`candidate_0fb628cbe7fc`、`candidate_d4883f1a28bf`。`dataset_catalog.json`的明确JSON pointer将技术ID、请求范围、HTTP状态、业务状态、原件路径、SHA和大小相连。已核对3份query及6份UI/程序XLS或XLSX原件的字节SHA；前两个是当前快照，第三个是2026年度样本。它们的查询第一页各20行，不代表分页全集。未重新解析完整工作簿，也未声称完整历史或主体覆盖。

`menu_7ce44d6beda3`的新样本由`verification/20261021/native_remaining_scoped/evidence_sync_review.json`中的query/selector回执绑定。成功样本不会消除原有拒绝记录。12条较新拒绝来自`plain_empty_refresh/runtime_latest.json`，另两条来自`menu_native_scope_refresh/evidence_sync_review.json`；14条原件SHA均匹配。空selector两条由`native_section_scope/runtime_latest.json`绑定。

月度系统负荷预测的两条回执在`verification/20261013/additional_three_samples_snapshot.json`，通过目录明确endpoint和SHA相连。各来源证据的`receipt_context_sha256`含观察时间和请求上下文，只作回执上下文摘要，**不是稳定任务身份**。稳定身份、消费状态和生产许可均未据此生成。

19项仍缺绑定原件是：`candidate_0f5a5b87e4ea`、`candidate_3b346e6f33ea`、`candidate_eee315dab049`、`queryCurrentGensetInstall`、`queryCurrentSocietyIndustriesUse`、`queryDaySystemLoadPredictionFinish`、`queryGensetInstall`、`queryGreenPowerTrade`、`queryImportantChannelActualTransfer`、`queryMarketQuotation`、`queryMarketSettlement`、`queryMediumLongTermTrade`、`queryRealtimeUnitOutputPrice`、`queryRetailMarketTrade`、`queryRetailMealSign`、`queryRetailTimePrice`、`queryRqscjjcqfslj`、`queryShareReturnStandard`、`querySocietyIndustriesUse`。其中`candidate_3b346e6f33ea`的目录虽含明确selector回执，所引原始文件本地不存在；不能拿其他来源相同空响应的字节补这个路径。目录中的拒绝/待核验状态继续保留。

## 找到的native历史库

第二阶段“本地未留存native requests/work库”应由本轮新发现补充：库存在于`verification/`历史验证目录。10个物理快照按整库SHA去重后为7种，不能按文件名或复制份数相加。

| 快照 | 表 | 行数 | 状态 |
|---|---|---:|---|
| 10/21 native_json_queue | requests | 2,061 | complete17 / pending2044 |
| 10/21 native_node_queue | requests | 2,091 | complete41 / pending2050 |
| 10/21 native_flat_curve | requests | 2,065 | complete21 / pending2044 |
| 10/21 native_remaining_scoped | requests | 2,103 | complete50 / leased1 / pending2052 |
| 10/25 pause_repair JSON | requests | 4,587 | complete178 / denied4 / denied_scope1872 / pending2533 |
| 10/25 publication（三份相同SHA） | work | 1,407 | complete277 / failed1 / pending1129 |
| 10/25 resume JSON（两份相同SHA） | requests | 4,912 | complete504 / denied4 / denied_scope1872 / pending2532 |

所有已读取文件均为SQLite header18/19等于1的rollback格式，旁边无wal/shm/journal。使用`mode=ro`、`query_only=ON`短连接，只读requests/work表的schema、identity/status及聚合；批次最多128行，连接关闭后才哈希和处理。读取前后核对整库SHA与sidecar。没有运行checkpoint、backup、immutable或任何journal操作，也没有打开当前生产库。WAL、未知header、sidecar或明确backup SHA失配时不连接。

10/21四份备份回执的`/queue_sha256`或`/sha256`与对应库整SHA实际相符，报告逐条登记回执pointer。10/25三种库虽有同目录历史恢复review/initial文件，未找到独立库SHA，明确降级为`same_directory_review_without_independent_database_sha`，不称精确备份哈希回执。

7种整库SHA产生19,226个`(snapshot SHA, table, old identity)`记录视图；合并为6,319个`(table, old identity)`后，902个身份出现跨快照状态变化，1,878个身份包含必须保留的leased/failed/denied/denied_scope事实。包括1个complete|leased、870个denied_scope|pending、3个denied|pending。所有状态按集合并列保留；后来的complete或pending不能消掉旧在途或拒绝。本轮只是历史状态检查，尚未补完所有业务维度到Engine身份的映射，也不是当前服务器任务总数。

## Crawlee历史journal及manifest外文件

`verification/20261025/pause_repair_1719/queues_before.json`嵌入42份完整journal，含10,925个旧任务键：8356 saved、1 empty、2561 pending、7 unknown。遗漏granularity的4份使用原`DailyRequestQueue`源码明确的day默认，月度5份保留month。

先列`state/tencent_redeploy_20261014.P8SetV/tencent_redeploy_progress_20261014.tar.gz`成员，再只读其中明确的5份业务journal，没有解包其他文件到磁盘。加入这5个较早视图后，共47个journal内容canonical SHA、12,360个任务视图，唯一旧任务键仍为10,925。存在1,201个pending|saved历史状态集合及7个unknown集合，不能把各视图状态相加当当前待办。报告存集合canonical SHA、数量与最多8个哈希例，完整集合可由同一纯只读函数复跑核对。

`resume_hold_2104`两处`queue_journal_sha256.json`只有42个digest，不是journal内容，不增加任务数，也不据此重造快照。原Crawlee storage/request queue的完整导出仍未被证明。

manifest维持560条原件路径。`data/2026/original`有1054个物理工作簿，其中494个不在manifest。本轮逐一登记这些文件的路径与字节SHA，消费证明一律false；不通过父目录名、标题相同或存在文件来宣布已完成任务。另列8份10/22历史交接tar成员，未发现目标技术ID或queue/journal成员，未提取正文。`complete_coverage.json`中19项剩余缺项也仍是占位、权限拒绝或待核验，没有据粗粒度覆盖表创造别名。

## 可复跑与验证

在仓库根目录用Python3.11+调用`pmos.recovery.shanxi.recover(root)`可返回同样报告；函数不写文件，不导入省份runner，不发请求。报告约304KB，连续两次本地运行结果一致。不要将报告输出到旧省份目录或生产state。

`python3.11 -m unittest discover -s tests -p test_shanxi_recovery.py -v`通过11项脱敏测试，覆盖source/SHA绑定、空数据不升级、认证与权限区分、嵌套错误和bool状态、路径/软链拒绝、WAL/sidecar/备份SHA失配不打开、相同整库SHA去重、较新pending不抹掉旧拒绝或leased、acknowledged reservation仍unknown。
