# PMOS：三省离线适配与旧队列兼容

2026-10-28第五轮精确身份桥与只读补证。继续使用现有PMOS共同引擎；陕西、冀北未迁移。**网站请求为0，线上入口仍禁用，现有采集、保活、浏览器和旧队列未改。**

本轮结果见[第五轮汇总](docs/PHASE_FIVE.md)：辽宁原余11条、安徽原余10条精确身份桥全部补齐，这两个固定cohort未映射均为0。辽宁用旧身份摘要在已证明的有限域中唯一复原；安徽用同菜单原生listing多链补证。旧状态、消费事实和保护项保持，身份补齐不表示原件或语义已完成。

原件/资格缺证继续保留：辽宁51个来源自身原件仍缺，其中34个无own/明确alias链；安徽3份精确binding副本和52候选仍缺证；山西19项业务原件仍缺，拒绝与unknown不解除。[第四轮](docs/PHASE_FOUR.md)的42条Monday及原有身份映射保持。

[第三轮](docs/PHASE_THREE.md)的43个辽宁原件链、安徽40/43份精确binding副本、三省历史快照及分层资格保留；所有线上验收仍pending。

## 山西迁移（2026-10-31，优先）

按用户要求先切山西，标准是不低于服务器现在的效果。设计与步骤见[山西迁移](docs/SHANXI_MIGRATION.md)。依据服务器只读快照对齐了生产行为（分通道5/15秒、单项隔离、发送前预检、页面导出取Blob、空闲300秒导出固定已证明日续期、只处理已有队列），并对服务器当前原生队列做了逐条身份核对：30个数据集17,677行全部一致。切换计划覆盖原生待办12,633/13,205（其余为用户暂缓）。本轮网站请求为0。

## 陕西（2026-11-04 合并）

陕西的代码从 `experiments/pmos_shaanxi_20261102/` 合并进来，和合并时 `mz` 上运行的 `20261103_direct_v25` 逐文件一致。陕西和另外三省的接法不同：不走 `pmos shaanxi ...` 这些通用命令，也没有 dataset 规格。入口是 `tools/shaanxi_owner.py`，任务在 ledger 的 `sn_*` 表里，来源清单是 `provinces/shaanxi/inventory.json`。

线上版本靠代码指纹固定，所以这次合并不改变线上。以后从主线部署，要走 `tools/upgrade_shaanxi_direct.py`。运行环境是 `mz` 的系统 Python 3.10.12，PDF 解析依赖见 `requirements-shaanxi.txt`。主线声明要 3.11+，在 3.10 上只有 `pmos.legacy` 和 `pmos.recovery.catalog` 用不了（`hashlib.file_digest`），陕西的代码路径用不到它们。

说明见[直接采集](docs/SHAANXI_DIRECT.md)、[接入记录](docs/SHAANXI_INTEGRATION.md)、[运行笔记](docs/SHAANXI_NOTES.md)和[部署](deploy/SHAANXI_DEPLOY.md)。线上运行记录留在 `experiments/pmos_shaanxi_20261102/evidence/`，不进 Git。

## 辽宁迁移（2026-10-31）

首个迁移目标是辽宁，设计和切换步骤见[辽宁迁移](docs/LIAONING_MIGRATION.md)。从旧生产 manifest 生成40个报表规格，并离线证明引擎身份与旧队列身份逐条相等（本地10/23快照11,278条中11,267条相同）；切换计划可接管32个来源。交接回执、旧墓碑跳过、请求预算和 stop_all 已实现并测试。真实切换的每一步仍需用户批准，本轮网站请求为0。

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
python3 tools/recover_existing_evidence.py all --stage bridges
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
- [第五轮辽宁/安徽精确身份桥与三省缺证](docs/PHASE_FIVE.md)
- [第二阶段汇总与缺项](docs/PHASE_TWO.md)
- [辽宁逐项范围](docs/LIAONING_OFFLINE.md) · [安徽逐项范围](docs/ANHUI_OFFLINE.md) · [山西逐项范围](docs/SHANXI_OFFLINE.md)
- [旧队列格式、身份、防重放与命令](docs/QUEUE_COMPATIBILITY.md)
- [五省架构和回滚](docs/ARCHITECTURE.md) · [适配规范](docs/ADAPTING.md)
- [Git/开发测试](docs/DEVELOPMENT.md) · [线上预算及验收门槛](docs/ACCEPTANCE.md)
- [第五轮验证摘要](evidence/phase_five_validation.json) · [第四轮验证](evidence/phase_four_validation.json) · [第三轮验证](evidence/phase_three_validation.json)

所有run/capture/启用/控制/解除保护CLI仍被发行版拒绝；service模板失败门禁保持。没有创建远端、发邮件、写飞书或启用任何定时任务。
