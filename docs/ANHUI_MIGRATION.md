# 安徽迁移：首批切换（2026-10-31 晚）

目标：让 PMOS 接替安徽旧采集的原生通道。本文件只描述离线已经完成的部分，以及切换时需要用户批准的步骤。本轮没有发网站请求，也没有操作 Edge、launchd 单元或旧队列，只读了本机 `anhui/` 目录。

## 旧系统行为（安徽会话 23:10 确认）

- **运行环境：** 运行在本机，由 launchd 管理：worker、keeper、page-recovery 三个单元。
  - 专用 Microsoft Edge 不属于任何单元，停这三个单元都不会关掉它。
  - CDP 端口记录在 profile 的 `DevToolsActivePort` 里。
- **页面：** 原生请求在唯一一个"综合查询"页（`/pxf-settlement-outnetpub/…columnHomeLeftMenuNew`）里用 fetch 发出。旧系统重放页面自己发过的请求，只改日期和选项值；请求头只放在内存里。
- **节奏：** 单通道，每次响应后间隔 1 秒（用户 23:40 批准，旧系统 23:39 已生效；之前是 5 秒）；空闲 300 秒后用系统负荷预测（日）保活。
- **失败处理：**
  - 401/403/40302/429 → 全省停下，等人工处理；
  - 瞬时错误、其他 4xx → 该条转 `pending_review`，同一个 key 不重发，其他日期照常继续；
  - 结果未知 → 永不重发。

## 首批：日期唯一变化的原生报表

`pmos/migration/anhui.py`：

- **`first_batch`：** 从注册表里选 `native_verified` 的报表，要求同时满足：
  - 绑定证明只有一个接口、一种请求体结构；
  - 日期只占一个参数位置；
  - 选择器正好是 `{"日期": …}`。
  
  其余报表按原因列出暂缓：公告列表、多选项、整日请求、待复核等。
- **`build_specs`：** 生成 fetch 规格，`template_body_set: {日期字段: "{date}"}`。PMOS 沿用页面自己那次请求的请求体，只改日期字段，和旧系统的重放一致。字段不存在时不发送（`template_body_field_missing`，记为未发）。
  - 还没观察到页面请求时，用该报表原来的 UI 规格自举：按菜单打开、设日期、点"查询"，截获页面对该接口的原生响应。
  - 身份约定：日期字段同时映射为 `日期` 维度，与旧身份逐条相等。
- **`legacy_rows`：** 用 `decode_jobs` 解码 `server_native_attempts_<dataset>.json`。只保留了 receipt 的已采记录，用旧 worker 自己的 receipt 补出日期和选择器；条件是 receipt 属于同一个数据集，且日期等于键。
- **`plan_cutover`：** 身份逐条相等的数据集进入范围。任何数据集只要还有 `attempting` 状态的旧发送，就整体不可切换。
- **`apply_cutover`：** 新建实例台账，范围内的旧发送全部作为墓碑导入，再写交接回执：范围、预算、`keepalive_dataset=queryRqxtfhyc`、`seeding=spec_periods`。

**10-31 23:3x 的结果（本机旧目录，只读）：** ready=true，范围 19 个数据集，共 5,009 条旧记录逐条相等，没有在途的旧发送。
- 19 个是：9 个日报（实际负荷、出力预测和实际、水电、非市场、备用、日前外来外送、日前平均申报价、系统负荷预测）和 8 个原生公告类报表，等等。
- `real_time_uniform_settlement_price` 有 1 条维度不一致，暂缓。

**配置（`provinces/anhui/province.json`）：**
- 页面选择：`page_url_contains=/pxf-settlement-outnetpub/`；
- 闸门：开；
- 间隔：响应后 1 秒（用户 23:40）；
- 失败处理：瞬时错误、结果未知只结算该条，连续 3 次暂停该报表；40302 时全省停下；
- 浏览器：本机覆盖文件 `<instance>/browser.json` 指向 `anhui/state/server_browser_profile`；
- 运行环境：用 `anhui/.venv` 运行，它的 Playwright 1.63 已打崩溃补丁（stray CDP reply）。

## 切换步骤（安徽会话在用户批准后执行）

1. **冻结：** 把 `state/server_native_rollout_control.json` 的 enabled 改为 false，等 job finished；再 `launchctl bootout` worker、keeper、page-recovery 三个单元，Edge 不受影响。确认没有 `attempting`。
2. **重算计划：** 用冻结后的文件运行 `plan_cutover`，确认 ready 和范围。
3. **交接：** 运行 `apply_cutover`，首批预算 5（3 个新日期加 2 次保活），实例目录写 `browser.json`。
4. **启动 PMOS：** 例如 `PYTHONPATH=<发布目录> anhui/.venv/bin/python -m pmos anhui accept`。重点观察：第一次走 UI 自举，之后用模板重放；每个请求和间隔；保活。
5. **扩大或回滚：** 验收通过后，另行批准更大的预算和持续运行；失败则 `stop-all`，安徽会话恢复三个 launchd 单元。

多维度、整日请求和公告列表，等旧系统补采结束后再迁。整日留空请求和选项重放的经验见安徽会话 10-31 的第 9 节。

## 第二批：多维度（用户 10-31 决定一次切换，先写代码）

**旧系统事实（只读调研，10-31 23:30）：**
- **候选 job key**：`sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',',':')))`，identity 为 `{dataset, period, selectors, identity_bindings}`。26 个队列全部能重算，0 不一致。
- **identity_sha256**：等于 `SHA-256(JSON.stringify([typeof, value]))`。选项请求值多数就是显示文字。例外：
  - 6d80 是内部 ID，从已存档原件同一条记录的 `unit_id` 取（114 台中 41 台可解析）；
  - 580f、766f、日前统一结算价的值不等于显示文字，只能走网页。
- **重放**：沿用兄弟模板的请求体，只改日期路径和唯一的选项路径（binding 的 `option_parameter_paths`）。类型不变。兄弟答复逐条回显选项的（`option_echo_fields`），新答复也必须逐条回显。
- **settle_failure**：以下情况只记该条：没发出、页面崩溃、`data=={}`、回显结构不同。连续 3 次才停用该指标的重放。
- **整日（4 个指标）**：选项留空一次取全部，先存原件再拆分；每个选项 96 点；重复 k 份合并；与已逐个采到的选项比对。旧系统预计在切换前采完。
- **新日期**：旧系统每天把 discovery 补到今天−1，再按日期轮转读下拉框，发现选项。

**PMOS 实现顺序：**
1. **导入与身份比对**（`pmos/migration/anhui_multidim.py`）：
   - 解码候选为旧行：键重算核对；身份 = `(dataset, 日/月, selectors)`。
   - observed_candidate 为待采。captured / captured_whole_day / whole_day_not_returned / pending_review / source_empty* / historically_captured 为墓碑，永不重发。requesting / attempting 为在途，阻止切换。
   - 可重放数据集生成 fetch 规格：`template_body_set` 含日期路径和选项路径；`scopes` 为候选里所有选项组合（不含日期）；身份约定 `dimension_fields` 把日期映射为“日期”，选项路径映射为各自标签。
   - 切换时在同一张回执里用 `legacy_pending_only`：首批报表用 `extend_scope` 按天扩展；多维度只种旧待采候选。
2. **回显检查和失败规则**：响应规格声明回显字段，每条记录的该字段必须等于本任务的选项，否则只隔离该条。连续 3 条才暂停该数据集（已有 isolation_streak）。
3. **重启自举和页面健康**：先挑一条已覆盖的记录走网页拿模板；渲染崩溃后换页；驱动丢失时退避。
4. **新日期**：第一版按“最近一个已发现日期的选项集合”滚动到新日期（选项集合少有变化；消失的选项记为空，新增选项暂时缺失，并记事件提示）。下拉框发现之后再补。
5. **整日请求**：旧系统采不完再做。

### 第二批进度（10-31 夜）

**第 1 步已完成**：`pmos/migration/anhui_multidim.py`，测试见 `tests/test_anhui_multidim.py`，全套 475 个测试通过。

- **重放规格**：`replay_routes` 只采用选项值都是显示文字、路由唯一的重放证明。
  - 生成 13 个 fetch 规格：日期路径和选项路径来自 `template_body_set`，每个旧候选里出现过的选项组合是一个 scope；回显字段写在 `response.echo`。
  - 第一次没有模板时，用 UI 查询自举：日期字段加上每个选项标签一个下拉，值取本任务的选项。原 UI 请求保存在 `source.ui_request`，可以原样重建。
- **候选导入**（`legacy_rows`）：每条候选先重算键并核对。阶段映射：
  - observed_candidate → 待采；
  - captured 类 → saved；
  - source_empty 类 → empty；
  - whole_day_not_returned → skipped；
  - pending_review、whole_day_option_review → held，永不重发；
  - requesting、attempting → 阻止切换。
- **合并切换**（`plan_cutover`、`apply_cutover`）：一张回执，`legacy_pending_only`。
  - 首批报表用 `extend_scope` 按天增长，旧系统已采完的日期跳过；
  - 多维度只把旧的待采候选种成任务。
- **回显检查**（`engine.echo_mismatch`）：原件先存档；有一条记录的回显字段不等于本任务的选项，就只隔离这一条（site_data），连续 3 条暂停该数据集。

**本机旧目录实算（只读）**：ready=true，范围 32 个数据集（首批 19 + 多维度 13）。
- 旧记录 33,147 条，已消费 29,288 条，作为墓碑。
- 待采约 3,860 条：重要通道 2,419、实时断面 1,076、9d35 233、queryRqscjjcqfslj 85 等。
- 多维度候选 28,138 条，逐条身份相等。
- 暂缓：
  - 6d80：请求值是内部 ID，需要从原件解析，taskId 只在页面里有；
  - 联络线实际、联络线预测：没有可用来自举的 UI 规格。

**第 4 步已完成（新日期）。** 逐选项的 10 个数据集带 `source.roll_forward_options`，并设 `period.lag_days=1`（与旧发现一致，最多到昨天）。
- 每天的首次播种中，比已知最新日期更晚的日子，按已知的全部选项生成任务，并记事件 `options_rolled_forward`。
- 已消失的选项答复为空：按 scope 记空，不暂停数据集。新出现的选项要等页面发现做好才能补上。
- 整日的 4 个（两个节点电价、1404、联络线预测）不滚动：逐选项每天要约 1,000 个请求，整日只要 3 个。它们等整日通道做好再接。
- 冻结时以计划为准：旧系统有请求在途时计划显示 not ready（实测出现过一次，重算后恢复 ready）。

**第 3 步已完成（页面健康）。**
- **渲染崩溃：** 页面的 crash 事件会标记崩溃，此后 `send` 一律拒绝（`page_crashed`，未发出，不消耗数据项）。
  - 下一个 tick 先换一个新页。Playwright 不会救活崩溃的页面，这和旧系统"新开页"的做法一致。
  - 配了工作页（辽宁、安徽）就从门户用 window.open 重开；否则在原地址新开一页。
  - 每小时最多换页 3 次，超过就全省停下（`page_crashed_repeatedly`）。
  - 已用真浏览器的 chrome://crash 测试通过。
- **驱动丢失：** 出现 "Connection closed" 或 "Target … closed" 这类错误时，判为 `DriverLost`，不当作网络错误逐条隔离。
  - 在途那一条记为 unknown，永不重发；
  - 不写业务封锁，记事件 `driver_lost` 后进程退出，由 launchd 或 systemd 退避后重启。
- **安徽浏览器配置（安徽会话 10-31 建议）：** 工作页 `pmos-anhui-work`。
  - 先接管旧系统留下的那唯一一个综合查询页（`adopt_url_contains`），不多开标签页。
  - 需要新开时（例如崩溃后），按门户自己的菜单点击“信息披露 → 综合查询”打开，等地址里出现 `columnHomeLeftMenuNew`。站点脚本会在地址里加一个经校验的动态参数（`$_ts` 一类防护），自己拼地址的 window.open 没有验证过，所以不用。辽宁的 window.open 方式由其旧系统冷启动验证过，保持不变。
- **计划换页（`browser.page_recycle`，安徽会话 11-01 00:20 同步）：** 同一个综合查询页用得越久，占用越大：曾涨到 2.4 GB、CPU 650%，并且约每 2.5 小时自己崩溃一次。
  - 引擎在两个动作之间检查，每分钟最多一次。页面已用满 1 小时，或 JS 堆达到 768 MB，就按崩溃恢复的同样流程换新页。
  - 计划换页单独计数，不计入“每小时 3 次崩溃即停全省”。事件记为 `page_recycled`，带页龄和堆大小。
  - 辽宁设为 6 小时或 768 MB：同站点页面 6 天涨到 1.8 GB，只有新页能释放。
  - 还没做：按渲染进程的 RSS 和 CPU 告警（macOS 上按 `--user-data-dir` 和 `--type=renderer` 过滤 ps）。
- **回滚注意：** 旧系统的 worker、keeper、page-recovery 都要求综合查询页恰好一个，否则报 `existing_anhui_tab_changed`。回滚前确认只剩一个综合查询页（PMOS 接管的就是那一页；如果崩溃后新开过，先关掉多余的页），再 bootstrap 三个旧单元。切换时 page-recovery 也必须 bootout。

**6d80（内部机组 ID）已接上。** 照旧系统的 archive_resolver：
- 从旧 worker 自己的已采原件里，找出每个机组 `NAME` 旁边的 `unit_id`。用原件前先核对内容 SHA。
- 只采用恰好一个、且 `SHA-256(JSON.stringify(["string", unit_id]))` 等于候选自身选项身份的值。
- 规格：scope 是 `{机组名称, unitId}`，请求只改日期和 unitId（taskId 沿用页面原请求体）。身份约定 `scope_dimensions: ["机组名称"]`，与旧身份逐条相等。
- 结果：42 台机组可解析，1,806 条候选身份相等。其余 1,994 条属于未解析的机组，PMOS 不发送（`partial_options`），旧记录照样作为墓碑导入。

**整日通道已接上（4 个：两个节点电价、1404、联络线预测）。**
- **规格：** `<数据集>_whole_day`，沿用页面原请求体，改日期，`genName=''`，节点电价另按 3 种类型分 scope。超时 120 秒，时间范围到今天减 2。
- **校验**（`engine.whole_day_mismatch`，原件先存档）：
  - 每个选项恰好 96 个时刻；
  - 节点模式不许重复，`price_type` 必须等于请求的类型；
  - 记录模式的重复份数一致、重复内容相同；
  - 空答复如实记为空，不填零。
- **模板预热**（`request.prime`）：整日请求没有自己的页面操作，所以用旧系统最新一条已采记录，通过页面查询一次来拿模板。结果只存为样本，每个规格 10 分钟内最多一次。
- **旧整日队列**：键为 `<日期>|<类型>` 或 `<日期>|全部`。2,424 条逐条身份相等；captured、source_empty、pending_review 作为墓碑；pending 继续采；requesting 会阻止切换。
- **合并范围：** 39 个数据集（首批 19 + 多维度 16 + 整日 4）。首批和整日按天增长。

**两个联络线数据集（23:5x）：** 安徽会话给出了菜单路径和报表代码（frame `infoCode=sjllxsdsqk` / `sjllxsdsxyc`）。
- 联络线实际：783 条候选逐条相等，待采约 711 条，滚动补新日期，答复不带选项名所以无回显。
- 联络线预测：1,914 条全部已采，按回显字段 `name` 校验；新日期由整日通道采。

**新选项的页面发现（第 5 步）已完成。** 11 个单下拉、选项值即文字的报表带 `source.discover_options`。
- 每天滚动之后排一次 `discover_options` 命令。它沿用单下拉发现的做法：按菜单打开报表、填日期、打开那一个下拉框，读出每一项的文字和 ElOption 值，再按 Escape 关掉；不点查询。读之后要核对已选值没有变化。
- 新出现、并且值等于文字的选项写入 `state/discovered_options`，加载规格时并入 scopes（重启后保留），同时给当天建任务。值不是文字的只计数，不采。
- 已用真浏览器夹具测试通过。

**渲染进程告警：** `browser.renderer_alert`（安徽、辽宁均为 1536 MB 或 300% CPU）。每分钟按 profile 和 `--type=renderer` 用 ps 读一次进程表，写入 `state/renderer_health`；超过阈值记 `renderer_heavy` 事件，每 10 分钟最多一次。

**launchd 模板：** `deploy/launchd/pmos-province.plist.template`。只在异常退出时重启（驱动丢失），预算用完或被阻断时保持停止。

**换页后沿用模板（安徽会话 11-01 00:37 实测）：** 旧系统的模板只保存 URL、会话级请求头和请求体结构，换页后从当前页的 main frame 发出照样成功：4 个整日请求都用了换页前的模板。
- PMOS 安徽配置：`template_frame_fallback: main_frame`。模板的框架没了，就从当前页的 main frame 发出，不再重新预热或自举。
- 模板保留 4 小时（`template_ttl_seconds`），默认是 15 分钟。
- 山西和辽宁不开，因为从 main frame 发出时 Referer 和 Origin 会变，没有验证过。

**还剩的（冻结时）**：用冻结后的文件重算计划，出发布目录，填好 plist，再给出整套切换和回滚命令。

## 11-01 02:08 采集空停后的核对（安徽会话同步）

安徽当时的情况有两点：
- 附属 target 崩溃，导致 driver 整个退出；
- 同一时刻 macOS 短暂拒绝 launchd 进程访问 ~/Desktop（EPERM），状态写不进去，任务卡在 running，没有人接手。

PMOS 对照检查后补了三处：
- **EPERM 重试**（`pmos/fsretry.py`）：状态文件的读、写、事件、删除，以及原件存档，遇到 `PermissionError` 且 errno 为 EPERM 时，每 0.5 秒重试一次，最多 30 次。EACCES 不重试。原件存档最要紧：此时答复只在内存里，而标识已经消费。
- **任何位置的 driver 死亡都不写业务封锁：** 不只是受保护的页面调用，打开框架等其他环节出现 "Connection closed" 或 "Target … closed" 时，在途那一条同样记为 unknown（driver_lost）、永不重发，进程按 DriverLost 退出，由 launchd 重启。重启后新进程能正常启动，因为没有封锁。
- **重启次数上限：** 与安徽"2 小时内最多 3 次"的预算一致，2 小时内第 3 次 driver 丢失时写入 `driver_lost_repeatedly` 封锁，交给人处理，不会无限重启。
- **PMOS 没有"任务卡在 running"的问题：** PMOS 不存在单独的 job 状态。工作进程本身就是循环，在途请求在台账里先记录尝试，再发送。进程退出时，要么已经结算（unknown），要么在下次启动时以 `unsettled_attempt` 阻断并等人复核，不会默默空转。

## 11-01 11:00 切换前核对（本机旧目录，只读）

- **计划：** 39 个数据集，身份逐条相等；只因旧系统正在运行、有 1 条在途而显示 not ready，冻结后即可 ready。
- **整日补采还没完，但不用等：** 日前节点电价剩 292 条（到 04-08）、实时节点电价剩 408 条（到 05-16），旧系统和多维度交替，近几小时每小时只完成 2～3 条。交接时这 700 条作为待采种入 PMOS 的整日规格，接着采。
- **已修的启动阻断：** PMOS 的驱动内存模块只认审过的 Playwright 包。安徽 venv 的 1.63.0 打过 stray-reply 补丁，SHA 不同，驱动一启动就退出（`unreviewed_playwright_driver`）。补丁只改 CDP 回复分发里那一处断言，与本模块无关，已把补丁版 SHA 加入 `REVIEWED_SHAS`。安徽 venv 下全套 501 个测试、16 个真浏览器测试通过。
- **DevToolsActivePort：** profile 在 ~/Desktop 下，读端口文件也走 EPERM 重试。
- **切换后 PMOS 不采、旧系统还在采的：**
  - 机组状态（f901）：旧系统正在采，待采 1,260 条（只覆盖 10-18～10-28），每小时 5～6 条。旧绑定只证明了日期，没有选项参数路径（分类、节点名称要走页面），PMOS 还不能重放。
  - 辅助服务三张（580f、6c65、c726）约 37 条待采；通道可用容量两张（16755、4b6d）约 29 条，旧系统已 2 天没动。
- **旧系统也已停更的（4～7 天没有新日期，切不切一样）：** 新能源总出力（风、光）、实时统一结算点电价、日前统一结算点电价、省系统负荷预测、分省可再生能源发电能力预测、新能源总出力预测（周）。公告列表类旧队列 1～8 天没动。
- **发布位置建议：** 发布目录和实例（`PMOS_DATA`）放在 ~/Desktop 之外（例如 `~/pmos-anhui/`），避开 launchd 访问桌面的 EPERM；浏览器 profile 仍用原来的 Edge。

## 11-01 计划换页遇 412 的自动判断（与旧系统 planned_recycle_challenge 对齐）

安徽 06:59 的计划换页中，新页第一个 document 收到 412，之后浏览器自己过了网站挑战并重载，但落在信息披露“首页”栏目，没有现货市场树。用户 10:5x 决定“允许自动判断后继续”。PMOS 的做法：
- **盯住新页的文档请求**（`pmos/browser/challenge.py` 的 `DocumentWatch`）：换页（崩溃恢复或计划换页）期间，站点任何顶层 document 返回 4xx/5xx 都记下。由点击打开的新标签页，其首个导航请求还没有 frame，同样算作新页自己的 document。以前 PMOS 不看 document 状态，412 后可能照常发送。
- **只在一种情况下自动判断**（`browser.page_recycle.challenge`）：
  - 必须是计划换页，且只有那一个 412；
  - 2 小时内最多 1 次，尝试开始时就扣掉额度；
  - 45 秒内页面必须在 `/pxf-settlement-outnetpub/` 加载完成，正文含“安徽”，且没有登录、安全、限流、权限字样（与旧系统 visible_reason 相同）。
- **落在首页栏目时**：关掉这一页，从已登录的门户菜单“信息披露→综合查询”重开一次（安徽会话的经验）。重开时如果再有 document 失败，就停下。
- **96 点验证**：通过前什么都不发（`recycle_challenge_pending` 存在 state 里，重启后照样先验）。用保活报表（系统负荷预测日）已存档的那天再查一次，答复里的数字必须与存档原件逐个相等，并且至少 96 个。
- **其余情况**：一律全省封锁等人，原因 `security_verification`（http 412），或 `page_document_failed`、`recycle_challenge_check_mismatch`、`recycle_challenge_unverifiable`。崩溃恢复遇到 412 从不自动判断。
- 测试：真浏览器夹具（412 挑战页自己重载到首页 → 从门户重开综合查询；额度用完、崩溃恢复、第二次失败都封锁），以及引擎测试（2 小时 1 次、先验 96 点再采、数字不同即封锁）。
- 旧综合查询页在用户重新登录后弹“会话已注销”：PMOS 从不点弹窗，换页后只关闭旧页。

## 11-01 辽宁经验 21（等待中的车道不得进入轮转）对照

辽宁的问题：一个只能等待的车道仍被调度，一次空转让主循环按“无事可做”睡了 10 秒。PMOS 每个 tick 按顺序扫描全部数据集，被暂停的、在等模板的都在同一次扫描里跳过，只有没有任何数据集能发时才睡 10 秒，所以没有同类问题。已加回归测试（一个等模板、一个暂停、一个可发：可发的连续发送，没有空闲轮）。

## 11-01 切换步骤（用户：现在就切吧）

发布目录 `/Users/yunyu/pmos-anhui/release-20261101-r1`，数据目录 `/Users/yunyu/pmos-anhui/data`（都在 ~/Desktop 之外），Python 用 `anhui/.venv`（带 stray-reply 补丁的 Playwright 1.63）。
1. **冻结**（安徽会话）：rollout control 设为 enabled=false，等当前任务结束；bootout worker、keeper、page-recovery 三个单元。Edge 不动，保留门户首页（地址含 `/#/dashboard`）和唯一一个综合查询页。
2. **计划**：`tools/anhui_cutover.py plan`，必须 ready=true、legacy_frozen=true。
3. **交接**：`tools/anhui_cutover.py apply --approval "<用户原话>" --budget 5`。它会重新生成多维度和整日规格（冻结时的选项、6d80 机组），重算计划，建新台账，写 browser.json 和 SHA256SUMS。离线干跑（旧目录只读）：39 个数据集，33,177 条旧记录作为墓碑，约 6,800 条待采种入。
4. **验收**：`launchctl bootstrap gui/501 <发布目录>/deploy/launchd/com.yunyu.pmos.anhui.plist`。预算 5 用完后正常退出，保持停止。
5. **持续运行**：用户批准后 `extend-budget --add <N> --approval "<原话>"`，再 `launchctl kickstart gui/501/com.yunyu.pmos.anhui`。
6. **回滚**：`stop-all`，bootout PMOS 单元，确认只剩一个综合查询页，再恢复旧的三个单元。PMOS 已发过的身份要先告诉旧系统，不能重发。

## 11-01 11:3x 验收第 1 步连不上 Edge → r2

- 现象：`connect_over_cdp` 访问 `ws://127.0.0.1:49290/devtools/browser/9720b6ab-…` 返回 404，进程以 1 退出，没有向网站发任何请求，台账 0 次尝试。
- 原因：DevToolsActivePort 文件写于 10-23，里面是那次启动的浏览器 GUID；10-31 起运行的 Edge 没有重写这个文件。端口对，GUID 已过期。
- 修复（`attach.cdp_endpoint`）：端口仍从文件读，浏览器当前的 socket 改问本机 `/json/version`，并要求它是同一端口上的 loopback 地址。`browser.profile_process_check`（安徽开启）还会用 lsof 和 ps 核对：监听这个端口的进程必须带 `--user-data-dir=<专用 profile>`。
- 发布：`release-20261101-r2` = r1 加上这一处修复（见 `CHANGES-r2.txt`）。交接回执的 code_sha 是 r1 的 SHA256SUMS（保留为 `SHA256SUMS.receipt-r1`），实例台账不用重做。

## 11-01 发布滞后：不问还没发布完的日期 → r3

辽宁经验 22 的第 5 点（实际出力类数据第二天才发布）在安徽同样成立，而且后果更严重：PMOS 把空答复记为最终结果（以后不再发同一个身份），把一天没结束时的不完整答复记为已保存（以后不再重采）。切换前首批 19 张日报周期都到今天、没有滞后，验收的前几次请求就会落在 11-01 的实际类报表上。
- **证据（旧目录只读）：**
  - 旧系统的"省间联络线输电情况"有 27 次空结果，全部发生在滞后 1 天的时候，滞后 3 天再采都有数据；
  - 旧首批从没在当天采过当天。
- **滞后天数**（`migration/anhui.PUBLISH_LAG_DAYS`，生成器写入 `period.lag_days`，以后重新生成也保留）：
  - 描述当天发生了什么的报表等 2 天：实际、实时、执行情况、运行日，以及不确定的几张（系统备用、开停机名单、关键断面可用容量、辅助服务申报出清、各类电源电量）；
  - 其余（预测、日前、计划、限值）等 1 天；
  - 整日通道原本就是 2 天。
  - 共改 30 个规格。
- **引擎**（与省份无关）：
  - `spec.newest_due()` 给出可以请求的最新一天，`ledger.next_task(newest=…)` 只挑这一天及以前的任务。交接时已经种下、但日期太新的任务（旧系统的待采候选）原地等着，不发送，不消耗身份。
  - 没有可发任务时，如果有任务在等发布，状态为 `waiting_publication`；如果台账按天增长、已经追平，状态为 `caught_up`。两者都不是 complete，所以 `accept` 不会退出。原来追平时 `accept` 以 0 退出，launchd 单元就一直停着，第二天的新日期没人采。
  - 邮件标题：这两种状态显示"正常（已追平，等待新日期）"，不会误报"已停滞"。
- **需要用户决定（未做）：**
  - 旧系统滞后 1 天采到的空结果（例如上面 27 条）作为墓碑导入，PMOS 不会重发。要不要隔几天重查一次，得由用户批准，因为这等于重发已消费的身份。辽宁同一问题也在等用户决定。
- 测试：滞后规则、种下的未到期任务等待且不发送、等待发布状态。全套 508 项通过。
- **r2 验收 11:31 实际发生的情况**（在暂停通知之前就已启动）：
  - 第 1 个请求采了 `actual_load 2026-11-01`，结果 success。原件中 11:30 以前的 46 个点是真数据，之后 50 个点全是 0，被当成完整的一天保存了。
  - 第 2 个是每日选项发现（只读下拉框，不点查询），在 `generation_types_energy_count` 上失败（`select_value_changed_by_reading`），按本地故障把全省封锁，进程停下。
- **r3 一并修复：**
  - 选项发现的本地或 unknown 故障只暂停这一张报表的发现（`discovery_holds`、事件 `options_discovery_held`），已知选项照常滚动；登录、权限、限流、网络答复仍会封锁全省。
  - 已消费过的发现（同一天）不再排队。否则解除封锁后会因 `consumed_identity_requires_review` 再次封锁。
  - `release-early <attempt> --approval "<用户原话>"`：用户批准后，把一份太早保存的答复释放回待采。旧原件保留在存档里，任务按滞后天数等到发布后再采。11-01 的实际负荷（attempt 1）要用它，但需要用户批准，因为这等于重发已消费的身份。
- **辽宁经验 22（报表引擎会话闲置超时）对照：**
  - PMOS 目前没有走 FineReport 引擎会话（parameters_d）的规格：辽宁的引擎线 024 和 068 不在首批范围；安徽的 `/fineReport/select*` 是普通 JSON 接口，没有引擎会话。所以目前不受影响。
  - 迁移辽宁引擎线时要做：闲置超过 10 分钟先重开报表页（文档加两个脚本，私有引擎先清本地映射槽位再映射）；`Dec-Session_Lifecycle_End` 单独作为一个暂停原因；失败时保存脱敏后的响应原文（PMOS 目前只给 site_data 留原文）。

## 11-01 选项发现误判"值变了" → r4

- **原因：** 可搜索的 el-select 在下拉框打开时会清空输入框，把选中项挪到占位提示里；按 Escape 关闭后，要等下一轮渲染才把文字放回去。PMOS 按完 Escape 立刻读输入框的文字，就误判为 `select_value_changed_by_reading`（generation_types_energy_count，11:31）。
- **修复（`ui.select_options`）：** 按 ElSelect 组件自己的值和输入框文字一起判断，等下拉框收起、两者都恢复原样（最多 3 秒）才算读完；真的变了仍报这个错。夹具 `filterable_select_page.html` 模拟了"打开时清空、关闭后延迟恢复"和"真的改了值"两种情况。
- **`release-discovery <dataset> --approval "<原话>"`：** 解除 r3 下被暂停的选项发现，从下一次按天滚动起重新读取。
- 测试：全套 511 项（安徽 venv、山西 venv）和真浏览器 18 项通过。

## 11-01 12:07 r3 验收：页面操作在后台标签页里全部超时 → r5

- **经过**（安徽会话按用户"三项都同意，继续验收"执行 ack、release-early 1、extend-budget +20 之后，用 r3 启动；r4 的通知那时还在排队）：
  - 12:07:42 起共 15 次尝试全部失败：选项发现 2 次 `select_options_missing`（约 11 秒），之后每次约 45 秒 `menu_navigation_timeout`；
  - `actual_load` 10-28～10-30 三条被当成 site_data 隔离（`isolated:bootstrap_menu_navigation_timeout`），数据集被暂停；
  - attempt 17（day_ahead_average_offer_price 10-31）在 12:18 bootout 时还在导航，台账里停在 pending；
  - 没有 4xx，没有封锁，页面已登录、正常。
- **原因：** 旧系统每次在页面上操作之前都会先 `ensure_visible`（如果不在前台就 `bring_to_front`），注释写明 Edge 的后台标签页会让页面上的操作检查一直等下去。PMOS 没有这一步。综合查询页在后台时，树节点展开和下拉框的过渡动画走不完，每一步都等满超时。11:31 那次是前台，所以成功了。
- **r5 修复：**
  - `ui.bring_forward`：UI 操作（`transport._ui`、`ensure_frame`、`select_menu`）开始前，页面不是 visible 就先 bring_to_front。
  - `ui.select_menu`：点到菜单叶子之前失败的，标成 unsent（没有打开报表，什么都没发）。引导阶段的 unsent 失败不再包装成 site_data 隔离，按 `_hold_unsent` 冷却 600 秒，连续第 3 次才长期暂停。
  - `engine._page_operation_failed`：UI 页面操作连续 3 次失败（不分报表）就封锁全省（`page_operations_failing`），保活照常，不再每次等 45 秒把预算耗光。
  - `ledger.attempts_since_handoff`：已证明没发出（not_sent）的尝试不计入预算。
  - 新命令 `review-unsent <ids> --approval`（用户批准后才能用）：核实过的菜单导航失败（`NAVIGATION_CODES`、没有 HTTP 状态），以及进程被停时还在导航的 pending 尝试，改记为 not_sent，被隔离的任务回到待采。理由：某一天的请求只有在菜单走完、填好日期、点了"查询"之后才会发出，菜单失败时这一步还没到。
  - `release-discovery --all`：一次解除所有被暂停的选项发现。
- 测试：前台检查；叶子之前失败即 unsent（真浏览器）；连续 3 次页面失败封锁全省，期间不隔离、不消耗；review-unsent 的接受条件与拒绝。全套 515 项（安徽 venv、山西 venv）和真浏览器 19 项通过。

## 11-01 回滚后的状态与下次切换

- **回滚**（用户选择"先回滚旧系统继续采"）：
  - PMOS 已 stop-all，单元已 bootout；台账停在 17 次尝试，其中 attempt 17 仍是 pending。
  - 旧系统三个单元已恢复。keeper 第一次保活报 `local_report_navigation_changed`，因为 PMOS 把侧栏留在了"省内现货市场 > 市场干预信息"。之后门户弹出"会话已注销"，正在等用户手动登录。
  - 回滚经验：PMOS 跑过以后，侧栏停在它最后走到的地方，旧 keeper 会拒绝。回滚时要先关掉综合查询页，从门户菜单重开，再恢复旧单元。
- **下次切换**，用 `release-20261101-r6`。它取自 git f7e4162（r5 的代码），另放入不提交的 6d80 生产规格，515 项测试通过。
  1. 冻结旧系统，和第一次一样。
  2. 把旧实例改名移开，不要删：`mv /Users/yunyu/pmos-anhui/data/anhui /Users/yunyu/pmos-anhui/data/anhui-acceptance-20261101`。apply 要求实例目录不存在。旧实例里的 17 次尝试和那份 11-01 部分原件作为证据保留。新台账按旧系统最新文件重建；11-01 的实际负荷按滞后 2 天，11-03 以后由新实例采。
  3. 在 r6 目录里运行 `tools/anhui_cutover.py plan`，再运行 `apply --approval "<用户原话>" --budget <N>`。apply 会把重新生成的规格写进 r6，并写 SHA256SUMS（即 code_sha）。之后把重新生成的规格提交 git。
  4. `launchctl bootstrap gui/501 /Users/yunyu/pmos-anhui/release-20261101-r6/deploy/launchd/com.yunyu.pmos.anhui.plist`。
- 11-01 回滚后在旧系统运行中只读 plan：ready=true，plan_sha 仍是冻结时的 e5ee70df…（旧系统在等登录，还没采到新数据）。

## 11-01 12:31 第二次切换（r6）与 r7

- **第二次交接**（安徽会话执行；用户原话"登录后直接用 r5 验收"；r6 = r5 的代码）：
  - 门户在 /#/dashboard，旧 worker 打开唯一的综合查询页并做了 96 点保活；
  - 旧单元 bootout；r3 验收的实例改名保留；
  - r6 plan 的 plan_sha 仍是 e5ee70df…；apply：code_sha a69cb99e…，预算 25，种入 6,801 条；
  - 12:31:41 启动。
- **验收结果：**
  - 页面操作已正常（没有 45 秒超时）；actual_load 10-30 采到了（滞后 2 天，正确）；2 张报表的选项发现成功。
  - 5 张报表读下拉框报 `select_value_changed_by_reading`，其中连续 3 次触发 `page_operations_failing`（12:32:24），之后保活照常、没有业务请求。
- **r7：**
  - `ui.select_options` 读之前要求选中状态稳定 1 秒（最多 5 秒，否则 `select_value_unsettled`）。日期一改，页面可能重新加载选项、重设默认值，之前 PMOS 在这之前就记下了"原值"。
  - 读完后的核对区分两种情况：组件值变了（`select_value_changed_by_reading`）、只是文字没恢复（`select_text_not_restored`）。
  - `engine.PAGE_ANSWERED_CODES`：页面能操作、只是读取核对没通过的，不计入"页面操作连续失败"。选项发现失败仍然只暂停那张报表的发现。
  - 夹具 `?reset=1`：填日期 400 毫秒后页面重设默认值。全套 515 项（两个 venv）和真浏览器 19 项通过。
- **r8（与旧系统的读法对齐）：** 旧系统 `adapters/selector_inventory.observe_selector_options` 读单个下拉框时，允许两种情况：
  - 只有显示文字变了，已选值没变；
  - 被打开的那个下拉框，已选值变成了页面列出的选项中恰好一个（`allow_single_root_model_change`）。

  r6 验收时，5 张报表一打开下拉框就会自己选中一项，PMOS 比旧系统严，所以都报了"值变了"。r8 的 `ui.select_options` 照旧系统放行这两种情况，其余情况仍报 `select_value_changed_by_reading`；下拉框关不上报 `select_dropdown_not_closed`。发现从不点查询，所以选中项变了也不影响任何数据。夹具 `?pick=1`（读取时选中一个列出的选项）通过，`?change=1` 改成页面没列出的值，仍会报错。
- 0f64（月度检修计划，2026-11）的发现报 `menu_label_not_visible`，记为未发出，这张报表的发现被暂停，它的采集照常。月份栏目的菜单以后再查。

## 11-01 12:39 r8 一启动换页失败 → r9

- **经过：**
  - r7 跑了 attempt 10～16：actual_load 10-29 采到；3 次选项发现成功；3 次 `select_text_not_restored`（r8 已按旧系统规则放行）。
  - 12:39:58 换上 r8。综合查询页 JS 堆 812 MB，超过 768 MB，按规则立即计划换页。
  - 从门户重开时立刻报 AttachError，被封锁为 `page_recovery_failed`（keepalive_allowed=false），进程以 0 退出。
- **原因：** 门户的真实地址是 `/?ACYxOEl1=…#/dashboard`（只读看过参数名），站点的动态参数夹在路径和 `#` 之间。PMOS 按子串 `/#/dashboard` 找门户，永远找不到（`portal_page_missing`）。测试夹具的地址里没有这个参数。
- **r9：**
  - `attach.work_page` 找门户时，用去掉查询参数后的地址比较。辽宁的 `open_from` 也是 `/#/dashboard`，同一个隐患一并消除。
  - 计划换页失败时旧页面仍然可用，所以封锁但保活照常（`Decision("province", planned)`）。崩溃恢复失败照旧不保活。
  - `page_recovery_failed` 事件记下 AttachError 的原因码（`code`）。
  - 夹具在 `/?ACYxOEl1=…#/dashboard` 提供门户页，用生产配置 `/#/dashboard` 测试。全套 517 项（两个 venv）和真浏览器 20 项通过。

## 11-01 12:49 r9 验收结果 → r10

- **r9 结果**（预算用完后正常退出，没有封锁、没有 4xx）：
  - 12:49:24 计划换页成功（堆 831 MB），新页 6D4398D9，旧页已关；
  - 选项发现 6 次成功（9d35、9dda 也读通了），1 次 `form_field_changed`（queryRqscjjcqfslj），0f64 月报 `menu_label_not_visible`（未发出）；
  - actual_load 10-27、10-28 和 day_ahead_average_offer_price 10-31 采到；
  - actual_load 10-26 报 `template_body_not_json`（未发出，冷却）。
- **严重问题：模板不保存请求体。** `Templates._capture` 为了不持有驱动缓冲，一直把 body 记为 None。`template_body_set` 的回放要在页面自己的请求体上改字段，没有请求体就只能报 `template_body_not_json`。也就是说，安徽的模板回放在真实页面上从来没成功过：采到的都是经由页面操作完成的，一旦截到模板，下一次回放就失败。整日和多维度这两块主要工作量都靠回放，所以都跑不起来。测试没发现，因为单元测试用的是假的传输层或直接给出 body，真浏览器测试也没覆盖这条路径。
- **r10：**
  - `_capture` 保存页面原请求体的短文本副本 `page_body`（POST，最多 64 KB），只给 `template_body_set` 用；不带 body 的回放仍然不发 body（山西、辽宁的行为不变）。
  - `ui._field`：报表的表单可能还在渲染，最多等 10 秒，等不到或出现两个才报 `form_field_changed`。
  - `_status`：非封锁状态下清掉残留的 `block` 和 `hold_keepalive`。之前封锁解除后 status 里还留着旧原因。
  - 真浏览器测试：页面自己 POST 一个 JSON 体，回放时只改日期，服务器收到的其余字段原样。全套 518 项（两个 venv）和真浏览器 21 项通过。
- **持续运行之前：** 整日和多维度逐选项还没在真实页面上回放过。建议先专门验收：加一点预算（需要用户批准），用 `collect-one` 让正在运行的进程各取一条，覆盖两个节点电价整日、1404 整日、重要通道、联络线、6d80、实时断面约束。
- **r11（专门验收的工具）：**
  - `pmos <id> prioritize <ds1:N,ds2:N,…>`（r12 起可带条数）：正在运行的进程先采这些报表，每张取满 N 条就退出列表，其余仍按交接顺序；不带条数表示一直优先，`--clear` 恢复。其他一切照常：滞后、整日预热、页面引导、各项检查。
  - `collect-one` 挑任务时也遵守滞后天数。
  - 不直接用 collect-one 验收整日：它不做预热，模板缺失时会报 request_template_missing，导致全省封锁。
- **r13（0f64 月报菜单）：** 月度输变电设备检修计划在"中长期市场"下面。左侧树平时只显示现货市场，要先点顶部菜单的"中长期市场"（`el-menu-item`）切换栏目。旧系统做选项发现时总是带 `clear_hover_menu=True`：点完顶部菜单，先把鼠标移出盖住树的悬浮菜单（`.menuBox.menuBoxExpanded`）。PMOS 只在规格写了 `menu_hover_exit` 时才这么做，而多维度规格都没写。r13 改为只要点过顶部栏目，就像旧系统那样先移出悬浮菜单。已只读核对真实页面：树的根只有现货市场，"中长期市场"是普通顶部项。夹具 `anhui_tree_page.html` 复现了悬浮菜单挡住点击，修复后通过。全套 520 项（两个 venv）和真浏览器 22 项通过。

## 11-01 13:14 r12 专门验收：整日预热失败 → r14

- **经过：**
  - attempt 29 actual_load 10-26 走 UI，attempt 30 的 10-25 走模板回放，0.1 秒 success。模板回放第一次在真实页面上跑通。
  - attempt 31 预热实时整日（10-28，template_prime），11.7 秒后 `select_option_missing`，被封锁全省（local，不保活）。
  - 优先列表里排在前面的日前整日没有先跑。
- **原因：**
  - 节点电价的"节点名称"是树形选择器（旧系统用 `multidimensional_tree`，state 里有 `multidim_tree_prefix_*_node_marginal_price`），不是普通下拉框。PMOS 的预热按普通下拉框去选具体节点，所以永远选不中。整日请求本身就是节点留空（`genName=''`），预热只是为了截获页面请求的格式，不需要选节点。
  - 日前整日紧跟在 actual_load 的回放后面，1 秒间隔还没到。`_prime_template` 先记下"已预热"再发请求，请求被 `spacing_not_elapsed` 挡回，什么都没发，这张报表却被跳过了 10 分钟。
  - 预热的本地故障按普通故障处理，封锁了全省。
- **r14：**
  - 整日预热只设日期，节点和类型都用页面默认（`build_whole_day_specs`，4 个整日规格的 `prime.request.fields` 已更新）。
  - 预热之前先等间隔；被间隔挡回不算已预热。
  - 预热的本地故障只让这张整日报表冷却（`_hold_unsent`，原因 `unsent:prime_<code>`），全省照常。
  - UI 查询或导出时，在点"查询"之前填表失败的，标成 unsent（`transport._ui`）。
  - 测试：预热等间隔、失败只冷却本报表、其余照常；真浏览器：填表失败即 unsent，查询没被点。全套 522 项（两个 venv）和真浏览器 23 项通过。
- **还要验收确认：** 页面在只设日期、节点为空时，点查询是否会发出请求。如果页面要求先选节点，预热会等不到答复而失败，届时只有这张整日报表冷却。

## 11-01 13:2x r14 结果 → r15

- **通过：**
  - 日前整日预热（只设日期、节点留空）26.1 秒成功：页面在节点为空时会发出请求。
  - 日前整日 03-29 两种价格类型回放成功：一条原件 17,849,837 字节，124,320 条记录，1,295 个节点，每个节点 96 个时刻，price_type 正确。
  - 13:23:27 计划换页成功。
- **崩溃：** 换页后轮到实时整日预热。它的身份（10-28，template_prime）已被 r12 的 attempt 31 用过（那次在点查询之前就失败了），被 `consumed_identity_requires_review` 拦住。随后 tick 把预热任务交给 `_isolate_item`，取 `task["period"]` 时 KeyError，进程以 1 退出。launchd 重启后又崩了一次，没有发出任何请求，也没有写下封锁。
- **r15：**
  - 预热不再算"只能请求一次"的身份（`PRIME_SCOPE`）。它是重新读一条已采过的记录，让页面露出自己的请求格式，答复只存为样本。旧系统每次重启后也会这样重新截获一次。所以 attempt 31 不需要复核，下次预热会照常发出。
  - tick 里预热任务的故障不再交给 `_isolate_item`：登录、权限、限流、网络类的答复照常封锁全省，其他只暂停这张报表（`prime_<code>`）。
  - `menu_label_not_visible` 带上诊断信息（`Fault.detail`）：第几层、菜单文字、是叶子还是展开图标、目标的宽高和矩形数。会写进 `discovery_holds` 和 `options_discovery_held` 事件。0f64 在 r14 里仍然报这个错，所以悬浮菜单不是根本原因，下次用诊断定位。
  - 测试：冷却后同一预热照常发出；预热遇到 unknown 故障只暂停本报表、不崩溃；展开图标尺寸为 0 时，错误里带出第几层和宽度。全套 523 项（两个 venv）和真浏览器 24 项通过。

## 11-01 13:35 安徽转入 r15 持续运行

- r15 专门验收全部通过（预算用完后正常退出，没有封锁、没有 4xx、没有回显隔离）：
  - 实时整日 05-07：一条原件 19,474,129 字节，1,399 个节点，每个节点 96 个时刻；
  - 日前整日在 r14 已通过；
  - 重要通道、联络线、6d80、实时断面约束：第 1 条走 UI，第 2 条模板回放 0.1 秒；
  - actual_load 14 天回放。
  - 1404 整日没有待采，0f64 这次没轮到选项发现，这两项留待持续运行中观察。
- 用户批准持续运行，原话"30000（约一个月）"：extend-budget +30000（共 30,055），`prioritize --clear`，旧 keeper bootout，旧系统三个单元都不加载，rollout=false。13:35:47 bootstrap r15（`accept`）。
- 报告邮件：用户选择"现在就做桥接，做好前先暂停旧报告"。旧的 `com.yunyu.anhui-collection-report-send` 已 bootout，由安徽会话参照山西 report_bridge 写桥接，调用 r15 的 `status --mail`。该命令的字段保持稳定，改动要先告诉安徽和山西会话。
- 积压 8,728 条（含实际负荷、系统负荷预测各 285 天的重采，用户选"让PMOS重新采"），每天新增约 532 条任务和 11 次选项发现。

## r19：补早期月份（2026-11-01）

用户原话："同意出 r19 补早期月份，预算加 150000"。

**缺口。** 安徽的交接是 `legacy_pending_only`：按天的报表（`seeded`）从规格的开始日期建任务，多选项报表只接手旧系统还没做完的候选，加上每天的新日期。旧系统从新日期往旧日期采，冻结时这些报表才采到 8 月底～9 月。于是 15 张多选项报表从 1 月 1 日起的几个月既没有任务，也没有旧系统记录。其中 4 张有整天版本覆盖，另外 11 张没有。任务做完后台账会显示"已追平"，邮件也写"正常"。

**做法。** 新增离线命令 `backfill`（`--dry-run` 只打印计划）：

- 把这些报表加进 `seeded`，从规格开始日期建任务。旧系统已完成的日子不建，`held`、`unknown` 的身份照旧不重发。
- 在 `state/backfill_before` 记下每张报表的边界（已知的最早日期）。边界之前的日子是回补日。

运行时：

- `_next_sendable` 分两轮：先是所有报表的边界之后的日子，再是回补日，选项少的报表先做，每张报表内从新到旧。这样一张报表的几万条回补不会挡住别的报表的新日期。
- 回补日需要模板时，直接用最新一天预热（`any_listed`，结果只存样本）。模板和日期无关，而早期日期的下拉框可能不列这个选项，甚至什么都不列。
- `_backfill_discovery`：有选项发现的报表，回补日每 7 天读一次下拉框（约 36 次/年），发现的选项当天建任务，下一次每日播种补上全部日期。读取失败只暂停这张报表的发现（`discovery_holds`），任务照常。
- `seeded` 报表以前不再读新一天的下拉框（`_roll_forward_options` 直接跳过），现在照读。

**计划（2026-11-01 只读核算）。** 最多 132,032 次请求：

| 报表 | 边界 | 回补天数 × 选项数 | 最多请求 |
|---|---|---|---|
| `publication_9dda25255779e435` | 09-15 | 257 × 2 | 514 |
| `publication_22c779b5bdc2edc8` | 09-15 | 257 × 2 | 514 |
| `renewables_output_actual` | 08-28 | 239 × 3 | 717 |
| `renewables_output_forecast` | 09-10 | 252 × 3 | 756 |
| `generation_types_energy_count` | 09-16 | 258 × 4 | 1,032 |
| `publication_9d35ad5e2d74c67c` | 09-25 | 267 × 20 | 5,340 |
| `publication_6d80c3d4514ccfc2` | 09-15 | 257 × 44 | 11,308 |
| `intertie_actual_huan_i` | 09-19 | 261 × 44 | 11,484 |
| `important_channel_transmission_actual` | 09-29 | 271 × 85 | 23,035 |
| `queryRqscjjcqfslj` | 09-08 | 250 × 121 | 30,250 |
| `real_time_transmission_constraints` | 09-24 | 266 × 177 | 47,082 |

这是上限：当天不存在的选项会答空（联络线最近每天 15 个有数据、其余为空）。

**还没覆盖。**

- `6d80` 的选项值是内部 ID，没有下拉框发现，只能用现有 44 个选项。只存在于早期的机组不会被发现。
- 旧系统的 `held` 326 条、`unknown` 14 条仍算已消耗，要采需要用户单独批准。
- 按月的报表不在这次范围内。
