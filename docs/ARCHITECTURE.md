> 第二阶段已扩展三省decoder、共用workbooks和旧队列影子兼容；最新逐项资格见PHASE_TWO.md与QUEUE_COMPATIBILITY.md。下文生产目标仍未获准切换。

# 五省统一架构、迁移与回滚

本方案以现有PMOS为唯一公共引擎。第一阶段只有辽宁、安徽、山西的离线实现；陕西、冀北是后续适配目标，没有迁入配置或队列。共用代码不共用登录上下文、账号凭据、出口、队列、锁、原件或故障。

## 模块边界与目录

```text
pmos/
  pmos/
    engine.py             共用顺序引擎；transport/decoder注入
    ledger.py             本实例任务/attempt；发送前提交；已消费保护
    contracts.py          版本化业务身份、ParsedArtifact、无凭据契约
    adapters/             native基础形态及辽/皖/晋源绑定decoder；不生成网站参数
    workbooks.py          共用XLSX源cells读取，结构和大小校验
    queue_compat.py       六类旧队列只读规范化、身份映射、非派发shadow
    queue_audit.py        真实本地旧快照的只读审计
    province.py/spec.py   省份历史目录及规格；不授予线上资格
    pacing.py/faults.py   响应后节流、预算、错误作用域
    archive.py/validate.py 容器/原件SHA、responses/quarantine与original
    offline.py            临时实例演示、受控原件只读回放
    legacy.py             显式旧manifest映射；无队列写入
    resources.py          纯资源准入判定，无GC/重连副作用
    reporting.py          短读快照、schema2指标字段handoff，无网络发送
    browser/              既有实验传输；本阶段不能由CLI启用
  provinces/{liaoning,anhui,shanxi}/
    province.json         非敏感参考策略、release=offline_only
    offline.json          本次可回放形态、来源代码、资格
    datasets/ scripts/    既有435规格及页面胶水，待逐项重验
  tests/fixtures/         固定脱敏JSON；原有本地HTML夹具
  evidence/               代码指纹、样本摘要、测试结果，不存生产原件
  examples/               安全配置样例
  docs/ tools/ deploy/    规范、离线测试入口、禁用的历史service模板
```

生产目标：`/private/pmos/<province>/<account-alias>/<instance>/`各自包含ledger、state、original、responses、quarantine、samples；凭据和profile放独立私密目录，权限0700/0600。当前 `Province.instance()`仍是 `PMOS_DATA/<province>`，上线前必须显式配置每个账号独立PMOS_DATA并完成跨root同账号owner协议；不能谎称多账号互斥已做好。

## 接口契约

| 接口 | 输入/输出 | 不变量与本次边界 |
|---|---|---|
| TaskIdentity v1 | province/account_alias/dataset/period(kind及真实起止)/dimensions/page/operation → canonical SHA | 数字ID与字符串ID不同；页码独立；session/template修订放receipt，不能换身份绕过去重。Engine/attempt现已接入规范v1，旧(dataset,period,scope)键保留兼容；source identity_contract、页大小和附件墓碑有跨层测试；仍有明确unmapped旧记录，完整对照是上线门槛 |
| ProvinceAdapter | reviewed source contract + raw response → ParsedArtifact | 保留原字段、0/负值/缺值、源坐标或source_field；未知单位null；本轮辽/皖/晋已分纯decoder，支持更多源绑定model、分页、源正文和cells，资格逐项分层；不能强塞通用fetch |
| Transport | send(explicit request) → Response(status/content_type/body/parts) | 当前fake可运行；未来需ObservedOperation(真实请求集合、attempt、起止、sending knowledge)包住UI/native多步，所有旁路请求均先仲裁。本阶段不宣称UI点击=1次网站请求 |
| Ledger | durable begin → receive/spool → finish → archive/confirm | pending优先阻断；任何已派发业务身份即使failed/unknown/成功未confirm也不可自动重发；分页scope含页；网络端不宣称exactly-once |
| Archive | 原bytes + receipt → SHA/path/size | 文件fsync+目录fsync+manifestfsync；坏文件进quarantine；业务有效original与spool不重复计数；空响应原文也保存，0字节不当有效原件 |
| SessionOwner（目标） | province/account/profile绑定、lease、generation → granted operation | 同账号的旧worker、新worker、keeper、适配必须共用owner；一个新root里的flock不足以排除旧worker。本次测试只证明同实例不能有第二worker |
| SessionEvidence（目标） | transport_connected、business_authenticated、renewal_verified、observed_at、generation、evidence_ref | 分开unknown/stale/verified；TIME或SDK成功不推导真实业务登录及续期。本次没有在线会话证明 |
| ResourceAdmission | cgroup计费、driver RSS、各自限额、inflight/keeper_pending → admit/wait/hold | 不把Chrome RSS简单加和当cgroup；不动在途，不重连/关闭profile；纯逻辑已测，实际资源采样和维护尚待接入 |
| ReportHandoff | 只读验证后源receipt → schema2 coverage指标字段 | 唯一中央sender/receiver/writer另行接收；完整metrics、HMAC、SMTP、IMAP、Feishu及读回状态分别验收。本次只本地字段，不发信、不写飞书 |

旧queue与新identity关联还需保留manifest/reference、旧任务ID、source SHA、模板/页面代次、query/export实际attempt、空/非空及解析资格。不得只按文件SHA合并不同范围；同字节可对应不同业务身份。

## 错误和保活策略

优先序：真实认证/风控或全局未知保护 ＞ 未结束请求 ＞ owner/在途仲裁 ＞ 人工停止边界 ＞ 省份策略 ＞ 数据集调度。已证实本地等待不能清除更高优先级保护。

| 分类 | 动作 | 保活 |
|---|---|---|
| auth、HTTP401、业务401/10003、可见注销 | 全省保护，保留浏览器和证据 | 停止；等待新授权/人工登录与基准验证 |
| 精确HTTP200/40302 | 样本隔离来源；正式采集仅在明确省份策略下隔离对应dataset/scope | 仅健康范围、无未知/在途、owner批准时允许；泛HTTP403仍全局保护 |
| rate/network/unknown、HTTP200业务500/未知码 | 保留attempt和未明结果，不自动重放 | 停止；同请求的有界被动观察也需要单独契约，不是重发 |
| 正常业务响应但文件坏、结构不匹配 | 原bytes保留quarantine，隔离来源；不确认为成功 | 健康且未占用共享锁时可继续，首个新故障立即保护 |
| 缺模板、已证实零发送的本地frame等待 | 不创建假attempt，不自动bootstrap；等待证据 | 只用已有合格独立keeper，不靠新的模板请求救场 |
| 泛Python异常、磁盘故障、未知本地归因 | 禁止自动认定“可保活本地问题”；人工审查 | 默认停止。只有明确允许名单可放行 |
| resource hold | 保留队列和连接，安全边界后有界回收/回差恢复，必须新健康证据 | 按省份keeper资源预算，不与在途竞争 |
| 报告故障 | 只影响报告链路，记录阶段；未知写入先读回 | 不改变采集/登录/采集block |

保活动作也经过共享真实请求审计和所有权，不计业务原件；“采完/人工暂停业务”不自动等于“用户停止所有请求”。必须明确collection暂停、idle hold、stop-all三种控制语义，保留各省最新授权。当前历史Engine的control仍是旧语义，CLI控制全部禁用；正式上线前必须补stop-all/keeper显式状态契约，不能直接使用历史start/stop说明。

山西最终驱动修复限定Python/Playwright/Node源码版本及明确已完成Route/Request；不把内部Dispatcher清理直接移植。本阶段只做模板缓存淘汰与内存准入纯函数。上线前必须补齐异步任务上限、监听器卸载、断连处理、driver-private版本核验与健康连接下的真实内存观察；不能以定期重连代替修复。

## 旧队列、原件兼容和迁移

1. 先固定省份、账号别名、源代码/运行代码SHA、原owner、当前策略与授权引用。只导出非敏感状态，禁止读取profile、Cookie或私密配置。
2. 各省由原owner在已结算边界冻结**只读导出**：pending、leased/reserved、sent、unknown、denied、failed、saved、empty、已发未确认和完整分页frontier。此次没有冻结或导出生产队列。
3. 建立旧ID→TaskIdentity v1逐项映射。真实范围缺失、旧cursor不可稳定标识、只有日期而无主体、状态歧义，全部pending_review。先导入所有已消耗/未知/拒绝墓碑，再考虑未尝试任务；manifest存在不能证明其他身份未发。
4. `legacy.review_manifest`只按显式字段映射核对受控路径和SHA，不写旧文件，输出queue_import_ready=false。第二阶段queue_compat已支持六类旧格式及非派发shadow，缺规则仍unmapped；没有绕过第2/3步的一键上线迁移。
5. 原件保持旧路径/不可变内容；新目录先保存受控引用和source SHA，不大批复制。新解析写侧车，保留旧parsed及其schema版本。复用原件不计新增网站请求，不从空目录补发旧日期。
6. 影子模式比较每省新旧身份数、唯一文件/来源、原件SHA、空/非空、业务周期、节点/成员/线路/分页覆盖。明确列表不等于附件、日期两端不等于连续、原件格式通过不等于字段语义通过。
7. 取得单省具体切换授权，持原owner锁，排除keeper/协调器在途，在原Chrome上handoff；不得先启动新worker再停旧worker。不能以新root避开旧锁。
8. 首批只做已授权、明确未消费的新范围；通过小预算再扩到队列，最后才安排长稳观察和自动运行。

## 分阶段和回滚

| 阶段 | 本次状态 | 放行条件 |
|---|---|---|
| A 文档/源码/故障审查、保留已有框架 | 完成，证据有SHA | 无生产改动，明确历史与现状 |
| B 共用离线引擎和三省样本 | 第一阶段完成；第二阶段已扩展逐项目录和更多源形态 | 当前资格见PHASE_TWO，不称全站全语义完成 |
| C 完整身份映射/旧队列影子导出、资源与owner协议 | 旧格式/临时shadow已实现；unmapped补证、冻结导出和owner仍待做 | 导出由原owner授权、逐项差异0/显式hold；完整操作计数 |
| D 单省受控上线 | 未授权、未执行 | 满足ACCEPTANCE中的小预算、keepalive及回滚条件 |
| E 第二/第三省，再加陕西/冀北 | 待稳定后逐省 | 不共享队列、账号和会话，新增scope独立验收 |

回滚恢复旧**代码/调度所有权**，不回退业务事实。持同一owner锁停止新派发，等已发请求结算；结果未知则保留保护，不启动旧worker接手未知范围。导出新版本期间所有attempt/response/原件/未知/拒绝身份并映射回旧侧，再恢复明确未消费队列；不覆盖旧数据库、不重建队列、不重新seed、不清block。原Chrome、profile、原件和旧一次性恢复回执一直保留。新旧两侧都不能安全接手时停在hold等待审查，不能用“回滚”作为重发授权。
