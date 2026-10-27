# 辽宁第二阶段离线适配

本阶段仅本地只读。未导入旧爬虫模块，未读取凭据/profile，未连接CDP或网站，未改旧源码、队列、原件、采集或保活。`pmos.adapters.liaoning` 是可注入同一Engine的纯decoder及只读库存工具；没有另一套worker。

## 接口

```python
from pmos.adapters.liaoning import decode, contract_for_sample, build_inventory

# raw必须来自明确的留存原件，调用者先核对登记SHA。
contract = contract_for_sample(raw)
result = decode(raw, contract, http_status=200)
# 返回ParsedArtifact；rows是源字段，缺单位仍为None。
report = build_inventory('/controlled/liaoning')
```

`contract_for_sample`只形成响应形态契约，绝不授予业务语义、单位、权限、历史全范围或线上资格。独立的请求页码/页大小证据可显式传入；没有它时仅核对响应分页结构，`request_page_evidence_used=false`。原生分页元数据全0时必须提供真实请求页码/大小的额外证据，不能从行数反推请求参数。根统一CLI负责选择decoder和保存摘要。

现有支持：

- 普通报告、说明性记录、多条曲线、字典之外的业务记录：明确`rows_path`下的原始字典列表，保留重复行、0、负数、null及原字段。没有依据时不把列表投影叫语义适配通过。
- `data.list`和`data.handleList`，以及嵌套`data.data.list`；每层HTTP200业务错误先分类，外层0不能掩盖内层401/40302/500。
- 公告、机构、成员及其他原生分页：保留实际字段和页码/总数；不用错误`pages=0`提前结束，拒绝短非末页和重复页。列表通过不等于正文或附件下载通过。
- `data.objectList.list`动态表：字段集合和原生列配置SHA绑定；列配置变化拒绝。null列表只在明确契约且total=0时解释为空，不能把未知结构作空。
- 已缓存组件源中显式时刻/字段配对可以绑定宽表时间字段；校验字段存在及有限数字，null/空串原样保留，不填0。本次6份样本附有源代码SHA绑定，其他样本不扩张时间/单位资格。
- FineReport原生`htmlTag/currentPage/totalPage`：严格1页、12×16网格、00:15—24:00共96点，保留源行列坐标和字符串值；原生双空行只记真实空表，不补96个0；合并单元格或变形网格拒绝。
- 附件容器：调用既有PDF/Excel等容器校验，只证明文件结构，不宣称扫描PDF/复杂工作簿内容已经完整结构化。

本轮14项新增离线测试覆盖上述形态、原生空表、0/负值/null/重复、字段和列配置漂移、分页、嵌套业务错误、数值布尔/非有限值、凭据字段拒绝及只读库存。动态表和附件有脱敏契约测试，但没有借此声称取得当前目录中相应来源的真实样本。

## 逐来源证据与数量

完整登记见 `../evidence/liaoning_phase2.json`，逐项包含原目录状态、manifest/portable来源关系、契约、样本原件路径/SHA、解析摘要SHA和未通过原因，没有复制业务行或原始数据。

| 口径 | 数量及本次结果 |
|---|---|
| 留存sitewide目录 | 170行，其中78行具有本地SHA绑定样本并通过相应结构/网格检查，92行仍缺本地样本或需审查 |
| 留存已接入manifest | 121技术来源：44 report、63 list、3 member、11 additional；不是121项本轮验收 |
| portable adapters | 79定义；旧validated不直接继承为统一引擎线上资格 |
| 上述目录与实际留存源合并 | 185个技术ID：91有样本通过、94缺证或待结构检查；不是185个独立业务指标 |
| 本轮实际样本回放 | 120份，全部按既有回执或原件目录SHA核对，回放后复核源SHA不变 |

这三个目录口径不同，且包含同一请求的菜单别名、既有日报工作流、历史排除项及新辅助来源，不能相加后宣称指标总数。170行中已有4个显式`canonical_dataset`映射沿用原目录；没有映射的同名/同形态来源不自动合并。原生report number只在真实endpoint和目录唯一绑定同时成立时关联留存样本，不能按标题拼来源。

## 真实缺口

- 43个技术ID的已登记远端proof路径在本机缺失；本任务没有通过SSH/网站补取。它们的旧证据仍登记，不生成假样本。
- 53个技术ID没有可用的本地SHA绑定业务原件；这些包含别名、待审、历史排除等范围，并非53个新网站故障。
- 一份留存分页响应缺独立请求页码/大小或可靠响应元数据，保留需审查，不猜页码。上述原因与技术ID可能交叉，不能直接相加作缺口总数。
- 三个FineReport来源能按既有原件回放，不代表所有原生engine阶段或当前挂起请求已恢复。
- 每个条目`semantics=not_accepted_by_structural_projection`、`online_status=pending`、完整覆盖false；需要字段/单位/范围证据才能晋级，现有采集和保活没有被切换。

旧队列兼容由共用层处理。辽宁至少有日报Crawlee/JSON、advanced/sitewide的tasks/outcomes/active、verified-source的pending/outcomes/hold、dimension frontier以及FineReport active.stage等不同状态。原sent/unknown/denied/held身份必须先导入不可调度事实，不能仅凭manifest或本轮样本通过就重新排队。
