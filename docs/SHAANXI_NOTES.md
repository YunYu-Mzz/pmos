> 2026-11-04 从 `experiments/pmos_shaanxi_20261102/` 合并到主线。本文原样保留，文中的 `evidence/` 指那个目录下的 `evidence/`，它是线上运行记录，没有进 Git。文中“独立副本、不改主代码”是合并前的约束。

# 陕西 PMOS 独立实验副本

用户授权复制 PMOS 并尝试适配陕西；主代码由 Claude 维护。本目录是独立源码快照，不含符号链接，所有改动只能落在本目录。

2026-11-02用户进一步授权“开始适配吧，一直到把陕西的采集也接入到PMOS”。目标扩展为真实接管，仍不修改原PMOS或陕西主代码。允许必要的只读队列/原件核验、独立副本实现和有证据的原owner安全切换；不将此前取消的登录测试恢复。

不读取私密配置/profile值；不启动第二个同账号worker、不自动重登、不重发旧失败/unknown、不改变其他省与网络。不套用其他省自动恢复/重试政策到陕西。正式派发须固定代码指纹、导入已消费/保护事实、排除原worker并持其内核锁、原生共享轮次锁和健康keeper；保留原件、旧队列和回滚事实。没有这些前置证明时仅允许离线/只读命令。

目录登记、夹具测试通过、原件回放通过和上线验收分别报告。215条目录不能声称全部适配或全部历史完成。

用户最新授权要求直接使用PMOS、去掉旧队列执行层。当前为SQLite前沿和直接浏览器通道；原journal仅冻结历史和显式回滚导出，不恢复运行时queueStep/executeQueue/queueSeed。非认证异常隔离本任务，取消残留加载也不刷新或重发旧身份。真实401与原keeper健康保护保留；固定release见README。

2026-11-03用户要求逐项处理异常、能采则采。允许离线核验并补记已有成功原件；只有明确证明未发出任何请求的菜单前置失败，才可在逐项人工审核后一次重入，保留旧失败回执和attempt，记录sn_exception_reviews及released_attempts。此例外不允许重放有请求、拒绝或在途证据的旧身份。旧快照保留实际捕获时间；重新采当前目录必须用明确的新snapshotRun，不伪称重建旧快照。仅放开已审核来源的新日期，不重发该来源的旧unknown。

2026-11-03 16:41恢复核查：15:10 keeper身份b39f234f43c4e90e6cf063267cc3820aca17e5c29d9fc2ceada82d49722e1dce只有已结束的ERR_ABORTED及ERR_SOCKS_CONNECTION_FAILED，无响应、无pending，已精确归档全局保护，原回执不变。原keeper同PID1240924自动结清待审并用全新身份f6a940cc8d1744ef58e8141f09a61890e8e21347b97d6fdd77e86ef98f3efec4验活：SDK HTTP200/status0，但getFiles HTTP200/status401“请求未授权！”，当前auth_expired保护不可清除，等待用户重新登录。PMOS已安全停为inactive，旧worker仍inactive，727项pending保留；原Chrome18942、observer184332、keeper、网络、主代码和journal均未改。证据实例reviews/resume_terminal_socks_20261103/，本地evidence/direct/resume_terminal_socks_20261103_blocked.json。恢复脚本是本次精确事件的一次性操作，不得重跑prepare/archive或重发任何旧身份；用户登录后需对新的401事件另做审核及新验活。

2026-11-03 16:55用户确认重新登录后已恢复：登录时原三个工作目标均被关闭，仅48FB3B67C4D26A0D2736482D47A9B8F8首页保留。原Chrome18942和observer184332不变；重建默认collector FE6D01E6220E93E4833D199DCA8FA11F、综合查询24652381FCDF26101A7EF0DB0122D9B3，keeper改用原保留首页48FB3B67C4D26A0D2736482D47A9B8F8，唯一临时launcher已关闭。16:50原生菜单新身份ad642bad7a5b2adf9abec07f8dfc7bd958c0b0b8fe4cc1489014c5d71535446d验证SDK200/0、业务200及3行基准一致，旧401原件保留未重发。独立SQLite215条来源及registry_metadata的工作页绑定更新，任务计数和历史原件不变。仅为清理原失效工作页绑定安全重启keeper，当前PID1589322；handoff启动身份已更新，release仍20261103_direct_v22/SHA36dcae26d0096bc438bf89275d54aa85e25eb980a3e6ed4c8faae58157357ded。PMOS PID1589545于16:52:53恢复，16:55:33真实新增44 saved、0 failed，首21个原件SHA及公共账本核验0错误；16:55:11自然定时keeper_1793696107025_37ab8297也完整通过，session_block不存在。此时725 pending、1 running；原旧worker仍inactive，原journal、主代码、网络、邮件均未改。间隔仍1秒、保活300秒。证据实例reviews/login_resume_20261103_after1641/，本地evidence/direct/login_resume_20261103_acceptance.json。当前恢复脚本和旧脚本均为已使用的一次性精确事件审核，不得重跑或复制成自动重放器。

2026-11-03 20:29浏览器通信修复已验收：仅独立副本先v23再v24，当前release 20261103_direct_v24，SHA3de4271709adee69ae7c5b625d2638399c6b7513f8605e6177b16978568be6bb，唯一PMOS PID1639497。默认业务页Runtime.evaluate及Memory.getDOMCounters曾30秒超时，浏览器管理命令1ms、综合查询页3ms；唤醒原页面后简单读取24ms。另原生初始化selectCol在网页ResourceTiming中约0.15秒完成，但CDP事件约54秒后才回传，初始化整轮55.7秒，旧45秒窗口会误挂起；尚未定位到Chrome内部的具体延迟机制，不应断言IP、网站封禁或登录失效。

v24每次只唤醒已准入采集目标（Target.activateTarget、Page.setWebLifecycleState active、Emulation.setFocusEmulationEnabled）；keeper目标禁止进入此路径。纯DOM检查限时5秒/通道7秒，CDP超时只记录方法等元数据并重连独立broker，不重启Chrome、不重发已发原生身份。open/monthly_menu观察90秒，其余数据请求45秒、下载90秒；非认证异常仍逐项隔离。两端46项Python与22项Node检查通过。20:18:26上线后至20:29:03真实新增213 saved、0 empty、0 failed、0 CDP timeout，跨4个来源；前192原件SHA及公共账本一致，0错误。最后2分钟54保存，仅是短窗，不可当整小时已采1620。20:22及20:27自然保活成功；Chrome18942、observer184332、keeper1589322身份、原journal SHA9d462963264a290c7a267f2884778b939bec8e6e9204f1edc8f2ffb47fb596f3、主代码pin和报送入口SHA均保持。旧120个attempt和13个held文件复核未改，旧失败不重试。20:29仍180 pending、1 running，不能声称全部历史完成。证据实例reviews/cdp_timeout_20261103_v24/acceptance.json、前段诊断reviews/cdp_timeout_20261103/；本地evidence/direct/cdp_fix_v24_acceptance.json和cdp_fix_v24_performance.json。部署与诊断脚本为已使用的一次性精确事件操作，不得重跑。


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
