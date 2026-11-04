> 2026-11-04 从 `experiments/pmos_shaanxi_20261102/` 合并到主线。本文原样保留，文中的 `evidence/` 指那个目录下的 `evidence/`，它是线上运行记录，没有进 Git。文中“独立副本、不改主代码”是合并前的约束。

# 陕西 PMOS 直接采集独立副本

2026-11-02，按用户要求移除旧队列执行层。当前为 **PMOS Engine/Ledger → 原生浏览器直接传输 → 一次校验解析 → 原件归档 → SQLite确认及派生任务**。正常采集、分页、分类展开、机组去重、每日补种及报表不调用 `queueStep`、`executeQueue`、`queueSeed`，不读写原 `collection_queue/journal.json`。

源码变更仅本独立目录；未由本次改动修改原PMOS或陕西主代码。原Chrome、登录、observer、keeper、代理和唯一计划邮件链路沿用。旧队列只留作冻结历史和显式回滚输入。

## 运行位置

连接：`ssh mz`（`mz`）。

| 内容 | 位置 |
| --- | --- |
| 当前固定采集代码 | `/home/mz/sd_crawler/pmos_shaanxi_lab/releases/20261103_direct_v25` |
| 原报送包装入口（保持） | `/home/mz/sd_crawler/pmos_shaanxi_lab/releases/20261103_direct_v22` |
| 实例、SQLite、原件和接管配置 | `/home/mz/sd_crawler/pmos_shaanxi_lab/instances/production_20261102` |
| 唯一采集服务 | `pmos-shaanxi-native.service` |
| 已退出的原worker | `shaanxi-queue-worker.service` |
| 原保活 | `shaanxi-keeper.service` |
| 唯一计划报送 | `shaanxi-collection-report-send.service` / 同名timer |

当前代码SHA：`c63e53fa9b755eac1d1bd1ad6d9bb70ea3661fd0fe1752c1aac17ec20e0d54f8`。

## 迁移和验收口径

- 一次性导入43,354个旧任务、20,504份已核验去重原件，保留来源/日期/快照保护、分页组、旧失败和未知身份。32,782条旧派发身份及2,119条附件派发事实留存；304页既存机组数据SHA核验。原PMOS的42,534条兼容历史事实、attempt和预算未删除。
- 在线前沿为同一 `ledger.sqlite3` 的 `sn_*` 表。新分页、分类及每日任务增量入库。原journal的inode、大小、mtime和SHA读回保持一致；PMOS独占原worker内核锁。
- 215个登记来源中209个启用来源全部使用既存真实原生契约通过静态计划编译；6个未准入来源保持。静态编译、目录数、实采任务与全历史完成分别统计。
- 16:30读回确认499个任务（444保存、55空），涉及33个来源，含55份已解析PDF；原件SHA和公共账本一致。任务持续增长，最新值见 `evidence/direct/direct_acceptance_final_v17.json`。这不是499个指标或全部历史采齐。
- 最终版本远端159项离线检查、13项浏览器通道检查通过。静态编译为零网站请求；两份中断确认通过已存原件恢复，未重新查询。辅助进程仍活着但调试连接已关闭时也会重连原浏览器；纯契约异常不再误判成驱动重试。
- 多轮自然keeper成功；Chrome18942、observer184332、keeper405241启动身份保持。旧08:58未知任务及旧来源保护未重发。兼容v6的历史证据保留在 `evidence/`，不能当成直接通道新增实采。

## 异常、间隔和报送

非认证异常隔离本任务，后续新身份继续；迟到回包按原请求ID记证据，不拼入新任务。必要时只取消该采集页残留加载，不刷新、不重放。真实401等待人工登录，不自动重登。原keeper按原300秒节奏优先运行。

保持1秒响应结束间隔，菜单阶段、实际查询及保活结束后均遵守。权威值来自PMOS省份策略；冻结旧登记中的5秒遗留值不参与新调度。最终版本111个原生阶段读回的最小间隔约1.011秒。16:30前5分钟保存51个任务，折合约612个/小时；混合了表格、目录和附件，不能作为固定速度。

报表读取冻结核验基线、PMOS SQLite及新原件，按SHA去重；兼容阶段PMOS原件也保留累计归属。16点计划报告因旧14点连接失败保护被挡，按用户已有“仅恢复全新报告”授权保留旧未明记录后，16:05原计划服务提交全新报告 `93063dce-7ed3-4ccc-9823-915ab29f7d1e`，SMTP accepted：20字段、254条覆盖、PMOS active、未暂停。原签名、SMTP路线、收件方不变；仅独立包装器补充提交前TLS EOF分类，认证/提交阶段未知及证书失败仍保留审核边界。没有重发旧报告；SMTP提交不等于飞书回读已确认。

预算仍为50,005次原生任务操作；一次任务可能含多次SDK/业务HTTP，实际请求另行记录。未改变Linger、开机启动或登录准备。

## 连续日期及离线PDF优化（v19）

同一来源表格连续确认最多64个任务，日期顺序采集并复用当前原生组件；附件每批最多8个后轮换，防止大文件长时间占据采集。当前快照的分页续采和月结算详情仍优先；目录、附件最多等待64个成功/空确认后获得轮次。保活始终优先，不缩短1秒响应结束间隔，不增加并发浏览器请求，不改变异常隔离或身份保护。

PDF完成签名/EOF和SHA校验、原件归档及任务确认后，在同一SQLite事务登记 `sn_pdf_jobs`。单个低优先级离线子进程运行既有解析器，主采集不等待它完成。`validated.json` 的 `pdfParse` 明确记录 pending / parsed / requires_review / original_only；无对应解析器不会报已解析，解析异常保留原件及复核记录。后台进程没有浏览器或网络操作，超时只隔离解析任务；采集服务优雅退出时停止它，之后可以只恢复离线解析。

2026-11-02 17:00:14切换v18，17:10:07再固定为v19。本地及远端各43项程序检查、15项浏览器通道检查通过。v19在官方blob下载已出现时直接核验浏览器落盘原件，免除大PDF在调试通道的第二次整包传输；没有官方blob时仍检查JSON业务错误和真实401。

既存PDF离线复核：原件SHA `975b60a14d3249f810487a359a04ad13d2dafeca85b4fbe5b45e1cd641e65599`，新旧解析CSV的SHA完全一致；后台启动约0.007秒即返回，解析1.32秒，0次网站请求。详见 `evidence/direct/offline_pdf_v19_real_original.json`。

升级前10分钟94保存、20空，折合564保存/小时。v19一小时观察从17:10:06开始；新版本速度以 `evidence/direct/performance_v19_*.json` 的真实时间窗为准。

## 维护

先读 `AGENTS.md` 和 `deploy/README.md`。状态：`ssh mz 'systemctl --user status pmos-shaanxi-native.service'`。

实例 `direct_migration/` 保存导入、升级及零发送复核；`native_turns/`、`direct_commands/` 保存直接派发和回执；`direct_parsed/` 保存校验及侧件；`original/` 保存内容寻址原件。`source_snapshots/` 是冻结历史，不是在线队列。

回滚先停止PMOS，显式反向导出SQLite新事实和原件引用后才能恢复原worker；不得恢复初始快照。报送入口也须恢复到记录中的兼容包装器。详见部署文档。

## 2026-11-02 22:24 人工登录后恢复

19:19旧保活因 `Keeper iframe binding changed` 进入待复核，PMOS随后被 `keeper_not_fresh` 挡住。用户确认重新登录后，只剩原保活目标的首页，两个采集目标均已关闭。本次在同一Chrome重建工作页，仅更新独立SQLite的215条来源绑定和 `registry_metadata`，不改源任务、保护、主代码或冻结journal。现有默认采集页为 `F63398706401C8BCBA2FE6F48A7FEA4B`，综合查询页为 `F88D8139AB23701C37FF6A9048291A5B`；保活目标仍为 `146E9030128A71A2BABCFE555D530F96`。综合查询原生导航函数、市场PXBSX及目标菜单只读匹配；临时启动页已关闭。

原失败保活身份 `c9d09483c4b5d3fe6ae88b499c5982a4bdab99dea39cc7d2c678ed50d4c8bdf3` 保留且未重发。登录后的新验活于22:15:50完成，SDK与业务均成功，三行基准SHA一致。仅重启keeper以归档内存中的旧待审状态，当前keeper PID1240924已写入handoff启动身份；Chrome18942及observer184332保持。22:22:41新的自然定时保活再次通过，1秒间隔、300秒保活、网络及邮件保持。PMOS PID1241307于22:24开始真实保存，首39任务原件SHA及公共账本核验无误。

完整审核在实例 `reviews/login_resume_20261102_2210/`，本地摘要为 `evidence/direct/login_resume_20261102_finalization.json`、`login_resume_20261102_first_tasks.json`。这是本次登录恢复，不代表所有保活页绑定异常已实现自动恢复。显式回滚到旧worker前须另外核对旧registry与这些新页面绑定，不能把旧目标ID直接恢复使用。

## 2026-11-03 异常逐项审核与修复

用户要求检查异常、处理能采的任务。仅独立副本升级v20–v22；原Chrome、observer、keeper、网络、1秒间隔、原计划邮件及主代码保持。

- 补入353个已有成功原件：浏览历史辅助写入被误计为未审核请求的256条曲线，以及打开页面时已经完整返回的97条目录第一页。原件SHA、实际查询参数、96时点、原生筛选、行数和页面解码字段仍由既有校验器验证。旧attempt及原始held回执不改，`sn_cached_recoveries` 独立记录修复；0次网站请求。采集时间保留原捕获时间，不能因补入把旧目录变成新快照。
- 四个综合查询菜单把“日内”改成“实时”，GUID与路径不变。保留原name作为历史别名，以 `nativeMenuLabel` 记录现名，仅修复精确标签匹配。498条原生菜单前置判断失败，经原回执证明无observedRequests/响应/在途/失败请求，逐项人工授权一次重新入队；`sn_exception_reviews`、`released_attempts` 保留原失败和审核依据，原生身份带显式审核编号。该入口不能释放有请求或拒绝证据的旧任务。
- 列表筛选按实际JSON键值比较，保留所有筛选值约束；同步已登记的原生20行页大小及分页控件，避免默认10行状态造成假变更。打开目录已有完整第一页时直接确认，不再多查一次。新当前快照使用独立 `snapshotRun`，旧快照不冒充当前、旧失败不重新请求。分页组包含该批次，继续严格校验总量及跨页唯一性。
- 下载前核对当前目录中精确附件身份，若处于另一同路由目录，先打开正确的原生目录。没有附件或真实拒绝仍保留复核。新增错误明细只记录经过筛选的原生绑定错误，不保存凭据。
- 日前节点电价旧来源挂起来自原采集页的未明请求。只读确认原目标已关闭、原回执没有拒绝信号后，仅放开154条尚未请求的日期；旧5月23日第23页任务和原unknown回执仍保留。不是重放该未知请求。

审核证据在实例 `reviews/exception_review_20261103/`；核心为 `audit_initial.json`、`applied.json`、`snapshot_refresh.json`、`dayahead_node_future_source_release.json`。权限拒绝、安全验证、旧412、过期快照、原分页总量不一致和真正未知请求仍分项保留，不声称所有异常已采齐。当前固定源码、配置与任务事实以SQLite和handoff升级链为准。


## 2026-11-03 22:03 第二轮973项逐项复核完成

仅独立副本升级为20261103_direct_v25，SHA c63e53fa9b755eac1d1bd1ad6d9bb70ea3661fd0fe1752c1aac17ec20e0d54f8。967异常及6受保护任务已逐项登记。183日结算PDF旧412中83份同一fileId已由既存恢复任务保存且原件SHA复核正确；100份符合原有“打开目录412、首次业务下载尚未发出”的严格证明，其中97份当前目录仍在。对97份仅准许在已打开目录中首次下载，不再执行旧拒绝导航；原held任务/回执、legacy_records、旧tombstones保留。首次新attempt无论失败/未知仍消费身份，不能再放开。UnissuedFileLedger仅供本独立DirectEngine使用，普通Ledger默认行为保持。

另5项旧标签页不存在的前置失败（4日期查询+1PDF）通过原observer结果、空任务目录、无legacy原生轮次、无已有attempt证明未发请求，逐项释放。两类证明分别追加sn_unissued_file_reviews和sn_unsent_preflight_reviews，不修改旧消费事实。75项Python检查在本地和远端通过，浏览器22项检查保持通过且JS未改。

上线后58个首次下载因其他正常任务切换页面，在文件下载之前零发送跳过。保留这58个not_sent attempt及零commands事实；只读核验当前同一原生目录的65个2025附件身份与固定方法全部匹配后，一次性恢复这58项并还原审核中保存的旧文件claim。未重发旧导航/失败业务；该轮所有97首次下载及5前置任务最终均saved。当前唯一PMOS PID1666587，原Chrome18942、observer184332、keeper1589322保持。

最终新增148 saved：106PDF、41表格、1目录；106PDF全部parsed。所有148原件SHA和公共tasks账本核验0错误，旧attempt、legacy事实、旧tombstones、main_code_pins、原journal、三原进程与报送入口SHA保持。6项“方式变化”来源仍为网站原回包status0/message暂无权限/data:null，不释放。9份完整单页原回包存在总量变更或跨页重复，仍作不完整证据，不能作完整成功。20项旧附件对应当前版本已保存、266旧快照已有独立当前完整扫描，两者均不冒称重建旧失败身份。其余拒绝/未明/未归属响应/无菜单/未准入为空来源分项保留。证据实例reviews/exception_audit_20261103_round2/acceptance_final.json。apply/deploy/reentry脚本均为已使用的一次性审核，不得再次执行。


## 2026-11-03 data export: server complete, client transfer blocked

User requested the Shaanxi package organized like artifacts/shanxi_data_export_20261028. Server export is complete under /home/mz/sd_crawler/pmos_shaanxi_lab/exports/20261103. Final ZIP: 2860758473 bytes; SHA256 59fb061ff8bee396496aa78d8e82685a0aa2e04395da8769bd3942083f0f0e47. 215 current source folders (209 enabled), plus 8 retired historical sample sources. 279903 business payload files; 280353 ZIP members. All 46529 saved jobs have exact original references. 18511 old-format references were resolved by original validation_input / receipt_directory SHA, adding 18509 raw files without any website request. All initial payload CRC/SHA and all supplemented/replaced entries verified. metadata_finished.json is the final server marker. One excluded offline_root/config/spot_registry.json is non-business registry bookkeeping, NOT a missing original; the actual task original and validated outputs are present.

Local artifacts/shaanxi_data_export_20261103 contains delivery_summary.json, SHA sidecar, metadata_finished.json. Full ZIP is NOT local yet: only .zip.part with 805555200 bytes. First scp lost its SSH connection; fixed host mz:22 then timed out / reported Host is down, ping unanswered, route remains en0 direct. No network setting was changed. Async user question asks whether the deployment computer slept/disconnected or network changed.

Local exec session 18304 runs evidence/export_20261103/resume_download.py: up to 30 attempts every30sec, same host, SSH + rsync partial/inplace only. On success it invokes verify_downloaded_archive.py: exact ZIP SHA, every member CRC, manifest sizes, 215 folders and every saved-job original reference, then atomically renames .part and writes local verification JSON; root index/README CSVs are also extracted. Do not deliver a nonexistent full ZIP link or the incomplete .part. After user restores connectivity, check session and completed file first; resume the preserved partial if needed, without rebuilding or querying the website. If host rebooted, do not auto-start the old collector or alter login; separately validate handoff process identities. Export did not modify any production source/service/browser/login/keeper/network/mail.


2026-11-03 user update: Do NOT transfer the package to the Mac. Deliver only its existing path on host mz . Local resume/download processes are no longer running. Do not restart transfer. Final remote export directory remains /home/mz/sd_crawler/pmos_shaanxi_lab/exports/20261103, final ZIP 2860758473 bytes, SHA256 59fb061ff8bee396496aa78d8e82685a0aa2e04395da8769bd3942083f0f0e47. The user accepts location-only delivery.
