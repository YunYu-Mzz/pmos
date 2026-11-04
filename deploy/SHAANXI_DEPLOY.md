> 2026-11-04 从 `experiments/pmos_shaanxi_20261102/` 合并到主线。本文原样保留，文中的 `evidence/` 指那个目录下的 `evidence/`，它是线上运行记录，没有进 Git。文中“独立副本、不改主代码”是合并前的约束。

# 陕西直接采集部署

固定采集release：`/home/mz/sd_crawler/pmos_shaanxi_lab/releases/20261103_direct_v25`。
原报送包装入口保持 `20261103_direct_v22`，本次通信修复没有改邮件配置。
实例：`/home/mz/sd_crawler/pmos_shaanxi_lab/instances/production_20261102`。
唯一用户服务：`pmos-shaanxi-native.service`。原worker必须退出；原Chrome、observer、keeper和网络不随采集升级重启。

通过 `ssh mz` 连接后：

```sh
systemctl --user status pmos-shaanxi-native.service
journalctl --user -u pmos-shaanxi-native.service -n 30 --no-pager
```

`handoff.json` 固定代码和进程启动身份；`native_release_upgrades` 保留升级链。在线任务为SQLite `sn_jobs`；`sn_commands`、`sn_turns`、`sn_requests` 区分任务、原生阶段与HTTP。归档先于任务/分页确认，崩溃观察原回执，不重新派发。

## 升级

固化新独立release并完成相应检查，优雅停止当前PMOS后，在新release调用：

```sh
python3 -B tools/upgrade_shaanxi_direct.py \
  --config /home/mz/sd_crawler/pmos_shaanxi_lab/instances/production_20261102/handoff.json \
  --previous-release /home/mz/sd_crawler/pmos_shaanxi_lab/releases/20261103_direct_v25 \
  --approval '实际用户授权和本次具体变更'
```

工具检查原锁、无未结attempt和升级链，记录SHA、换固定服务路径后启动。不能用兼容队列流程重置账本。`--review-zero-send` 仅供专门复核：必须全库没有任何直接command/issued记录，才可释放纯只读阶段的未发送任务；不能用于真实失败或未知请求。

20:29的v24通信修复使用已留档的一次性collector-only升级步骤，报送drop-in SHA保持，通用升级工具仍会更新报送入口；后续若任务仅涉及采集器，应继续保留独立的具体范围审核。

原报送是唯一发送器。drop-in为 `~/.config/systemd/user/shaanxi-collection-report-send.service.d/40-pmos-owner.conf`，仅改独立包装入口；不复制或更改私密配置、签名、SMTP、收件方或发送状态。

## 显式回滚

在当前固定release调用：

```sh
python3 -B tools/shaanxi_owner.py rollback \
  --config /home/mz/sd_crawler/pmos_shaanxi_lab/instances/production_20261102/handoff.json
```

工具先停止PMOS、取原锁，检查原保护、人工控制和冻结journal无漂移，将SQLite当前任务/分页/保护与原件引用反向导出，再恢复原worker。附件消费跨类型保留；不会用初始快照覆盖新事实。未知目标、未结命令或控制冲突时保留证据并停在检查处，不另起worker绕过。

原worker恢复后，将报送drop-in恢复为实例 `direct_migration/previous_report_override.json` 记录的原文本（`20261102_report_v5`），再 `systemctl --user daemon-reload`。此步骤让原报送统计已反向导出的任务，避免继续读取冻结SQLite。检查下一次自然报送，不重发旧邮件。

源码变更仅独立副本。未改Linger、原保活和开机启动政策。
