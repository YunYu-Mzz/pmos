# 安徽第二阶段离线适配

范围：只读现存安徽公开计划、原生尝试日志、绑定及本地原件；不导入旧生产模块、不连接浏览器/网站、不写队列或状态。原生计划145个菜单入口拆成148任务组，另52个未准入候选逐项保留。148组不等于148个指标已完成全年、全部主体或全部语义验收。

## 实现

`pmos.adapters.anhui` 提供以下纯离线接口，所有源码位于统一项目中：

- `decode(raw, contract, http_status=200) -> ParsedArtifact`：原生响应、旧XLSX、PDF容器、公告正文源和历史派生参考，明确区分资格。
- `project_bound(payload, model, period=...)`：只重建已经有证据的绑定模型；不从重复零值推测新列映射，不生成接口或请求参数。
- `build_inventory(legacy_root)`：对148组及52候选输出逐项资格清单，核对全部留存captured响应、派生文件和绑定；只读文件，无输出文件副作用。

原生契约来自经SHA核对的不可变binding，或显式标记的早期可变state binding：`{model, success_code, period}`。模型与实际selector只在本地内存使用，不将真实binding/model/业务数据复制到Git。证据中只保留相对受控路径、SHA、模型类别、统计数量和scope摘要。

支持的原生模型：`cells`、`scalar`、`records`、`raw_records`、`matrix`、`time_records`、`raw_fields`、`observed_records`、`clock_codes`、`multi_records`、`portal_contract_list`、`portal_contract_curve`、`monthly_trade`、`dynamic_sdk`，以及独立`listing`形态。当前留存captured数据实际出现11种原生模型，公告列表另有真实源回放；scalar/matrix/raw_records只有脱敏夹具契约覆盖，不冒称已取得新的真实样本。

关键约束已落实：

- HTTP200中的业务401/40302/未知码不能算数据；成功码类型发生变化也拒绝。
- 时间序列必须符合已绑定时点、字段、真实scope常量；record/formatRecord不一致即拒绝。仅已明确允许的相同源重复次数可接受，不能将重复值当两个独立曲线。
- 文本状态原样保存；数值状态码包括未知码保留，不解释成价格；显示空白旁仍保留源null及完整`original_data`。
- 多表按当前各表动态投影，不能把先前表的数据冻结到后续日期的header。
- 月度量价保留原始精度；UI显示值的加权合计是明确的派生行，不当原始响应记录。零数量缺价保留为空，非零数量缺价拒绝。
- 门户合同曲线按真实整月及汇总行校验，不当实际负荷。合同列表保留完整原字段，未映射的合同周期/附件仍未映射。
- SDK多页必须页码连续、列定义一致、总数一致、整日期完整；真实HTTP原始页SHA与bundle逐页对应。
- 原生空集合、来源字段映射待定、完整UI投影分别标记；不补零、不推单位，不由日期范围两端推连续覆盖。
- 旧XLSX使用共用`workbooks.xlsx_cells`保留每个单元格的坐标、类型、原始数值字符串、公式和缓存值，不执行公式或推断日期/单位。PDF只检查容器，不能声称全文/扫描件已结构化。

## 148组的资格分层

完整逐项清单见 `../evidence/anhui_phase2.json`：

| 组数 | 本次离线资格 | 边界 |
|---:|---|---|
| 81 | 已留存的原生响应、绑定和parsed逐格重建通过 | 仅各自既有绑定scope，不扩大主体或历史；新pending_review仍保护 |
| 60 | 原始公告列表源、摘要SHA及分页计数通过 | 列表不是附件下载，不从validDate推实际首发或统计期 |
| 4 | 旧XLSX原件SHA及完整单元格读取通过 | 字段/业务语义映射待补 |
| 1 | 公告正文JSON源核验通过 | 保留声明周期，不额外推断披露时间 |
| 1 | 旧PDF原件SHA及容器通过 | 内容/扫描件解析待补 |
| 1 | 历史合同派生JSON参考核验通过 | 缺独立源证明，不能升级为原件验收 |

后7组具体为：三个历史月报组`publication_e0693cbeead18de2`、`publication_c1add81ba5c2f3e5`、`publication_32101a70fcd45761`，以及`portal_contract_summary`（四组XLSX）；`interregional_exchange_monthly`（公告正文源）；`market_disclosure_monthly`（PDF容器）；`portal_historical_contracts`（派生参考）。标题及来源口径以逐项清单和旧计划为准，不根据ID推新业务含义。

全量审计核验约1.37万份captured源与parsed、103份列表源、14份SDK HTTP原始页及42份上述旧载体。准确数量和链摘要以本轮生成的evidence为准；有43份早期捕获只保留可变state binding，明确标记`legacy_mutable_state_binding`。源/parsed/不可变binding哈希、整表重建、原字段保留均逐份核对，失败不跳过冒充成功。

## 正在运行的队列及读取一致性

原采集仍在运行，因此审计是滚动只读视图，**不是冻结生产快照**。输出记录实际读取字节的SHA，以及扫描结束时已变化的plan/registry/journal路径。不可变原件/parsed/binding有独立SHA；不能用不断增加的captured计数证明一次原子状态。清单保留读取时的`pending_review`、`attempting`和其它旧phase，任何历史样本通过都不会解除或重试这些身份。

旧队列兼容需要联合原`plan.units[*].queue_key`、`jobs`、`attempted`、原生`native_attempts_<dataset>`日志和receipt，不能只看pending：

- `native_attempts`先写`attempting`，成功才为`captured`，错误留`pending_review`；已发未确认不回pending。
- captured receipt包含source/parsed路径和SHA、period、selectors；scope摘要由真实selector建立，不只拼日期。
- publication队列有正文page_captures、originals、attachment_references；这些角色分开，不把列表当附件。
- historical_contracts的parsed_path是派生参考；contract_summary的original_path是XLSX，不可统一按JSON源导入。
- 未准入52候选逐项列出`candidate_not_admitted_evidence_required`，不将无新任务、空选项或旧拒绝当永久无数据。

## 验证和重跑

20项脱敏专项覆盖上述数值、缺值、scope、文本状态、多表、整月、SDK分页、原始公式、空导出、认证和资格边界。使用根统一的禁网测试入口运行；专项可从根执行：

```sh
PYTHONPATH=pmos python3 -m unittest discover -s pmos/tests -p 'test_anhui_offline.py' -v
```

只读重跑接口：

```python
from pmos.adapters.anhui import build_inventory
result = build_inventory('/Users/yunyu/Desktop/sd_crawler/anhui')
print(result['unit_status_counts'])
```

不运行安徽旧的audit_native_captures.py入口，因为它会向旧state写审计回执；本统一审计无该写入。线上模板、owner交接、全实际请求计账、完整业务字段语义、PDF内容和旧合同独立源证明仍需后续验收，不以离线清单代替上线许可。
