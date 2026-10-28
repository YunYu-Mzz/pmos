# 原生 Monday 查询锚点的离线身份

2026-10-28。`TaskIdentity` v1 增加 `week_start_monday` 类型，表达旧来源原生请求中的周一日期。以下为脱敏示意的 period 部分：

```json
{"kind":"week_start_monday","anchor":"2025-12-29"}
```

它只表达查询锚点。没有生成周末、周起止、ISO 周号、统计期或调度范围。原 `week`、`range` 仍要求实际 start/end；`day`、`snapshot` 等原有 canonical 文档及身份 SHA 不变。类型字段将这些身份区分，账号、省份、技术来源、operation、typed dimensions、页码也继续参与身份。

输入可以使用 `label` 或 `anchor`，规范化后一律是 `anchor`；同时出现时必须一致。锚点须为真实且规范的 `YYYY-MM-DD` 周一。非周一、紧凑日期、ISO 周字符串、时间戳、空值、额外字段、start/end 均拒绝，不自动纠正。

## 辽宁42条的证据与守恒

恢复函数要求既有 source rule 明确声明同一 `week_start_monday` 类型，再逐项核对原生 period 字段、精确参数集合、周期和选择项。普通 day 声明、只有日期、snapshot 或 engine outcome 兜底都不能被当作 Monday 证明。源码只 AST 读取；不运行旧采集器。

旧 `ReportAdapter.params` 对此类非分页报表只允许默认 page=1，实际请求不含 page。旧 job 中该默认值不另造分页身份；page=2、0、布尔值等未审阅分页直接拒绝，不静默丢弃。

`pmos/recovery/monday.py` 进一步逐条对照已保存第三轮报告：

- 固定原5个 source view 的 SHA、原1,552条缺映射记录 cohort，核验旧 key hash、dataset、state、consumed 和 facts SHA。
- 原1,499个已补身份 SHA 与 blocker 保持不变；原来42个协议缺项的 query-period SHA 必须等于新 anchor 的 canonical SHA。
- 每条旧参数回执/source rule 的证据 SHA 继续匹配；只去掉已经满足的映射 blocker，其他 hold、owner、stage、拒绝和未知事实不变。
- 原11,480个已映射记录也由共同守恒检查覆盖。13,032条的状态仍为12,951 pending、74 saved、6 empty、1 unknown，consumed仍为81。

42条全部可表达，其中41条仍是pending，1条仍是saved且consumed。原1,552条缺映射中累计补回1,541条，余11条缺精确旧 key→查询桥。两种来源视图中的同一查询锚点不按文件份数当成独立任务。

历史第二、三轮报告不改写。`baseline_queue_records` 明确保留第二轮当时不支持 Monday 的解码结果，使旧1,552条分母和事实摘要不因协议扩展改变。当前 resolver 才补入新身份；本轮报告另存 `evidence/liaoning_phase4_monday.json`。

## 离线入口与边界

源码 checkout 使用 Python3.11+：

```sh
python3 tools/recover_existing_evidence.py liaoning --stage remaining
```

此入口先核验 Monday cohort，再检查剩余证据；默认不写报告。`legacy-review --period-kind week_start_monday` 只增加明确的离线类型选择，仍受新影子目录、账号和不可派发门禁限制。本轮没有扩展 DatasetSpec 的日历 seed 或生产任务调度。

脱敏测试覆盖 canonical/跨年锚点、旧六类身份 SHA、不同类型/账号/维度/操作/页码隔离、错参数和伪日期、旧记录守恒、保护项及影子派发拒绝。真实原件不复制入 Git，网站请求和生产写入均为0；身份映射不授予线上资格。
