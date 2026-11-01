# 辽宁迁移：设计、当前进度与切换步骤

2026-10-31。目标是让 PMOS 接替辽宁旧采集器。本文件只描述离线已完成的部分和切换时需要用户批准的步骤；**本轮没有网站请求，没有操作服务器、浏览器、旧采集、保活或旧队列。**

## 设计取舍

1. **不重发是迁移的唯一硬约束。** 旧采集器已经发出过的任务（saved、empty、denied、failed、unknown、在途）在 PMOS 里一律是墓碑。引擎遇到旧墓碑时把任务记为 `skipped`（原因 `legacy_consumed:<旧状态>`），不发送，也不当异常阻断全省。PMOS 自己发过但未确认的身份仍按原设计报异常等人工审查。
2. **身份必须逐条相等，才允许迁移该来源。** 规格从旧生产 manifest 生成，用 `identity_contract` 声明“哪个请求字段是周期、哪个是维度、以及它们在旧身份里叫什么”（`dimension_fields`），引擎计算的身份必须与 `queue_compat` 从旧记录导出的身份完全相同。只要有一条旧记录映射不上或不相等，该来源就不进切换范围。
3. **历史证据缺口不阻断迁移。** 前几轮的原件/资格缺证（51个来源缺自身原件等）属于历史档案完整性问题；缺证来源不进首批范围即可，不需要先补齐全部历史证据。
4. **切换是整省的。** 旧采集器在一个进程里跑所有通道并独占同一账号和浏览器，不能让新旧两边同时发请求。切换时旧采集整体停止；PMOS 只接管已证明身份相等的来源，其余通道在迁移完成前暂停，不由任何一方采集。
5. **交接由回执授权，预算硬上限。** 导入旧墓碑后的台账默认不能派发；只有写入一次性的交接回执（旧owner停止证据、导入的快照SHA、来源范围、请求预算、代码SHA、用户批准）后才能发送，而且只发范围内的来源。预算计入全部网站请求（采集、样本、保活、探测），在记录尝试的同一事务里检查，用完即停。
6. **三种控制语义分开。** `stop_all`：任何请求都不发，包括保活和挂起时的保活；`paused`：暂停采集、保活继续；阻断（block）：故障，按故障分类决定能否保活。

## 已完成（离线）

| 部分 | 位置 | 状态 |
|---|---|---|
| 旧 manifest → 规格生成 | `pmos/migration/liaoning.py` `build_specs` | 40个日/月报表来源（pdbln 报表组件，`queryParam[日期字段]`、`queryParam[选择字段]`、`initData()`，与旧浏览器代码一致），已写入 `provinces/liaoning/datasets/`，均未启用 |
| 身份一致性检查 | `parity` | 本地10/23快照：40个来源11,278条旧记录，11,267条与引擎身份完全相同；34个来源全部相等 |
| 切换计划 | `plan_cutover` | 可切换32个来源（另2个旧采集正挂起该来源，6个身份不全），约8,600个未消费任务；无在途旧任务、无全局挂起 |
| 交接与执行 | `apply_cutover`、`Ledger.grant_handoff`、引擎 | 新实例台账导入全部旧记录为墓碑（含范围外来源），写交接回执；引擎按回执范围采集、跳过旧墓碑、预算用完停、`stop_all` 全停 |
| 测试 | `tests/test_liaoning_migration.py` | 8项：规格形态、身份相等与额外参数检出、在途/挂起阻止计划、无回执不派发、回执须与导入快照一致、跳过旧墓碑、范围外拒发、预算硬停、stop_all；全套363项通过（1项可选浏览器测试跳过） |

身份不全的6个来源（`realtime_node_price`、`dayahead_power_and_average_price`、`sitewide_2010f5bbae811586`、`sitewide_90426958a40aec46`、`sitewide_c1a062960820eff8`、`sitewide_e814f8cf448bfafb`）正好对应旧队列里11条只有结果、没有查询参数的已消费记录。第五轮身份桥能复原它们，但旧辽宁代码在10/30之后又改动过（`advanced_tasks.py`、`server_verified_sources.py`），身份桥按设计因源码漂移拒绝运行，所以这6个来源暂不进范围。

## 首批切换前还缺的工作（按顺序）

1. **用切换时的冻结队列重算计划。** 本地快照是10/23的；真实切换必须在旧采集停止、无在途任务时，从服务器读出声明的队列文件（只读）再算一次 `plan_cutover`。`apply_cutover` 会拒绝计划之后有变化的队列。
2. **日报通道（系统负荷等原44项）。** 旧日报用 Crawlee 持久队列，本地没有它的快照。需要一份冻结的只读导出，补 Crawlee 队列的解码和身份一致性后才能迁移；在此之前切换会让这条通道暂停。
3. **伴随请求计数。** 组件调用（`initData`、菜单、字典、SDK 换票）一次操作可能触发多个请求。上线前要按操作窗口记录页面实际发出的每个请求并计入预算和间隔，不能把一次操作当一次请求。
4. **浏览器所有权。** 旧采集服务自己拥有 Chrome，服务退出会关闭浏览器；PMOS 只附加到独立运行的浏览器（`deploy/pmos-browser@.service`）。所以切换时 Chrome 必然重启一次，**需要你重新登录（可能要短信验证码）**。
5. **保活报表。** 选一个已在范围内的来源作保活（例如日报通道迁移后的系统负荷），并在首次采到原件后才能续期。
6. **其余来源。** 公告列表63个、补充11个、成员3个、周一锚点1个、无参数规则4个、身份不全6个，逐类补身份一致性后再加入。
7. **回滚回执。** 回滚时要把 PMOS 期间已消费的身份交还旧侧，避免旧采集重发；需要确定旧队列可接受的写法，或回滚时这些身份由旧侧跳过的方法。

## 切换步骤（每一步都需要你单独批准）

1. **冻结。** 在旧采集已结算的边界停止旧采集服务（会关闭浏览器）。确认无在途任务。
2. **只读导出。** 读取声明的旧队列文件和 manifest，记录 SHA；在服务器的新 PMOS 实例目录运行 `plan_cutover`，确认 `ready=true`、范围和排除原因。
3. **启动独立浏览器。** 用原 profile 启动 `pmos-browser@liaoning`（仅本机回环调试端口）；你在浏览器里登录。
4. **交接。** `apply_cutover` 生成新台账和交接回执：范围、请求预算（建议首批5次：3个未消费周期 + 2次保活，与 ACCEPTANCE 一致）、代码SHA、你的批准原文。
5. **小预算验收。** 启动 PMOS 工作进程，预算用完自动停止（`handoff_request_budget_exhausted`），观察不超过45分钟。核对每个请求、原件SHA、间隔和保活。
6. **扩大或回滚。** 验收通过后另行批准更大的预算与持续运行；失败则 `stop_all`，保留全部新事实，按回滚回执恢复旧侧所有权。

复跑本地计划（只读，结果写在本项目 `var/`）：

```sh
cd /Users/yunyu/Desktop/sd_crawler/pmos
python3 -c "import json; from pathlib import Path; from pmos.migration import liaoning as m; print(json.dumps(m.plan_cutover(Path('../liaoning')), ensure_ascii=False)[:2000])"
```

## 2026-10-31 晚：按服务器现状重算，并补齐切换所需代码

**重算（只读）。** 22:36 从 launch-advisor 只读复制了以下文件：
- `queue.json`、`active_manifest.json`；
- 两个 10-16 任务文件；
- `lncrawler/*.py`、`*.js`；
- `operations/*/issued.json`（9,288 个）。

没有发网站请求，旧采集照常运行。

- **发出记录补全（`issued_identities`）。** 已消费的结果在队列里不保存查询参数，但旧 worker 发请求前会把每个 job 写进 `operations/<id>/issued.json`：key、dataset、params、period、selection、page。PMOS 按 key 读回这些 job，用同一套解码得到身份。同一个 key 对应两个不同 job 时一律不采用。
  - 结果：补回 7,564 条，冲突 0 条，未解决 0 条。
  - 40 个来源中有 39 个与旧身份逐条相等。
- **切换计划 ready=true：** 可切换 37 个来源，共 20,946 条旧记录。其中已消费 7,434 条（只作墓碑，不重发），未消费 13,512 条。
  - 排除：报表引擎线 024 和 068（旧系统 `legacy_source_hold`，最后迁）、`realtime_node_price`（节点列表不在规格中）。

**按辽宁生产行为补齐的代码与配置（辽宁会话 22:4x 确认）：**
- **只停控制器。** 停 `liaoning-crawler.service`，**保留** `liaoning-browser-owner.service`：它的 Playwright 驱动一停，Chrome 就会跟着关掉。
  - 浏览器和登录因此保留。
  - 页内旧租约 30 秒后到期，旧保活自行停止。
  - 网站会话在最后一次请求后约 35–42 分钟失效，PMOS 必须在 30 分钟内接上。
- **工作页（`browser.work_page`）。** 浏览器里有门户和多个旧工作页。PMOS 从门户用带用户手势的 `window.open` 新开一个自己的栏目页（窗口名 `pmos-liaoning-work`），原生继承 sessionStorage，与旧冷启动的做法一致。之后按窗口名找到这一页。
- **报表从站点菜单打开（`request.site_menu`）。** 与旧 `OPEN_REPORT` 一致：在栏目页解密的菜单里，按路由（以及 infoCode）找到唯一一项，调用 `chAside.handleNodeClick`，由页面自己创建报表框架。41 个报表和 022 已写入规格。
- **策略：**
  - 每个响应后 1 秒再发下一个（用户 21:05 批准）；
  - 空闲 300 秒后保活；
  - 原生拒绝、结果未知：只暂停该来源（`denial_streak` 和 `isolation_streak` 都是 1），永不自动重发；
  - 429、登录失效：全省阻断；
  - 连续两期为空：暂停该数据集；
  - 启用闸门和内存准入：浏览器 cgroup 为 `system.slice/liaoning-browser-owner.service`。
- **保活（回执写入）。** `apply_cutover(..., keepalive_dataset="system_load")`，即 022 系统负荷，与旧 LNCrawler 的空闲保活相同。

**切换步骤**（冻结时间按辽宁会话的建议：明早普通来源采完之后。每步由辽宁会话在用户批准后执行）：
1. 停 `liaoning-crawler.service`（不停 owner）。确认没有在途任务、没有挂起；记录最后一次请求的时间，30 分钟内要接上 PMOS。
2. 在服务器上用冻结后的文件运行 `plan_cutover`，确认 ready、范围和排除原因。
3. 用 `apply_cutover` 生成新实例台账和回执：
   - 首批预算 5 次（3 个未消费周期加 2 次保活）；
   - 实例目录写 `browser.json` 覆盖 profile 路径；
   - `keepalive_dataset="system_load"`。
4. 启动 PMOS（`systemd-run … python -m pmos liaoning accept`），观察工作页的打开、报表通过菜单打开、每个请求和间隔。
5. 验收通过后，另行批准更大的预算和持续运行。失败则 `stop-all`，再由辽宁会话按原方式恢复旧控制器（浏览器一直没停）。

## 11-01 零点暂停后的改进（辽宁会话同步）

00:03 网站零点刷新 SSO 令牌，正好碰上一个页面操作。闸门没等到确认，这个通道被暂停了 1 小时；实际上业务请求一个都没有发出。PMOS 照辽宁的修复补了三处，三省通用：

- **零点静默窗口：** `policy.quiet_windows: ["23:58-00:10"]`，北京时间，可以跨午夜。窗口内不开始业务请求，也不执行排队的命令，只保活，每次最多等 60 秒再检查。辽宁、安徽、山西都已配置。
- **没发出去的不长期暂停：** 闸门能证明没有业务请求发出（unsent）时，这一项不算消费，数据集冷却 10 分钟后自动重试；连续第 3 次才长期挂起，等人复核。一次成功会清零连续计数。确实发出过、结果不明的请求仍然永不重发，走的是另一条路径。
- **邮件醒目提示：** `status --mail` 增加 `stalled_minutes` 和 `headline` 两个字段，原有字段一个不少。`headline` 的取值：
  - "已暂停：<原因>"（有 block 时）；
  - "已暂停（paused/stopped_all/idle）"；
  - "已停滞 N 分钟"（超过 30 分钟没有业务答复）；
  - "正常"。
  
  邮件桥可以把 `headline` 放进标题。

## 11-01 午后：带上安徽切换的经验

- **发布滞后（与安徽同一规则）：** PMOS 会把空答复和一天没结束时的不完整答复当成最终结果，以后不再请求。辽宁旧系统是"补到今天"（`schedule.target_end` 等于今天），所以会把当天还没发布的实际出力记成空，这正是辽宁经验 22 第 5 点。
  - `migration/liaoning.publish_lag`：预测、日前、计划类（不含"执行情况"）等 1 天，其余等 2 天，任何报表都不采当天。构建器写进 `period.lag_days`。
  - 50 张日报规格已写入：17 张等 1 天，33 张等 2 天。
  - 交接时种下、但日期太新的旧待采任务原地等着，不发送、不消耗身份（安徽 r3 起的引擎规则）。
- **安徽切换中修好、辽宁也受益的通用改动：**
  - 页面操作前先切到前台；
  - 菜单在点到叶子之前失败，记为未发出；页面操作连续 3 次失败就停下等人；
  - 未发出的尝试不计入预算；
  - 找门户时忽略地址里的动态参数（辽宁的 `open_from` 也是 `/#/dashboard`）；
  - 计划换页失败时仍保活；
  - `prioritize` 可以按配额做专门验收；
  - 追平后进程不退出；
  - 驱动内存模块认可的 SHA。
- **仍待用户决定：** 旧系统滞后 1 天采到的空结果作为墓碑导入，PMOS 不会重查。

## 11-01 下午：节点电价和维度车道接入 PMOS（用户选"先不切，补完节点电价再切"）

服务器上只读重算（13:40）时发现：PMOS 只能接 44 个报表类来源中的 37 个。节点电价、维度车道、报表引擎线、列表类等 81 个来源，冻结后都会停采。用户选择先补节点电价再切。

- **维度车道导入（`migration/liaoning`）：**
  - `dimension_jobs`：读 `state/dimension_coverage_20261024/operations/*/issued.json` 和 `receipt.json`，只取 stage=data 的业务请求，字典读取不导入。值为 null 的参数（1c01 的 name）不算查询参数。
  - `dimension_options`：每个来源的选项，取它自己的选项字段（大多是 name，1c01 是 type），把维度请求里出现过的和 runtime 里当天的清单合在一起。
  - `build_specs`：维度来源的规格按这些选项展开；0400 没有代码规则，按维度车道自己的两个参数（mdate、name）补上。
  - `legacy_rows` 把维度请求作为旧记录导入，来源为 `DIMENSION_OPERATIONS`。
- **只剩任务键的记录（`task_key_identities`）：** LNCrawler 的任务键是 `liaoning:2026:expanded:<数据集>:` 加语义身份 `{"period","selection"}` 的 sha256。一条既没有查询参数、也没有 issued 记录的旧记录（e6a2 的 10-16 prior outcome），在 2026 年的日期乘以规格选项的范围里，恰好有一个原像能对上，就用它；否则仍然不处理。
- **采得太早的空结果（`early_empty_keys`）：** 用户原话（经辽宁会话转达）是"10-31、11-01 这类当天或第二天采到的空结果，当作未采处理"。按日的，采到时距日期不足 2 天；按月的，在月底后 2 天之前采到的：都作为未消费导入，PMOS 按滞后天数再采。10-23 那批：原键的空结果同样作为未消费导入；重查键的结果是 saved，算已消费，PMOS 不会重发。
- **采集顺序：** 选项在 100 个以上的来源（两张节点电价，每天约 1,324 个节点）排在范围最后，其他报表每天的新日期先采。
- **规格：** 维度来源写入全部选项：
  - 两张节点电价各 1,324 个节点，实时等 2 天、日前等 1 天；
  - 39c8 有 8 个选项，46fa 2 个，7d2f 9 个，931a 7 个，e6a2 2 个，1c01 2 个；
  - 0400 和 e6a2 原来是候选规格，改为构建器生成的完整规格。
- **服务器只读计划：** ready，范围 40 个来源。旧记录逐条身份相等：实时节点 6,535 条，日前节点 5,187 条，1c01 898 条，e6a2 610 条。只有两条报表引擎线（e38351fe、0353290a）按 legacy_source_hold 排除。
- **仍然接不住、冻结后会停采的：**
  - 三条报表引擎线（e38351fe、0353290a、033），因为没有 FineReport 会话处理；
  - 电力日报和附件通道；
  - 列表类 63 个、additional 11 个、member 3 个；
  - 维度车道每天的字典读取：新出现的节点暂时不会补进来。
- **存储：** 节点电价每条约 106 KB，剩约 79 万条，约 84 GB。PMOS 在剩余空间低于 2 GB 时会暂停整个省份。
- **切换工具：** `tools/liaoning_cutover.py`。
  - `plan` 只读；
  - `apply` 要求 liaoning-crawler 已停、liaoning-browser-owner 还在运行；它建新台账，写 browser.json（指向浏览器属主的 profile，并开启进程核对）和 SHA256SUMS；保活用 system_load。

## 报表引擎三条线（后补；用户 11-01："先切 40 个来源，引擎后补"）

旧系统的做法，由辽宁会话 11-01 只读核对，路径都相对于 `lncrawler/`。

| 车道 | 数据集 | 名称 | 页面源 | 打开方式 |
|---|---|---|---|---|
| engine068 | sitewide_e38351fe661a034a | 非市场机组总出力 | lnsnxh_fschjzsjcl | 从门户打开 |
| engine024 | sitewide_0353290a57044c3e | 除水电外非市场发电总加预测 | lnsnxh_csdwfscfdzjyc | 私有引擎，靠映射打开 |
| engine033 | nonmarket_generation_forecast | 非市场机组总出力预测 | lnsnxh_fscjzzclyc | 私有引擎，靠映射打开（不在 manifest 里） |

- **打开报表页：** 新开一页，共 3 次请求（文档、finereport.js、finereport.chart.js），两个脚本都核对 sha256（`engine_bootstrap_068.LIBRARIES`）。
  - 068：目标地址由 `engine_bootstrap_contract.mapped_target` 根据菜单回执算出。
  - 024/033：地址取自映射槽位 `window.__lnIsolatedEngineMappings20261029[source]`，要求 path 为 `/px-basesystem-reportform/decision/view/form`，查询参数只有 ref_c、ref_t、viewlet。
  - 然后执行 BOOT（默认日期 2026-10-09，单位"单位:MW"），再执行 `server_engine_collection.INSTALL_QUEUE`：把 FR.ajax 改为排进队列；form_getsource、js_log、info/collect 不发出。
- **映射（只有私有引擎）：** `engine_mapping_lane`。用 webpack 的 require 调原生 SDK `queryIpSpotgoodsInfoBytableName(source)`，发 1 次 POST 到 `/px-settlement-infpubquery/spotgoods/queryIpSpotgoodsInfoBytableName`；返回的地址只存在浏览器里。槽位已有地址时，映射会直接拒绝，所以重新映射前要先在本地删掉这个槽位（`DROP_MAPPING`）。
- **取数（`EngineCollection.dispatch`）：** 一天 3 个阶段，每个阶段放行队列里恰好一个请求，path 都是 `/px-basesystem-reportform/decision/view/form`：
  - parameters_d（fr_dialog，POST）：答复必须是 `{"status":"success"}`；
  - load_content（fr_form，GET）：答复以 `{` 开头并含 `"elementCases"`；
  - load_report_content（fr_form，POST）：`daily_values` 要求 currentPage=1、totalPage=1、htmlTag 是 12 行 16 列的表，得到 96 个点。表格只有 `[[''],['']]` 时是空白报表，属于还没发布，按"未采"处理。
  - 发送前先设参数：`_g().parameterEl.getWidgetByName('日期').setValue(日期)`，再 `parameterCommit()`。
- **会话结束：** parameters_d 返回 HTTP 200 的 FineReport 报错页（约 2.7 KB，含 `Dec-Session_Lifecycle_End`）。
  - 同一会话里，闲置 11.5 分钟以内从没失败，65.7 分钟以上全部失败。
  - 旧系统超过 600 秒没有请求就重开：先关旧页，私有引擎再重新映射，然后重新打开报表页。
  - 兜底：不是期望的答复体就暂停。
- **身份：** 一天一条。
  - e38351fe、0353290a：键与普通队列相同，`liaoning:2026:expanded:<数据集>:<sha>`，参数 `{"mdate"}`。
  - 033：`nonmarket_generation_forecast:engine_history:<日期>`。
  - 结果记在 `state/server_engine_collection_20261023/<数据集>/queue.json` 的 outcomes 里。每个阶段有自己的操作目录，导入时按 issued.json 里的 `job.key` 归到同一天。
- **033 和电力日报：** 033 与电力日报 power_reports 共用这个数据集，电力日报有 152 天（05-18～10-16），引擎有 153 天，合起来是全年。
- **PMOS 实现要点：**
  - 新开一个属于 PMOS 的引擎页；
  - 用页面自己的 FR 排队，逐个放行三个阶段并核对答复；
  - 闲置超过 10 分钟先重开；
  - 导入旧引擎队列作为墓碑；
  - 从 11-02 起补采。

## r10：63 个披露栏目和它们的附件，加两张周报（2026-11-01）

第四优先的前两项。周报在上一个提交里（`--lane weekly`）。这一版接列表和附件，工具是 `tools/liaoning_daily_lane.py --lane lists`。

**规格和旧系统逐项核对。** 63 个 `pub_*` 和旧清单的 `list` 一一对应（`source` 相同，菜单 guid、接口、页大小都比过）。查出两个错，都已改：
- `pub_jyjgJbxx`：应该是 `tabFlag: true`、`getDataJY`。页面的 `getIpc` 对 `jyjgJbxx` 打开 tabFlag，旧系统的 `native_list_contract` 和归档也是这样。
- `pub_dwqyqyjbxx`：10-23 网站把这个菜单搬到 `operationOrganizationQuery`（旧系统 `state/menu_recheck_20261023/organization_route/receipt.json`），规格还是旧组件名。

13 个 `candidate` 改成 `sampled`，证据是旧系统每天把它们列到底。

**每天列一次（`documents.daily_listing`）。** 交接之后，`seed` 对 `snapshot` 规格只播种一次，文档栏目也不例外，这样列过一次就再也发现不了新文件。打开这个键的规格每天播种。山西的栏目没打开，行为不变；是否也要打开由山西会话决定。

**停在已知页（`stop_at_known`）。** 第一次列到真正的末尾，之后遇到整页都已知就停。只读回放了旧系统归档的约 1,235 页（10-23 到 11-01）：
- 所有栏目都是新的在前，没有一处倒序。
- 不存在新记录出现在已知页之后的情况。回放报出 3 个栏目 10-29 "会漏"，核对后不是：10-23 旧系统只翻到第 6、7 页，10-29 第一次翻到更深的页，那些是老记录。
- 和旧系统的差别：旧系统每天全部翻完（约 180 页），PMOS 每天约 63 页。老记录被原地改动（guid 不变）PMOS 看不到。

**只下 2026 年的附件（`documents.business_year`）。** 规则照搬旧系统 `attachment_dimensions.expand_records` 和它的派发条件：
- 年份取 `fyear`，其次 `businessYear`，再其次标题里第一个年份，不看发布日期。
- 标题写了别的年份就不取，不管字段写什么。旧系统把这种放进 `scope_reviews` 待审，从没发过。
- `fyear` 可能是发布年，所以标题和这个文件名都只写目标年份、`businessYear` 不矛盾时也算。

回放旧系统全部列表页：PMOS 选中 295 个文件，就是旧队列的已存 292 个加 0 字节 3 个，没有解析失败。其中 9 个已经不在今天的列表上（记录换过文件）。**不取的文件：**
- 标题和字段年份冲突 9 个（旧系统也扣着）：标题写 2025 年的 4 个（市场结构情况、2025年12月交易总体情况、12月零售市场电费结算、12月市场化用户电费结算），写 2027 年的 5 个（年度测试交易公告、申报功能介绍）。
- 2020 到 2025 年 1,199 个，2027 年 3 个。
- 没写任何年份的 328 个，其中 161 个是 2026 年发布的：市场干预情况原始日志 114、通知公告 23、新闻发布 22、其他 2。旧系统也不取。要不要取由用户定，而且要在第一次全量列表之前定：列表走过之后，老记录不会再读一遍。

**墓碑（`list_lane`）。** 只读 `state/attachment_coverage_20261029/queue.json`：
- 已存 292 个：写 `saved`（身份 `document_identity`，按文件 id），不再下载。`import_late_legacy` 现在同时写 `file_tombstones`。
- 0 字节 3 个：`2026年7月` 的三份并网考核文件。旧系统 10-29 发出，网站回 HTTP 200、0 字节，旧系统复核后记 `settled_empty_http200_attachment_preserved_no_retry`。默认写 `empty` 不再发；`--resend-empty` 才留给 PMOS 重发。墓碑收不回来，所以这要在第一次 `apply` 之前定。重发如果还是 0 字节：`empty_file` 让这个栏目暂停（`isolation_streak: 1`），告警发出，放开之后才继续，其余两个也等着。
- 年份冲突 9 个：不写墓碑，`business_year` 不取，计划里列出。
- 旧的列表页不导入：列表属于当天，PMOS 第一次自己列到底，约 180 个请求。

所以默认情况下，第一次全量只发列表请求，一个文件都不下载，要等网站出新文件。

**分两步。** 这些栏目在 PMOS 上一次都没发过（`attempts` 里 0 条）。先 `--only` 试 4 个，通过后再全量：
- `pub_LSTCFDJGCS`：9 个文件全有墓碑，验证跳过。
- `pub_jyjgJbxx`、`pub_dwqyqyjbxx`：验证刚改的两个。
- `pub_ddjg_lgxzkhztjgdckhmx`：4 页，已存 33 个加那 3 个 0 字节。带 `--resend-empty` 时，这是唯一能验证下载的地方。

**还没有验证：** 真实网站上的打开菜单、列表、下载都还没跑过，不重发那 3 个的话下载要等第一个新文件；隔一天再列时这个子系统的会话还在不在（山西出现过子系统闲置后 40302）。

其他：`check_document` 认出 `.pptx`（原来存成 `.zip`，旧系统有 13 个）和 `.ppt`。

## r9：常驻服务和停采告警（2026-11-01）

用户定的第三优先。之前 `pmos-liaoning` 是 `systemd-run` 的临时单元，服务器重启后不会自启；`liaoning-crawler` 又已 disable，所以重启后没人采。旧的 `liaoning-monitor.timer` 已停，5 分钟一次的告警没有替代，只剩每小时邮件。

**服务（`deploy/liaoning/`）。** `pmos-liaoning.service`：开机自启，`Wants`/`After` 浏览器属主；`KillMode=mixed`；`Restart=on-failure`，但退出码 2（封锁等人工）不重启；工作目录和 `PYTHONPATH` 指向软链 `/home/ecs-user/pmos-liaoning-current`。切版本：`stop-all`，等退出，改软链，`systemctl start`。

**告警（`pmos/monitor.py`、`tools/pmos_monitor.py`、timer 每 5 分钟）。** 只读，不碰网站、浏览器和封锁。邮件账号用状态邮件的 `reporting-private/config.json`，不新增凭据。

| 条件 | 含义 |
|---|---|
| `worker_down` | 服务不在运行，也不是人为 `stop-all` |
| `blocked` | 全省被封锁 |
| `held:<报表>` | 报表长期暂停，要人工放开（带冷却时间的不算） |
| `stalled` | 30 分钟没有答复（已追平、等发布、暂停等不算） |
| `worker_hung` | 进程在，5 分钟没更新状态 |
| `disk_paused` | 磁盘不足，采集暂停 |

每种情况发一封，持续时每 6 小时提醒一次，恢复时再发一封；发送失败下次重试。状态记在 `<实例>/monitor.json`。

**没有覆盖。** 服务器整机宕机、断网时告警发不出来，这时只能靠"每小时状态邮件没来"发现。浏览器属主服务 `Restart=no`，它自己崩了不会重启，只会表现为 `blocked` 或 `worker_down`。重启后如果登录失效，也只能告警，要人工重新登录。

## r8：stop 打断在途请求（2026-11-01）

16:21 第 A 步停 r6 时，`systemctl stop` 把 SIGTERM 发给整个 cgroup（KillMode 默认值），Playwright 驱动被杀，一条 0.4 秒的查询等满 45 秒，记成 `unknown / network_error`。辽宁不隔离 network 类故障，于是全省被封锁。

- **以后怎么停：** `pmos liaoning stop-all`，等 unit 自己退出。`systemd-run` 和 `deploy/pmos@.service` 用 `KillMode=mixed`。
- **`release-interrupted <attempt> --approval`：** 只允许 `unknown`、没有 HTTP 状态、原因是 network_error / driver_lost / interrupted 的采集请求。历史记录不改，任务回到 pending，再发一次。要用户批准，因为无法证明网站没收到。不放开的话，worker 轮到它会隔离，而辽宁 `isolation_streak=1`，整张报表随之暂停。

`plan` 的 `never_asked` 改名 `no_original`：它只说明没有原件和日报队列记录，不说明没问过（09-10、09-13 问过，中断了）。

## r7：报表引擎三条线（2026-11-01）

用户定的第二优先。10-23 网站把这三张报表搬到了 FineReport 页面（路由 `spotgoodsSum`）。

**不照搬旧系统。** 旧系统的主页面不能动，所以它另开隔离页，禁用脚本，手工注入 FineReport 库，还从 webpack 里取 SDK 做映射。PMOS 打开报表本来就靠菜单组件（42 张规格在用 `site_menu`），而 `spotgoods` 组件挂载时会自己调映射接口并设 iframe，所以新传输 `finereport` 直接用页面自己的流程：
- 打开：`site_menu` 点菜单项，页面完成映射、文档、脚本加载。
- 取数：保留旧系统验证过的做法。`FR.ajax` 只排队（`form_getsource`、`js_log`、`info/collect` 不发），`日期` 控件 `setValue` 再 `parameterCommit`，三个阶段逐个核对后放行，每个阶段之间停 1 秒。答复校验和旧系统一致：`{"status":"success"}`、含 `"elementCases"`、12 行 16 列 96 点。
- 重开：闲置超过 600 秒（或不知道闲置多久，比如刚重启）时，清掉组件的 `src`，再调它的 `queryIpSpotgoodsInfoBytableName()`，页面重新映射，得到新会话。
- 会话结束（答复含 `Dec-Session_Lifecycle_End`）：不算答复，也不消耗这条任务。这张表冷却后再试，那时会先重开。

**规格。** `set` 里日期仍叫 `queryParam.mdate`，这样任务身份不变，旧记录还对得上。

**计划。** `tools/liaoning_daily_lane.py plan --lane engine`。旧系统完成的日子来自 `report_engine`，033 加 `power_reports`，两张 sitewide 加路由变更前的 `advanced_reports`。

**未验证。** 只在测试页上跑过：真实页面原生加载后 `_g()` 和 `parameterEl` 是否就绪、原生加载时会不会自动查询，都要线上第一次运行才知道。失败只暂停这张报表，不影响全省。

**不做。** 一天 3 个阶段只记 1 次请求（`attempts`），打开和重开的请求也不计入预算，和其他报表的伴随请求一样。

## r6：提前采到的空结果不重采（2026-11-01）

用户在辽宁会话的原话："确认切 r4，空结果不重采，做保活验证"。`plan` 和 `apply` 都带 `--early-empty-consumed`，那 15 天也写成墓碑（`daily_lane(early_empty_consumed=True)`），状态如实记 `empty`，任务原因是 `legacy_consumed:empty`。计划：5,164 条墓碑，21 个日子要采。

这 15 天的归档就是 0 个数据点，不会再有数据，除非用户以后决定补采。`release-early` 用不上，它只能释放 PMOS 自己发过的请求。真要补，要另写一个小命令：把这些 `daily:<报表>:<日期>` 记录改成未消耗，再把任务重开。

## r5：日报和系统负荷 17 张（2026-11-01）

r4 没有部署就被 r5 取代。r4 把 `nonmarket_generation_forecast`（033）放进了组件通道，但 10-23 网站把它搬到了 FineReport 页面（路由 `spotgoodsSum`），旧系统从此把它排除在组件通道外（`runtime_excluded`），组件原件停在 10-16，新日期都来自引擎。所以 033 归第二优先的引擎线，它的新日期和 04-14 都在那里采。下面 r4 这一节里 18 张、5,453 条墓碑、37 个日子的数字，r5 是 17 张，以 r5 的 `plan` 为准。

## r4：日报和系统负荷 18 张（2026-11-01）

用户原话（经辽宁会话）："发给五省统一，按这个顺序排"，这 18 张是第一优先。切换时它们不在范围内，14:31 旧系统冻结后新日期没人采。网站的历史还在，晚几天不丢数据。

**为什么不解码 Crawlee 队列的身份。** PMOS 的身份只由"报表 + 日期"决定，由引擎算出。旧系统已完成的证据是原件，队列只用来识别"处理过却没有原件"的日子。只读核对：队列标记已处理的 4,997 条全部有原件，这类日子是 0 个。17 张的接口路径和查询参数都和原件的 `query_identity` 一致，5 张 `candidate` 因此升为 `verified`，并按 `publish_lag` 设了滞后天数。

**判定（`daily_lane`）：**
- 原件里有数据，或者过了发布期才采到的空结果：已完成，写墓碑。
- 只有当天或第二天采到的空结果：不算完成，PMOS 再采（用户原话"当天或第二天采到的空结果，当作未采处理"）。这样的日子有 15 个，正常每天 96 点，这些是 0 点。
- 队列没处理、也没有原件：PMOS 采。这样的日子有 22 个。
- 队列处理过却没有原件：当旧失败，不重发，并列出来。目前 0 个。
- `nonmarket_generation_forecast`：power_reports 或 report_engine 任一有数据就算完成。引擎 `nonnull_points` 为 0 的不算。
- `system_load`：`raw/system_load` 的记录就是它的原件。

计划（只读）：5,453 条墓碑，37 个日子要采，其中 15 个 10-31 之前的日子是早期空结果，4 个是较老的未答复（`distributed_solar` 10-24、`distributed_wind_actual` 10-26 和 10-27、`nonmarket` 04-14）。

**顺序。** 这 18 张排在范围最前面，新日期先于 80 万条节点电价。

**保活。** r3 其实发不出保活：`system_load` 不在范围内，PMOS 归档里也没有它，`keep_session` 只能记 `renewal_needs_one_saved_period`。r4 把它纳入范围，并在 `province.json` 里设了固定日期 `session.keepalive_period`，所以不必等它的第一个新日期归档就能保活。

**操作（离线，worker 要先停）：** `tools/liaoning_daily_lane.py plan`，停 `pmos-liaoning`，`apply --approval "<用户原话>"`，再用 r4 启动。
