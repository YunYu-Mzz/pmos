# 第四轮：Monday身份与剩余证据复核

2026-10-28。本轮完成辽宁42条Monday锚点的离线身份表达，再按第三轮固定剩余集合查找三省已有原件、回执和历史快照。网站请求、生产写入均为0，没有操作现有采集、保活、浏览器、服务或旧队列。第二、三轮证据报告保持原样。

## 结果与剩余项

| 对象 | 本轮结果 | 仍缺的事实 |
|---|---|---|
| 辽宁42条Monday记录 | 42条全部可表达；41 pending、1 saved/consumed保持；原1,499个已恢复身份SHA不变 | 这些是原生周一查询锚点，不产生统计周或线上许可 |
| 辽宁11条旧身份 | 新增精确桥0，原5 empty、6 saved及消费事实保持 | 11条仍缺同时吻合旧key、查询参数和body SHA的桥 |
| 辽宁51个缺证技术来源 | 17个有明确same-request别名及目标原件链 | 34个仍无own/alias精确链；51个来源自身目录仍缺原件，新增独立原件0 |
| 安徽20条旧公告 | 补回10条身份，余10条；全部20条旧pending8、saved12、consumed12保持 | 另3份binding仍缺精确不可变副本，52个候选仍未准入 |
| 山西19项业务原件 | 补强3条元数据链，新增业务原件/旧身份映射0 | 19项仍缺业务原件，拒绝/空响应/别名与unknown边界保持 |

所有计数沿用固定技术来源或记录视图口径，不能相加当作独立业务指标或当前生产任务总数。mapped只代表离线身份可表达，所有输出仍`dispatch_allowed=false`。

## Monday身份的具体实现

`TaskIdentity` v1新增`week_start_monday`，canonical period为`{kind, anchor}`。仅接受规范的真实周一日期；拒绝非周一、时间戳、ISO周号、start/end和冲突别名。没有推算周末、改成day/snapshot或扩展生产日历seed；旧六类身份SHA不变。

辽宁resolver须核对原source rule与精确原生参数。旧ReportAdapter只允许默认page=1且不发送分页参数；其他page直接拒绝。`monday.py`固定第三轮报告SHA，重核原参数回执与writer源码SHA，再逐条对照42个query-period SHA及全部旧key、状态、consumed、facts和保护项。13,032条的原有身份、状态和81条consumed均保持；原1,552条未映射累计恢复1,541条，余11条。

历史baseline函数继续使用第二轮当时不支持Monday的表达，避免协议新增改变旧比较分母。没有重写原队列。契约及复核入口见[MONDAY_IDENTITY.md](MONDAY_IDENTITY.md)。

## 剩余证据如何判定

辽宁17条别名必须同时满足catalog和native_coverage中的明确source/route、报表number、prior_dataset及same-request声明，目标metadata endpoint也须精确对应同一原生报表，原件SHA及样本结构再通过。它们复用已有目标原件，不继承目标权限、终态、范围或语义资格。字典成功、filter/period成功而table_data拒绝的材料继续作为负证。

安徽10条由同菜单原listing source/parsed双SHA，或旧article marker、discovery/adoption/pagecapture/article source多链补回。旧key按原算法重算，原生guid/informationId必须唯一；空白规范化仅按已核SHA的旧源码契约。漂移队列不能替代旧scope：原menu从第三轮已承诺的旧source/parsed链重新锚定；两个旧记录使用留存快照恢复事实，其中当前已变saved的记录仍保留旧pending。

安徽3个精确binding副本仍未找到。52候选中35项能沿已有回执/图片继续查找，但没有新的独立业务原件资格。后7组42份旧载体重核；24份合同侧车的source SHA与原XLSX相连，但UI/export crosscheck均为false，语义资格不提升。当前队列、列表index和源码相对前轮的变化分别列入漂移组，不计作新恢复记录。

山西补强的是：独立回执内嵌空selector重现原SHA；相同静态form及query/selector路径关联且相关selector继续拒绝；禁用的月区间查询form。没有生成缺失原件、虚构export或转移消费事实。前轮26项空/别名/拒绝边界逐回执重核；7种历史verification库与42个完整journal未给19个ID补出新身份。4次历史拒绝、14个拒绝来源和7个unknown集合全部保留。

## 验证与可复跑入口

离线回归302项，301通过、1项可选浏览器测试跳过。三省真实剩余复核在禁止网络、子进程和文件写入的审计钩子下通过；仅放行山西verification静态rollback库的7次`mode=ro`连接。没有连接当前活动SQLite。

源码目录、Python3.11+：

```sh
python3 tools/test_offline.py
python3 tools/recover_existing_evidence.py all --stage remaining
```

此命令先验证辽宁Monday，再运行三省`*_remaining.recover`，默认不写文件。原`--stage retained`入口继续保留；它复核旧输入，遇已漂移源码或材料可拒绝，不替换历史cohort。可选输出仍只允许PMOS var下不存在的新目录，软链不能转向旧省目录。

采集持续运行，报告不冻结它。受控输入首次SHA固定、重读变化拒绝、结束重核；不同稳定复核时点的列表index SHA或引用数可能不同，而旧cohort与状态保持。安徽本轮两次稳定复核已观察到此类index变化，恢复数均为10，未把当前新采集计入旧集合。报告仅含路径、技术ID、资格、状态和SHA，不含真实参数、标题、主体值、article ID或业务原文。

- [辽宁剩余证据](LIAONING_REMAINING.md) · [安徽剩余证据](ANHUI_REMAINING.md) · [山西剩余证据](SHANXI_REMAINING.md)
- [Monday逐项报告](../evidence/liaoning_phase4_monday.json) · [本轮验证和代码指纹](../evidence/phase_four_validation.json)

后续缺项仍须精确来源/查询/原件链、旧身份桥或字段语义证据；本轮不通过新网站请求补取，不解除任何生产保护或实施owner切换。
