# 架构说明

## 为什么能统一

三省都是同一套国网电力交易平台（`pmos.ah/ln/sx.sgcc.com.cn`）：相同的 Vue 前端、网关请求头、业务状态码、"会话已注销"提示和会话超时规律。旧代码之所以变成三份，是因为每省各自摸索、为单次事故写了大量一次性脚本（山西约 270 个、辽宁约 160 个、安徽约 70 个部署/维护脚本）。新代码把平台共性收进 `pmos/`，把省份差异全部变成数据（`provinces/<省>/`）。

## 分层

```
cli.py ── engine.py ──┬── ledger.py   (SQLite：任务 + 每次请求的尝试记录)
                      ├── pacing.py   (响应后间隔、可选上限)
                      ├── faults.py   (失败 → 停哪一层、能否保活)
                      ├── validate.py (响应 → 原件 / 空 / 失败)
                      ├── archive.py  (内容寻址原件 + manifest)
                      ├── spec.py / province.py (声明式配置)
                      └── transport ──┬── browser/attach.py   (CDP 附加，绝不关闭浏览器)
                                      ├── browser/transport.py (fetch / component / native / ui)
                                      ├── browser/ui.py       (Element UI 填表、开帧)
                                      └── browser/session.py  (注销提示、请求捕获)
```

引擎不直接依赖浏览器：`transport.send(request) -> Response` 和 `check_session() -> Fault | None` 都由外部注入。测试用假对象，生产用浏览器实现。

## 一次请求的生命周期

1. 检查是否存在未结尝试；有则拒绝发送。
2. 计算间隔：距最近一次任意响应结束不足 N 秒则不发；采集和样本另受小时/日上限约束。
3. 写入 `attempts(kind, dataset, period, started_at, outcome='pending')` 并提交。
4. 通过传输方式发送。
5. 判定响应：HTTP 状态 → JSON 业务码（导出接口拒绝时会返回 JSON）→ 文件容器校验。
6. 先归档原件（fsync），再写 manifest，最后确认任务；尝试记录更新为 `success`、`empty`、`failed` 或 `unknown`。

进程在第 3 到第 6 步之间中断时，尝试会一直停在 `pending`。下次启动会阻断，直到人工核实后执行 `settle-unknown`。这样保证已经可能被服务器处理过的请求不会被悄悄重发。

## 工作循环（`Engine.tick`）

每个 tick 只做一件事，优先级从上到下：

1. 页面出现注销/网络环境变化提示 → 阻断，不再保活。
2. 已有阻断 → 进入挂起：失败类别允许时继续续期保活；保活第一次失败就停止，不重试。
3. 存在未结尝试 → 阻断。
4. 有待执行的适配命令（样本）→ 执行一个。
5. 采集关闭、暂停、全部完成或在等待间隔 → 执行会话保持（需要时续期，或做时间探测）。
6. 否则采集下一条待办任务（按 `collection_order` 选报表，同一报表最新周期优先）。

公告栏目（`documents`）是两级任务：每天的列表任务逐页翻（每页一个尝试，游标持久化），把新附件写进台账 `items` 表并生成附件任务；附件任务逐个下载。两类任务都走同一套间隔、台账和故障分类：文件校验不通过或记录不符合 `expect` 只隔离该栏目。

## 会话保持的依据

旧系统的日志给出了同一结论：

- **山西**：10-13 最后一次导出在 10:26:05，时间接口 10:51 成功、10:56 失败；10-20 两次掉线分别发生在最后一次业务请求后 30 分钟和 32 分钟。另一方面，只靠导出撑过了 24.7 小时，最长一次登录持续约 4 天 20 小时。
- **辽宁**：空闲 33 分钟时会话仍在，42 分钟时已失效。连续采集期间从未掉线。
- **网站自带续期无效**：前端每 60 秒调用一次 `refreshExpireTime`，但传入的 token 是一个还没取到值的 Promise，服务器因此返回 4000/40302。时间接口轮询也不算活动。

因此 `renew_after_idle_seconds`（默认 600）到点后，引擎会用 `session.keepalive_dataset` 重新读取一个已经采过的周期。这次请求只做校验、不归档，不计入采集上限，但计入共享间隔。

## 失败分类（`faults.py`）

| 类别 | 例子 | 处理 |
|---|---|---|
| auth | 401、10003、"会话已注销" | 停整个省份，不保活 |
| permission | 40302、403 | 适配样本：报表标为 `denied`；正式采集：停整个省份 |
| rate_limit | 429、"访问过于频繁" | 停整个省份，不保活 |
| network | 超时、连接断开 | 停整个省份，不保活；尝试记为 `unknown` |
| unknown | 未知业务码、返回 HTML 页面、文件类型变化 | 停整个省份，不保活 |
| site_data | HTTP 200 且业务成功，但文件损坏 | 只隔离该报表，其余继续 |
| local | 模板缺失、报表帧未打开、我们自己的问题 | 停整个省份，允许续期保活 |

"允许保活"只对已确认是本地原因的阻断开放。可能掩盖 401/403 的笼统错误不会进入允许名单，这是旧系统复查时反复暴露的问题。

## 运行与部署

- **浏览器服务**：`deploy/pmos-browser@.service`。有界面的 Chrome，持久配置目录，`--remote-debugging-address=127.0.0.1`。重启采集服务不会影响它。
- **采集服务**：`deploy/pmos@.service`，执行 `python -m pmos %i run`，`Restart=no`。出错后挂起等待人工处理，不会自己反复重启。
- **一省一进程**：每省一个独立运行目录（`PMOS_DATA/<省>`）和进程锁，三省可以同机运行而互不影响。
- **系统升级**：服务器上的 needrestart 已配置为不自动重启 `liaoning-*`/`shanxi-*` 服务。新服务名为 `pmos-*`，部署时同样需要加入排除列表。

## 与旧系统的对应

| 旧系统 | 新代码 |
|---|---|
| 山西 `browser_transport.ObservedTransport` | `browser/transport.py` 的 `fetch` 传输 |
| 辽宁 `browser_collector.js`（组件 + watchXHR） | `component` 传输（捕获网络响应，不再修改 XHR 原型） |
| 安徽 `adapters/daily.py` 填表导出 | `ui` 传输 + `browser/ui.py` |
| 三套 Crawlee 队列 + journal | `ledger.py`（SQLite，一个文件） |
| 三套 `server_*.json` 状态与五种限速来源 | `state.py` + `pacing.py` |
| 山西 `session_hold` / 辽宁 `hold_keepalive` | `engine._hold` + `faults.decide` |
| 各省 `dataset_catalog.json` 的运行时标志 | 规格文件 `status`（配置），运行时隔离写在 `state/` |

旧系统里按进程号、事故时间编写的恢复脚本没有迁移，相应的情况统一由 `ack`、`settle-unknown` 和规格生命周期处理。
