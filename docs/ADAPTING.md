# 适配规范：新省份与新报表

目标：每个报表都经过同一套流程，状态和证据写进规格文件，任何人都能看出它为什么可以自动采集。

## 报表生命周期

```
candidate ──样本成功──▶ sampled ──另一周期也成功──▶ verified ──人工启用──▶ enabled
    │                                                                    │
    └─ 样本 40302/403 ─▶ denied（永不自动重试）      文件校验失败 ─▶ 运行时隔离该报表
retired：保留历史，不再调度
```

- 状态写在 `provinces/<省>/datasets/<报表>.json` 的 `status` 字段；每次变化都在 `history` 里追加一条带时间的记录（样本周期、原件 SHA256、失败原因）。
- `enabled` 只能由 `verified` 经人工命令进入。跳过验证必须写明原因（`--force-reason`），原因会记录在规格里。
- 运行时的临时隔离（如连续两个空周期、文件损坏）记在运行目录的 `state/dataset_holds.json`，不修改仓库里的规格。

## 一、新增省份

```bash
python -m pmos new-province hebei --name 河北 --origin https://pmos.he.sgcc.com.cn
```

生成 `provinces/hebei/province.json`，然后按需修改：

| 字段 | 说明 |
|---|---|
| `browser.profile_dir` 或 `browser.cdp_url` | 怎样找到已登录浏览器的调试端口；本机差异写在运行目录的 `browser.json` |
| `browser.page_url_contains` | 站点有多个标签页时，用来选定唯一工作页 |
| `policy.response_interval_seconds` | 响应结束后的间隔，至少 30 秒 |
| `policy.max_per_hour` / `max_per_day` | 可选上限；`null` 表示不限 |
| `policy.renew_after_idle_seconds` | 业务空闲多久后续期，默认 600 秒；该平台约 30 分钟注销，不要超过 1500 秒 |
| `session.keepalive_dataset` | 用于续期的报表；必须是 `enabled` 且标记 `keepalive: true` 的按日报表 |
| `session.probe` | 可选的时间探测接口，例如 `{"path": "/px-trade-auction/auctionFeignAll/queryCurrentTime"}`；它只用于尽早发现掉线，不算业务活动 |
| `session.collection_order` | 采集顺序，未列出的报表按 id 排在后面 |

然后部署浏览器服务（`deploy/pmos-browser@.service`），由人在浏览器里登录（密码 + 短信验证码，采集器不会代填）。

## 二、新增报表（标准五步）

### 1. 捕获请求

```bash
python -m pmos hebei capture --minutes 10
```

这期间由人在浏览器里打开目标报表，选好日期，点一次“查询”和“导出”。记录写在运行目录的 `captures/*.jsonl`，内容是每个接口调用的路径、方法、请求体（敏感键已打码）、HTTP 状态、业务码、返回类型和大小。**请求头、Cookie、Token 一律不保存。**

### 2. 生成候选规格

```bash
python -m pmos hebei candidate 12 --id system_load_forecast --name 系统负荷预测（日） \
    --period day --start 2026-01-01 --date-field date
```

- 第 12 条捕获记录会变成 `datasets/system_load_forecast.json`，状态为 `candidate`。
- 请求体里与周期相符的值会被替换成占位符（`{date}`、`{month}` 等）。用 `--date-field` 限定只替换哪些字段，避免误替换。
- 按站点实际情况选择传输方式（`--transport`）：

| 情况 | 传输方式 |
|---|---|
| 导出/查询接口可直接重放（山西的绝大多数导出） | `fetch`（默认） |
| 接口每次需要前端现算的令牌或签名 | `component`：调用报表的 Vue 组件方法 |
| 只能通过页面操作得到文件 | `ui`：填表、点查询/导出 |

  后两种需要手工补全 `frame`、`open_url` 或 `menu`、`component`/`call`/`set` 或 `fields`/`action`，格式见 [DATASET_SPEC.md](DATASET_SPEC.md)。

### 3. 校验规格

```bash
python -m pmos hebei check
```

### 4. 取两个不同周期的样本

```bash
python -m pmos hebei sample system_load_forecast 2026-10-11
python -m pmos hebei sample system_load_forecast 2026-10-12
```

- 样本由正在运行的工作进程执行。它与正式采集共用限速、台账和浏览器连接，不会额外并发。
- 第一个成功样本把状态改为 `sampled`；另一个周期也成功后改为 `verified`。同一周期重复成功不算数。
- 样本原件保存在运行目录 `samples/`，与正式原件分开。
- 被拒（40302/403）时状态改为 `denied`，不会再自动尝试，也不会停掉该省其他报表。
- 认证、限流、网络或未知异常仍会停住整个省份，等人工处理。

### 公告栏目

公告/附件栏目按 [DATASET_SPEC.md](DATASET_SPEC.md#公告与附件documents) 写 `documents` 规格，验证分两步：

```bash
python -m pmos hebei sample pub_xxx 2026-10-23              # 第一页列表 → sampled
python -m pmos hebei sample pub_xxx 2026-10-23 --scope file # 下载一个附件 → verified
```

### 5. 启用

```bash
python -m pmos hebei enable system_load_forecast
```

工作进程会在下一轮自动生成该报表的历史周期任务（从 `period.start` 到今天，最新的在前），已有原件的周期不会重复采集。

## 三、只在必要时写代码

规格无法表达时，按以下顺序处理：

1. 先确认能否用 `component`、`native` 或 `ui` 方式让网站前端自己完成；需要先把页面带到某个状态时，用 `prepare` 步骤（设字段、发事件、调方法、等待）。翻页用 `pagination`，公告附件用 `documents`。
2. 页面胶水实在无法声明时，写一个省份页面脚本 `provinces/<省>/scripts/<名称>.js`（一个异步函数），在 `prepare` 里按名称引用。脚本只做页面内的准备，不直接发请求、不保存数据。
3. 仍然不行，再在 `pmos/browser/` 增加一种通用传输方式，或在 `validate.py` 增加通用校验，并补离线测试。
3. 不要写只为某次事故、某个进程号或某个日期服务的一次性脚本。旧系统里大量这类脚本正是维护困难的原因。

## 四、上线与日常运维

- 新省份或新传输方式第一次上线时，先只启用保活报表，实测会话能稳定维持后，再逐个启用其他报表。
- 掉线后，人在浏览器重新登录，然后执行：

  ```bash
  python -m pmos <省> status
  python -m pmos <省> ack auth_expired
  ```

  `ack` 必须写出被解除的阻断原因，原因不符时拒绝执行。
- 出现未结请求（进程在请求途中被中断）时，先到网站核实，再执行：

  ```bash
  python -m pmos <省> settle-unknown <attempt>
  ```

- 不要在浏览器里点网站弹出的“会话已注销”对话框的“确定”。它会调用服务器注销，把所有标签页一起登出。
