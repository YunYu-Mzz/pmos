# 三省代码审阅与复用决定

核验日期：2026-10-27，北京时间。仅本地源码、最新交接说明和留存材料；没有 SSH、CDP、站点请求或实时健康检查。根 README 的早期辽宁单负荷/180秒/批次限制和 10-20 山西评审中的60秒均已过时。最新保留授权参考为辽宁15秒、安徽20秒、山西15秒，空闲保活均有300秒相关记录；此处和离线配置都不改变生产策略，也不是新的线上授权。

证据文件 `../evidence/phase_one_review.json` 登记34份代码/文档/历史回执的路径及SHA，以及9份真实原件的路径、源SHA、解析摘要SHA和样本边界。原始数据未复制进本项目。文档内的运行PID和成功数只是历史核验时点，不在本次验证存活。

## 既有 PMOS 试验的事实

必须先看 `pmos/live_test_reports/shanxi_20261023/result.json` 和山西 HANDOFF/AGENTS 中2026-10-23旧代码恢复记录：旧试验40项离线通过，首轮 probe 因 `request_template_missing` 在发送前失败，台账记了1次probe，但实际发送0、采集日期0、保活0、观察0分钟；随后按用户要求结束，旧采集恢复。回执和原阻断保持原样，本次未启用它。

本轮开始时现存代码已经有缺模板等待、bootstrap、64项测试，不能把这些当试验成功。基线64项测试通过、其中1项本地浏览器测试因缺Playwright跳过。发现的具体问题与决定：

| 能力 | 保留理由 | 本次修复或替换 | 仍未证明 |
|---|---|---|---|
| 可注入transport的Engine、独立Ledger/Archive | 已有顺序、节流、故障和归档测试；无需另造共同框架 | 三省固定夹具都走同一个Engine，decoder显式注入 | 真实网站多步骤操作完整接入 |
| 请求前attempt提交 | SQLite FULL/WAL、pending启动保护可用 | 已派发身份永久视为已消费；成功响应先存responses再finish；坏二进制进quarantine；归档/确认之间崩溃不重发 | 旧队列全部身份映射及生产崩溃恢复 |
| 缺模板等待 | 当前代码已优于失败时版本 | 发送前再次检查fetch就绪，缺模板不记假发送；保留旧故障回归；删除无源码证据的山西负荷bootstrap | 模板首轮取得、跨会话失效、当前原生导出 |
| JSON/Excel/PDF容器检查 | XLS与XLSX分开，原件SHA归档可用 | 嵌套业务错误、二进制MIME里的JSON错误、伪捕获HTTP401均不能漏判 | 复杂工作簿全值校验、扫描件OCR及完整附件覆盖 |
| 省份JSON规格 | 435个历史定义可读、可构建，来源仍可追溯 | 线上启用资格与历史资格分开；enabled归零；只选明确样本增加offline契约 | 435项在统一运行时的权限、筛选和参数真实性 |
| 省份策略 | response-end节流可复用 | 取消代码层30秒统一下限；参考配置15/20/15，数值检查仍严格 | 不同请求类型的真实伴随请求审计 |
| attach-only边界 | 不主动launch/close/refresh的方向可保留 | CLI与service模板硬性阻止本阶段上线；本地UI测试改显式选择 | 现有旧owner与新owner的原子交接，不同root对同账号互斥 |
| Playwright模板缓存 | 页面证据应只在RAM | TTL/容量/已分离frame淘汰；不保留请求体；提供监听器detach | Node私有Dispatcher/Route/Future回收、所有异步监听任务上限和完整退出清理 |
| 统计与汇报 | 唯一中央收件/飞书写入链路已存在 | 新统计短只读连接、限制批量、返回前close；复现DELETE journal长游标阻写；提供schema2指标字段handoff | 完整metrics载荷、签名配对、SMTP/IMAP/飞书逐格闭环 |

不复用一次性热修复/恢复/安装器。旧源码里的内部SDK补丁有精确版本、原进程和已消耗授权约束，不能复制到统一框架后自动执行。

## 逐项差异与待补能力

下列路径均相对于根仓库。表中“已实现”指相应旧代码存在，不等于本次重新验收其线上版本。

| 能力 | 辽宁 | 安徽 | 山西 | 共同边界与第一阶段状态 |
|---|---|---|---|---|
| 配置与凭据 | `lncrawler/config.py`、browser_owner；本地/阿里云授权历史不同 | Mac独立Edge/代理，活动汇报副本在Application Support | 腾讯独立浏览器和采集服务，部署模板与实际用户路径有差异 | 公共包+独立省/账号/实例root；真实凭据不读不迁，样例只放占位路径 |
| 会话附加与认证 | 原生产/辅助页面、两层Web Locks，browser_owner后续改造 | 独立keeper和resident适配worker共享server_runner.lock | existing_chrome校验已有profile/端口/唯一页面，Inspection窗口 | attach必须在owner仲裁后；登录、SDK、业务认证、续期证据分开，当前线上入口关闭 |
| 原生导出/模板 | Vue报表、native SDK、FineReport多步链路 | 原生UI、observed native、网页有值但导出0字节；依赖下拉 | observed fetch、原生XLS/XLSX、加密动态表和原生分页 | 不把三种变同一fetch；本次只解析已保存JSON，旧browser传输保持实验性 |
| 请求与响应后节流 | 15秒新授权覆盖原30/60/180；历史自动映射漏计需保留 | 20秒业务、keeper独立300秒，并非无条件统一同一间隔 | 15秒单worker，导航/字典/时间请求也须记实际活动 | 共用仲裁/台账，适配器必须拆出每个真实网络步骤；本次假时钟验证 |
| 单worker/所有权 | 完整metric turn、owner锁、Web Locks、多个lane交替 | worker/keeper分进程，操作锁共享，不重复连接 | 主/唯一协调器各自职责，owner/runner/Inspection锁不同 | 一省一故障域；同账号不同root也必须共享owner门禁，统一版本尚未实现现场交接 |
| 去重与未知结果 | 旧unknown节点、历史用电、拒绝lane不能重播 | attempted/pending_review、旧UI/API页高亮未确认不能重查 | native_json_queue先leased/sent，失败/denied_scope不再lease | 本次consumed保护跨collect/sample、分页身份含页；旧队列只读，不自动seed迁移 |
| 保活 | local hold可保活，真实认证/未知停止；后端armed可能陈旧 | 独立keeper、同故障链归属、96点基准，持续业务推迟额外心跳 | TIME模板TTL90与300秒空闲不匹配曾失败，已核BASE导出是另一证据 | 本次缺模板不发送；local允许名单、pending优先；TIME成功不等于续期验证 |
| 驱动/浏览器内存 | cgroup与RSS口径、主机余量/回差；真实OOM根因并未全清 | resident连接减少重复附加，但不继承山西驱动修复证明 | 已定位buffer、Future、Route和重复utility上下文；最终v3仅30分钟有限验证 | 本次缓存有界、纯资源准入测试；未接入实际GC/私有驱动回收，不宣称长稳 |
| 错误分类/隔离 | menu missing不等于permission；HTTP200业务40302保留 | 空导出、空选项、local renderer、global session分开 | 精确40302按来源隔离、500/data=null不是空成功 | 显式policy且HTTP200/40302才允许正式采集dataset隔离；泛HTTP403仍保护；认证/未知不放行 |
| 原件与校验 | 原始response+元数据、quality侧车，多来源重复版本保留 | Excel与网页源、原生JSON、正文/图像各有角色 | 内容SHA、原生XLS、分页JSON、receipts绑定 | 原bytes不改；responses/quarantine/业务original分别统计，0字节不计有效文件 |
| 解析/结构化 | native_envelope、report_contracts/field_quality，单位冲突明确 | native_curve严格96字段；native_tables映射全表，特殊多表不强并 | native_menu_json及源绑定decoder；真实总数与pages可能冲突 | 本次两个曲线形态+一种分页表；保留0/负值/null，不推单位/披露语义 |
| 覆盖 | 节点/类型按日期字典frontier，历史月分页 | 148队列就绪不等于全年完成，150报告行不等于原生验收组数 | 243报告行、分页份数与日期不同，缺用户编号不可用日期顶替 | 列表、附件、记录、身份、文件、周期、范围分别统计；本次只样本 |
| 邮件 | 独立sender，只读，后端keeper为unknown | 独立Mac活动release/launchd，源代码修改不等于已加载 | 唯一schema2/HMAC收件和Feishu写入端，写后读回 | 统一只交handoff，不创建第二收件/飞书端；本次没有发信或写外部应用 |
| 飞书/披露时间 | 明细稳定身份、审核列独立 | 同指标scope/单位及日期证据保留 | 969行审核版、未知写入先读回、批内按省合并 | 不从菜单/字段名猜首发，规则截止/更新时间/实际首发分别建模；本次不迁移这些审核规则 |

## 样本结论与缺证

- 辽宁：`local_collection/system_load`三个留存日期，各96点可重建；无单位推断。不能据此宣布最新139技术来源、节点/类型和所有历史队列已迁移。
- 安徽：`native_curves/province_system_load_forecast`三个留存日期，各96点可重建；旧模块有MW绑定证据，但本阶段契约未携带完整UI单位证据，故统一输出unit=null。不是全省实际负荷，也不继承默认线路/机组范围。
- 山西：`native_query/menu_af7397f1e57d`三个日期，每页3行；字段和值完整保留，分页计数通过。只是对应源的单页样本，不是全目录/全主体/全年通过，也不证明负荷导出或TIME模板问题已在线解决。

所有三省均标“共同流程离线通过；所选原件仅样本通过；线上验收待批准”。陕西和冀北只在五省总体方案预留能力，不读取其不稳定接口或生成猜测配置。
