# PMOS 统一采集代码：第一阶段离线交付

2026-10-27。沿用本目录既有框架，修复共用引擎并增加三省离线适配；没有建立第二套框架。**本版本只支持离线验收，不可替代生产采集器。** 山西曾在缺模板时实测失败；本次没有重新上线，也没有清除该次阻断。

| 省份 | 本次离线实现 | 留存原件回放 | 线上资格 |
|---|---|---|---|
| 辽宁 | 96 点列表曲线、嵌套业务码、原件/任务流程 | 系统负荷 3 份通过 | 仅所选样本通过，待线上验收 |
| 安徽 | 96 点对象曲线、零/负数和完整时点校验 | 省级负荷预测 3 份通过 | 仅所选样本通过，待线上验收 |
| 山西 | 原生分页表、页码/行数/总数检查 | `menu_af7397f1e57d` 3 份通过 | 仅所选样本通过，待线上验收 |
| 陕西、冀北 | 预留适配接口 | 未迁移、未读取其生产目录 | 不在第一阶段范围 |

**“离线通过”是共同引擎和明确样本形态通过，不是三省全站适配完成。** 原有 435 份规格保留作历史迁移目录，其 candidate/sampled/verified 代表旧环境资格，不能当统一运行时验收。当前 enabled=0；已撤销三个默认启用项，移除山西负荷缺源码佐证的推断 bootstrap。所有线上/控制 CLI 动作均被本发行版硬性拒绝，无环境变量绕过；部署模板也有失败前置门禁。

需要 Python 3.11+。离线核心只用标准库，不需要安装 Playwright，也不会打开浏览器。

```sh
cd /Users/yunyu/Desktop/sd_crawler/pmos
python3 tools/test_offline.py
python3 -m pmos offline-demo all
python3 -m pmos provinces
python3 -m pmos shanxi check
```

若系统 `python3` 是 macOS 3.9，请改用本机已安装的 3.11+。本次验证使用 `/Users/yunyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`（3.12）。网络禁用测试入口同时禁用可选本地浏览器测试；运行时只写临时目录，结束即清理。

回放某个明确授权的本地原件，须提供已登记 SHA256：

```sh
python3 -m pmos replay liaoning /controlled/original/response.json --sha256 EXPECTED_SHA256
```

只输出哈希、行数、分页和资格摘要，不输出业务行或认证；不修改输入文件，不连接网站。契约只接受本阶段选定形态，其他来源应补契约，不能套用参数或猜字段。

交付入口：

- [差异、证据和保留/修复/替换决定](docs/REVIEW.md)
- [五省统一架构与迁移回滚方案](docs/ARCHITECTURE.md)
- [适配与扩展流程](docs/ADAPTING.md)
- [下一阶段上线预算与验收清单](docs/ACCEPTANCE.md)
- [Git、配置、开发和测试](docs/DEVELOPMENT.md)
- [可核验的只读样本摘要](evidence/phase_one_review.json)
- [测试及交付核验摘要](evidence/validation.json)

没有复制原始生产数据、profile、真实凭据或私密配置。旧三省目录、状态、队列、原件、运行服务和现有邮件/飞书链路均未由本任务修改。
