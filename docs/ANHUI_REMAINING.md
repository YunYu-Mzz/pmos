# 安徽固定剩余项补链（2026-10-28）

本轮固定第三阶段的 20 条未映射公告、3 份旧 binding 捕获和 52 个候选，不改变旧分母。仅查已存在的本地原件、回执和快照；没有网站请求、浏览器/SSH、数据库访问、生产写入或采集/保活操作。原 `anhui.py`、第二/三阶段报告和旧省份目录均未改。

新结果见 [anhui_phase4_remaining.json](../evidence/anhui_phase4_remaining.json)：20 条旧公告身份补回 **10 条，剩 10 条**；3 份 binding 和 52 候选继续保留缺证。后 7 组没有新增业务语义资格。

## 10 条身份桥

| 固定旧来源 | 本轮补回 | 证据链 |
|---|---:|---|
| `publication_eda606edc1c9d156` | 1 | 旧 key 精确复算；同菜单 listing 原件与 parsed 双 SHA；按既有源码唯一的空白规范化规则回联唯一原生 ID |
| `publication_86e8adf7fd232a7d` | 2 | 原生 listing/source 身份链，加旧 snapshot/逐记录 facts 承诺；原 pending 不变 |
| `interregional_exchange_monthly` | 1 | 旧 discovery 的 dataset/job key/题名日期、adoption、旧 page capture 和内容寻址 article source 串联，同菜单与唯一 ID 均核对 |
| `market_disclosure_monthly` | 6 | 精确旧 key marker、dataset/key 目录和内容 SHA 原 article、题名日期及唯一原生 ID 串联 |

不是把空白近似当作独立依据。唯一使用空白规范化的条目同时满足原旧 key 算法、原菜单承诺、原件/parsed 双 SHA 和唯一 `(guid, informationId)`。规则与已检查的旧 `native_publications.py` 一致；不做模糊标题匹配，不把规范化题名重新生成新旧 key。

listing 在身份检查前先通过共同响应校验，直接/嵌套拒绝或错误不能因保留列表行而通过。原 UI 证明旗标必须为真正的 `True`；行数、页码、总数和页大小严格区分整数与 bool，并检查分页计数。

身份沿用共同核心：`operation=article`、`snapshot=undated`、真实 `record_id` 只在 RAM 中。发布日期仅用于核对旧公告身份，不变成统计期或捕获快照日期。报告只含 ID 的 SHA、技术 ID、路径、资格和计数。

剩余 10 条为 `interregional_exchange_monthly` 7、`market_disclosure_monthly` 2、`publication_4ab104c9f4b8bf37` 1。既有正文/PDF、UI 列表、网络候选形状或旧下载文件不能填补缺失的精确原生 article ID。

## 旧事实与菜单承诺

RAM before/after 均使用第三阶段原 source namespace，范围为 5 个 source views、20 条记录。原状态 pending 8、saved 12，consumed 12；旧 key、dataset、legacy_state、state、consumed、facts SHA 和保护 blocker 逐条不变。只在成功补身份时移除 `identity_mapping_required`，`attempt_index_required` 等其他保护仍保留。

`publication_86e8adf7fd232a7d` 的整份当前队列已漂移，其中一个旧 pending 已成为当前 saved。本轮没有继承这个新状态：从 `state/mz_migration_20261022_093815/remote_snapshot/state` 的旧队列找到完整旧记录，facts SHA 与第三阶段逐条承诺相同；另一个目标记录的当前完整 facts 仍吻合旧承诺。旧 snapshot 整文件 SHA **不等于**第三阶段原 view SHA，报告明确按记录核验，没有声称找回整份第三阶段冻结队列。

旧 `facts_sha256` 不包含队列顶层菜单。因此，当前队列漂移时，模块直接从第三阶段已承诺 SHA 的旧 listing 原件/parsed 双链取得唯一旧菜单，再匹配 current/snapshot 与新身份来源；不会只信当前队列的菜单。报告逐条保存 `menu_scope_sha256` 和相关旧双链引用。

保护集合摘要为 `12a8d40b48d260368e34a57c64bc25f963f6b66b8011e1d20f79c8fbcbe874c5`。10 条桥中，8 条使用整份 SHA 未变的旧队列、1 条使用精确旧 snapshot 记录、1 条使用已变整队列中 facts 完全未变的旧记录。

## 仍缺的 binding、候选和语义

3 份旧 parsed/source 仍按原 SHA 读取，预承诺 binding digest 保持不变。当前内容寻址目录没有可验证的精确副本；两份对应的可变 state binding 内容 digest 都不等于旧承诺，不能凭相似 model 升级。

本轮另定向查了既有迁移/审计快照、恢复备份、`exports/native_binding_review` 与目标数据目录。旧 review 目录中目标日的文件是原业务响应，不是承诺的 binding；已查看的快照未找到对应两个 binding basename。没有批量读取全树 JSON、提取压缩包或读取配置、认证、profile、HAR、日志、活动 SQLite。这是上述已查位置的缺证结论，不表示其他位置永久不存在副本。

52 个固定候选均保留原 surface/source_index。进一步沿当前工作清单查找，35 项能串到既有公开回执或图像引用；同菜单匹配、公开路径和已声明 SHA 分别核验。它们主要证明页面、筛选或故障边界，没有补成独立业务原件资格，52 项继续未准入，不清空失败、拒绝、未知或旧排除范围。

后 7 组的 42 份既有载体原 SHA 再次核验。另有 24 份合同汇总 parser 侧车，其 source SHA 均可绑定旧 XLSX；但 **24 份的 `export_summary_crosschecked_with_ui` 全为 false**。报告完整保留这些侧车和负证，不把它们当作 UI/导出范围一致或字段语义验收。PDF 仍保留容器资格，历史合同原 source 补链仍不等于新增字段语义通过。

## 漂移、复跑与检查

与第三阶段已登记引用相比，本轮分组登记 1 份当前队列、1 份 listing index 和 1 份已重新审阅的规则源码变化；它们不改变固定 cohort 的 key/facts 承诺。未声称当前全部采集结果等于第三阶段快照。

所有实际读取文件保留首次 SHA，二读变化立即拒绝，文件改回也不清漂移。阶段报告还检查 `pmos/evidence` 每级父目录软链、读前大小，并在末尾重核两个基线文件的首次 SHA。固定第三阶段报告 SHA 为 `a39390f063c6b31d8479d370ccf63c3157a016d49cf407c794c410107e4a8814`。

`pmos.recovery.anhui_remaining.recover(legacy_root)` 返回脱敏 `summary` 和完整引用报告；`baseline_queue_records()` / `resolve_queue_records()` 返回固定 20 条旧记录的 before/after RAM 视图。两者保留原 source_ref 和旧 key；真实选择项和 ID 不能直接序列化。函数本身不写报告、数据库或队列。

18 项脱敏专项测试通过，包括跨菜单、冲突 ID、旧 pending/当前 saved、原件篡改、直接/嵌套拒绝、假 bool、分页类型、父目录软链、基线末尾漂移及 24 份 false 负证保留。完整真实 `recover()` 另在禁止 socket、子进程、SQLite 和所有文件写入的 audit hook 下通过；独立复审也验证了 20 条旧事实与保护集合守恒。
