# 第二阶段结果与仍缺的证据

本阶段把第一阶段三个代表样本扩展为最新本地目录逐项审计、更多响应形态解码和旧队列兼容。继续只有一套Engine/Ledger/Archive；三个适配器是纯离线decoder，不带各自worker、登录器或请求重放器。完整测试和代码指纹见evidence/phase_two_validation.json。

## 已完成

- 辽宁：逐170目录及185联合技术ID列明资格，普通/嵌套/handleList/宽表/动态表、带证据分页、FineReport 96点HTML、附件容器；120份留存回放。170目录78有样本、92缺证；联合来源91有样本、94缺证/待审，不能称185个独立业务指标。
- 安徽：145入口对应148任务组逐项读源；81原生model+60列表、4旧XLSX、1正文、1PDF容器、1派生参考；另52候选逐项缺证。全量source/parsed/binding/hash及原UI投影重建，准确动态数量以本轮evidence为准。可变旧binding43份单列，既有pending_review/attempting不改变。
- 山西：联合270技术来源/214本地registry行。复用已有xlrd2.0.2/olefile0.47时221来源有样本、49缺证，816/820次评估通过、4历史40302拒绝；779不同源路径中775通过。标准库806次通过、14阻断，差别完整保留。560条manifest XLSX全回放，不将额外494个未进manifest的物理文件冒充已确认任务。
- 公共工作簿读取：完整源cells、坐标、类型、style、cached值、公式及共享公式属性保留，不执行公式。ZIP在CRC解压前限制总展开/part/重复名/路径/加密；XML按实际编码拒绝DTD/实体，覆盖UTF-16绕过；空workbook、坏bool/数值、行坐标冲突拒绝。
- 旧队列6种格式和day/month/year journal；精确周期/维度/页大小/稳定fileId身份、旧ID关联、原子attempt身份落盘、跨dataset附件墓碑、跨省账号隔离。缺规则不猜字段；后续snapshot不覆盖旧unknown/denied/failed。Engine和Ledger同事务门禁禁止shadow派发。
- 同一个Engine实际跑过本轮三个省份decoder，完成解析、spool、业务归档、队列确认和重复身份拒绝；不是仅有独立纯函数测试。

## 独立复核发现并修正

复核在脱敏材料上复现并修正：辽宁FineReport形态绕过业务40302/401；安徽空越界页、NaN/Infinity、错dataset/scope/model关联和伪单位证据；山西断面日期/主体/是-否枚举漏绑定、非首动态空页和跨页size漂移；ZIP预检晚于解压、UTF16实体绕过；旧队列内层失败误成pending、孤立active丢失、路径门禁晚于建库、shadow检查竞态、不同period写法和附件捕获日期绕过去重。

这些修复有对应回归。缓存/原件/报表形态通过不代替网站长期运行或全部字段语义，代码尚未被生产进程加载。

## 本地旧状态的实际边界

临时影子索引核验辽宁13,032条旧记录视图（11,480 mapped、1,552 unmapped），安徽114,599条（112,903 mapped、1,696 unmapped），没有生成生产可执行任务。来源视图可重叠，不直接相加当唯一任务总量。具体saved/empty/pending/unknown和缺失输入见evidence/*_queue_phase2.json。

尚未补齐的事实包括：

1. 辽宁远端proof/旧outcome的业务身份、部分sitewide/advanced快照未在本地留存，94联合来源缺证；没有为补证连接远端或新请求。
2. 安徽52未准入候选、后7组的语义/PDF全文/独立源资格；43早期可变binding仍弱于不可变证据，部分周期/portal快照映射未明。
3. 山西49来源缺原件/契约或拒绝，本地214登记与后续服务器243行不是同一快照；本地没有native requests/work库和相应导出，不能称生产队列已导出迁入。
4. 三省完整Crawlee导出/所有旧失败历史、真实当前owner交接、在途请求全结算、组合UI动作的稳定子fileId、真实旁路请求审计、驱动回收与长稳。
5. PDF/Word/图片只容器或源正文、XLS/XLSX源cells通过之处不自动变成业务字段语义通过；单位外部证据resolver尚未接入的来源保持null。

以上已在清单中明确blocked/unmapped/pending，不以源码存在、空列表、文件存在、同名标题或历史status补成“全部适配完成”。当前用户授权仍是离线，生产门禁完全保留。

## 下一阶段的具体输入

先由原owner在无在途的已结算边界提供**非敏感只读导出**：旧ID、精确省/账号别名、业务/查询周期、主体/类型、分页、operation、attempt发送知识、终态、原件/元数据SHA和别名关联；不需要密码、Cookie、token或profile。将本轮unmapped逐项补证对齐，再重新影子比较。是否暂停/切换原worker以及任何网站查询，仍按ACCEPTANCE的具体预算和回滚方案另行批准。
