# 山西第四轮剩余证据核对

本轮仅查本地现有材料：网站请求0、生产写入0、旧任务新增映射0、新业务原件0。第二、三轮报告及旧山西代码/队列均未修改。结果见`evidence/shanxi_phase4_remaining.json`，纯只读入口为`pmos.recovery.shanxi_remaining.recover(root)`，顶层`counts`可供统一runner摘要。

## 19项仍缺业务原件，三条元数据链补强

| 本轮分组 | 数量 | 资格边界 |
|---|---:|---|
| 目录未核验占位 | 14 | 无精确原件或可证明别名，不按同名标题合并 |
| 目录历史权限拒绝、无raw回执 | 1 | `queryRealtimeUnitOutputPrice`继续拒绝，不能拿另一个负荷诊断查询的成功补证 |
| 目录待验证 | 1 | `queryImportantChannelActualTransfer`仍未取得原件 |
| 同一静态form关联、相关selector拒绝 | 1 | `candidate_0f5a5b87e4ea`与`menu_bf184857aa12`的关联不改旧ID或消费事实 |
| 独立回执内嵌空selector | 1 | `candidate_3b346e6f33ea`原路径仍不存在，未创建原件 |
| 仅月区间查询form | 1 | `candidate_eee315dab049`无业务原件，不推断export路径 |

`candidate_3b346e6f33ea`的`verification/20261019/channel_forecast_empty_selector_evidence.json`含精确dataset、step、请求上下文、原件SHA、size和内嵌payload。与目录逐字段核对上下文；内嵌payload按原成员顺序紧凑UTF-8编码为42字节，SHA恰好等于原回执的`ea24e6…`。这证明的是**独立回执内嵌空selector可核SHA**，不是恢复了缺失路径中的原始文件，不是业务非空样本或queue确认。没有借用其它来源相同空响应的字节，也没有写入该原路径。

`candidate_0f5a5b87e4ea`由`special_route_sources/manifest.json`及`discovered_special_form_review.json`明确绑定留存JS，实际字节SHA为`05e914…`。同SHA、query路径与selector路径均与`menu_bf184857aa12`的native契约一致，且路径可在同一JS原件中核对；不按标题推断。相关menu的selector原件及回执仍是权限拒绝。该关联可解释两个技术入口使用同一form，但不证明candidate曾发业务请求，不把menu的消费事实迁到candidate，也不解除拒绝。

`candidate_eee315dab049`的留存JS实际SHA为`57ff6d…`，与`remaining_special_source_review.json`和`special_form_contract_review.json`的精确ID及SHA相符。其契约明确为禁用的`month_range_query_only`；只补静态form证据，不虚构导出按钮、完整月份、分页或业务原件。

10/27披露审查的`findings.json`逐项对应这19个技术ID，仍无绑定的原始发布时间/业务原件链。本轮只登记该派生审查路径、pointer、SHA及状态，不把它升级成原件或语义证明。

## 旧身份与前轮边界

按第三轮固定整库SHA去重，只读7种`verification/`历史rollback快照；读取前后核SHA，header18/19须为1，wal/shm/journal一律不存在。只用`mode=ro`与`query_only=ON`查询明确requests/work业务字段的匹配计数，随后关闭。19个ID在requests dataset/contract/receipt及work scope/binding/receipt中均未找到；四条已知请求路径也无匹配。因此没有足够事实补旧任务身份，未打开活动库或WAL，也没有运行journal、backup或checkpoint操作。

42份完整历史journal中没有这19个ID。`queryMarketClearingDA/RT`各自有旧journal，仍保留其独立技术身份；目录`queryMarketClearing`的引用关系不制造新任务或消费证明。前轮7个unknown集合的数量、canonical SHA与哈希例完整保留。

另对第三轮9个仅空查询、2个空selector、1个别名、14个拒绝来源共26项定向复核原件与回执SHA及pointer。空响应与较新拒绝并列保留，原4次历史拒绝与14个拒绝来源不变。没有因同form关系、静态源文件或新成功样本解除任何拒绝/unknown。

文件名核对包含Git忽略的文件，遍历范围为state/verification/data，先剪除private/profile/auth/config/HAR等目录，未发现19个ID或realunit精确step的直接命名文件。该范围内无匹配不等于世界上没有原件。新增10/28运行capture约113MB，超本轮受控输入上限，且是新采集运行材料，未打开或用来抬高历史消费数。

## 验证

14项脱敏测试覆盖内嵌SHA与上下文必须同时相符、布尔/整数范围类型不混同、不同来源的同空字节不能补路径、成员顺序变化拒绝、queue确认不推断、首读SHA保持、私密/软链拒绝、固定第三轮成品SHA及1MiB上限、起始目录软链拒绝、忽略文件名检查、WAL不打开、历史ID匹配不自动映射以及unknown保留。测试命令：

```sh
python3.11 -m unittest discover -s tests -p test_shanxi_remaining.py -v
```

报告仅含技术ID、路径、SHA、资格和计数，不复制真实请求字段、主体选择值或业务原文。所有条目仍`dispatch_allowed=false`。
