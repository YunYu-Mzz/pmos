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
