# PMOS：三省离线适配与旧队列兼容

2026-10-28第四轮离线身份与只读补证。继续使用现有PMOS共同引擎；陕西、冀北未迁移。**网站请求为0，线上入口仍禁用，现有采集、保活、浏览器和旧队列未改。**

本轮结果及具体剩余项见[第四轮汇总](docs/PHASE_FOUR.md)：辽宁42条Monday查询锚点全部可表达，原1,552条未映射累计补回1,541条，余11条；51个缺证技术来源另有17条同请求别名链，新增独立原件0。安徽原余20条再补10条，余10条，3份binding及52候选仍缺证。山西19项仍缺业务原件，另补强3条元数据链。旧消费事实和保护项均保持。

[第三轮](docs/PHASE_THREE.md)的43个辽宁原件链、安徽40/43份精确binding副本、三省历史快照及分层资格保留；所有线上验收仍pending。

## 第二阶段基线（保留历史口径）

| 省份 | 本轮离线覆盖 | 仍保留的边界 |
|---|---|---|
| 辽宁 | 普通/嵌套记录、宽表、动态表、分页、FineReport 96点HTML、附件容器；120份留存回放 | 170行原目录中78有样本、92待证；技术来源联合185个中91有样本、94缺证/待审。两种口径不可混为指标总数 |
| 安徽 | 148任务组逐项具备读取路径：81原生模型、60列表、4XLSX、1正文、1PDF容器、1派生参考；全量重建约1.37万captured原件 | 后7组不冒充原生语义验收；43份早期可变binding保留资格限制；另外52候选逐项缺证；列表不等于附件 |
| 山西 | 270技术来源中221有形态样本、49缺证/阻断；820次留存评估816通过 | 使用已有可选xlrd/olefile的离线环境；标准库为211/59。4历史40302仍拒绝，源单元格/附件容器不等于业务字段语义通过 |

三省统一状态均为**离线形态/样本通过，线上pending**，不是全站全年全部范围完成。原435份datasets规格继续作为历史来源目录，enabled=0，不继承旧环境上线资格。旧山西PMOS缺模板实测失败记录也未清除或重跑。

旧队列已实现6种格式兼容、day/month/year journal、精确身份映射、未知/拒绝/失败保留、附件fileId防重放及新Ledger影子索引。第二阶段只读规范化辽宁13,032条和安徽114,599条记录视图，当时未映射分别1,552/1,696条；第三轮在同一旧cohort补证。**这是非派发离线对照，不是生产迁移完成。** 山西历史SQLite及journal现已从verification等路径找到；完整Crawlee storage导出仍未证明，详见第三轮汇总。

## 运行

需要Python3.11+，源码checkout离线核心只用标准库，不启动浏览器、不连接网站。macOS系统Python若为3.9，可使用本机已安装的 `/Users/yunyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`（本轮3.12）。

```sh
cd /Users/yunyu/Desktop/sd_crawler/pmos
python3 tools/test_offline.py
python3 -m pmos offline-demo all
python3 -m pmos offline-audit liaoning
python3 -m pmos offline-audit anhui
python3 -m pmos offline-audit shanxi
python3 -m pmos legacy-audit liaoning
python3 tools/recover_existing_evidence.py all --stage remaining
```

`offline-audit`会对指定省份全部本地留存材料只读重建，默认只输出摘要。可加 `--report var/new-review.json` 保存新报告，仅允许写本项目var。当前仍在运行的旧队列/尝试日志可能变化，报告明确记录滚动视图及读取期间漂移，不冻结旧进程。

原件精确回放：

```sh
python3 -m pmos replay liaoning /controlled/response.json --sha256 EXPECTED_SHA256
python3 -m pmos replay anhui /controlled/source.json --sha256 EXPECTED_SHA256 \
  --contract /controlled/reviewed-contract.json --contract-sha256 CONTRACT_SHA256
```

不带contract使用第一阶段固定形态；带contract使用本轮省份decoder。真实binding/model与selector只在RAM，不复制进Git。单位仅接受与本次原文SHA绑定的明确响应单位字段；布尔值、路径或格式正确的hash本身不构成单位证据。

XLS/旧DOC可选离线依赖见 `requirements-offline-workbooks.txt`（xlrd2.0.2、olefile0.47）。本任务没有安装依赖，而在单独审计子进程复用本机已有纯Python库，证据含版本、来源和SHA。缺库会明确阻断，不把容器识别当全值读取。

## 组织与审阅入口

`pmos/engine.py`、`ledger.py`、`contracts.py`共用；省份decoder放 `pmos/adapters/{liaoning,anhui,shanxi}.py`；工作簿通用代码只有 `workbooks.py` 一份；旧队列兼容在 `queue_compat.py`，本轮只读补链在 `pmos/recovery/`，不会导入旧worker。

- [第三轮只读补证、历史快照与剩余项](docs/PHASE_THREE.md)
- [第四轮Monday身份与剩余证据](docs/PHASE_FOUR.md)
- [第二阶段汇总与缺项](docs/PHASE_TWO.md)
- [辽宁逐项范围](docs/LIAONING_OFFLINE.md) · [安徽逐项范围](docs/ANHUI_OFFLINE.md) · [山西逐项范围](docs/SHANXI_OFFLINE.md)
- [旧队列格式、身份、防重放与命令](docs/QUEUE_COMPATIBILITY.md)
- [五省架构和回滚](docs/ARCHITECTURE.md) · [适配规范](docs/ADAPTING.md)
- [Git/开发测试](docs/DEVELOPMENT.md) · [线上预算及验收门槛](docs/ACCEPTANCE.md)
- [第四轮验证摘要](evidence/phase_four_validation.json) · [第三轮验证](evidence/phase_three_validation.json) · [第二阶段验证](evidence/phase_two_validation.json)

所有run/capture/启用/控制/解除保护CLI仍被发行版拒绝；service模板失败门禁保持。没有创建远端、发邮件、写飞书或启用任何定时任务。
