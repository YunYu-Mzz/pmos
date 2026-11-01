# 五省统一状态邮件

2026-11-01。用户要求："统一一下所有省份邮件格式，都统一披露相同的信息和内容……按照安徽的模板来吧"。

## 统一的内容

协议仍是 `[CRAWLER-STATUS-V1]`：签名 JSON 附件，由唯一汇总端（山西 `report_bridge.py receive`）写入飞书。协议、签名、汇总端都不变。变的是五省各自发出的内容，现在全部来自同一个文件 `pmos/pmos/status_mail.py`。各省 `reporting/` 目录里放一份逐字节相同的 `status_mail.py`，`tests/test_status_mail.py` 会比对仓库里每一份副本。

每封邮件：

1. **只发 20 个标准字段**（`FIELDS`），不再发山西、冀北的 10 个附加字段。`ordinary_*` 计全省全部原件：旧系统加 PMOS，按路径（陕西、冀北按原件 SHA）去重。
2. **`collector_state` 只有三种值**：`active`（在采）、`inactive`（未在采）、`None`（未知）。汇总端的运行行只认 `active`，原来的 `active(PMOS)` 在飞书上显示成"待核验"。
3. **`counts_note` 是同一句话**，各部分顺序固定，省不了的值写"未知"：

   > 2026-11-01 12:31起安徽由PMOS采集（正常）。原件合计14815份（旧系统冻结12345份、PMOS2470份；近1小时2439份）；近1小时请求2608次；任务完成2470、空163、待采6060、等待发布66；暂停栏目0个；阻断：无；停滞0分钟。按文件路径去重；近1小时按文件mtime估算。逐指标表仍为旧系统冻结统计。

   - 由 PMOS 采集的省：用 `apply_pmos()`，数据来自 `pmos <省> status --mail`。"起"的时间是交接回执的 `granted_at`，按北京时间，即 `status --mail` 新增的 `since` 字段。没有该字段的旧版本就不写时间。
   - 还由旧采集器采集的省：用 `legacy_note()`，标题由指标推出（正常、已暂停、已停止、状态未知）。
   - PMOS 状态读不到时：用 `pmos_unreadable()`，前缀"PMOS状态读取失败。"，采集器字段全部置空，不沿用旧值。
4. **邮件正文是安徽的四行**（`render()`）：标题、核验时间、原件份数与字节、说明句、固定结尾。

## 各省接入

| 省 | 采集方 | 改动 |
|---|---|---|
| 安徽 | PMOS | `snapshot.py` 的 `merge_pmos` 换成 `apply_pmos`/`pmos_unreadable`；`protocol.render` 改为引用 `status_mail.render` |
| 山西 | PMOS | `report_bridge.py snapshot()`：三类原件合计写入 `ordinary_*`，只发 20 字段，PMOS 用 `apply_pmos`；发信正文改为 `status_mail.render`。汇总端的 `render`（旧纯文本飞书块）不动 |
| 辽宁 | 切换中 | `snapshot.py` 新增 PMOS 桥接：`PMOS_DATA/liaoning/ledger.sqlite3` 存在（交接已写入）后读最新 `/home/ecs-user/pmos-liaoning-*-r*` 的 `status --mail`，之前用 `legacy_note`；`sender.py` 让共享协议副本用统一正文 |
| 陕西 | 旧系统 | `legacy_note(dedupe='原件SHA')`，任务数取自同一次读到的 journal；`protocol.render` 改为统一正文 |
| 冀北 | 旧系统 | 原件合计从附加字段移入 `ordinary_*`，只发 20 字段，`legacy_note(dedupe='原件SHA')`；`protocol.render` 改为统一正文。由用户部署 |

各省 `reporting/` 目录不在任何 git 仓库里，部署时以 SHA256 核对。
