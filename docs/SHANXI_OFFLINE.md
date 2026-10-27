# 山西第二阶段离线适配

范围：2026-10-27本地留存源码、目录、原件和非敏感契约。仅修改PMOS统一项目；未导入旧生产模块、打开队列数据库、连接浏览器/网站、改变采集/保活/服务/控制。现有统一发行版线上门禁继续保持。

## 当前实际覆盖

新增 `pmos.adapters.shanxi.decode(raw, contract, http_status=200)` 和 `decode_pages`，由同一Engine注入使用。不是把旧worker搬入统一目录。响应/原件形态如下：

| 形态 | 本次离线能力 | 资格限制 |
|---|---|---|
| 原生普通分页、主体名录、实时节点等 | 原字段/重复行/0/负值/null保留；页码/页大小/总数、连续多页SHA及计数核验；日期/主体绑定按明确契约检查 | 页面列表不是全部主体、全部日期；零分页元数据仅允许已核实源配合真实请求页码/页大小 |
| 动态表 | 保留objectList/list结构，明确空动态表；必需字段和真实周期校验 | 空表不解释为全年无数据；HTTP200业务错误先分类 |
| 单记录曲线 | required_fields精确匹配，不补时点、不填0 | 字段存在不自动证明单位/业务意义 |
| 市场力多表、滚动交易行 | 表名和原行字段分别保留；多表不合并成一张有统一语义的数值表 | 不推断TOP4/HHi/MMR/RSI之间单位或业务解释 |
| 公告列表/原生正文 | 列表作为metadata；正文保留原HTML/源字段，纯文本抽取不执行script/style | 发布窗口不是统计期；不代表附件下载或数字字段完整结构化 |
| 机构详情 | 公司applyId绑定原公告、股东infoId绑定同公司；比例范围核验，原值不转换 | 当前快照，不虚构日/月历史；注册资本单位未知 |
| XLSX | 调用共用 `pmos.workbooks.xlsx_cells`；完整源单元格、坐标/行列/类型/style、0/负值/blank及缓存公式保留；CRC/关系路径/实体/重复坐标和大小保护 | 不执行公式、不把样式数值自动解释成日期或单位；完整cells不等于所有业务字段语义验收 |
| CFB/BIFF XLS | 显式识别，与XLSX分开；已有可选xlrd/olefile环境下读取所有缓存单元格；缺依赖拒绝 | 标准库环境缺xlrd/olefile时8次XLS回放拒绝；本轮另仅在离线子进程复用已有xlrd 2.0.2/olefile 0.47，8次原生XLS缓存单元格回放通过，未安装或改生产依赖 |
| PDF/图片/Word/Office附件 | 内容识别、大小/SHA和容器完整性校验，保留源角色 | 只达到容器级；不宣称PDF所有表、扫描件OCR、Word物理合并网格或数字语义已迁入。2次DOC在标准库环境因缺olefile阻断；复用现有可选库后容器校验通过，内容语义仍待补 |

单位只有在响应中明确绑定的单位字段与本次原文SHA一致时才可输出；仅有格式正确的摘要、布尔值或路径都不足以证明单位。外部UI单位证据解析尚未接入时保持null。

## 本地核验结果

可重跑入口 `audit_inventory(Path('/controlled/shanxi'))`，不传上限时读取所有已登记的本地原件；可用 `samples_per_source` 限定每个来源的样本数。函数只返回摘要，不写文件、不打开数据库。调用者将结果保存到PMOS的受控输出路径。

本轮读取12个公开目录/契约/registry快照，联合列出270个技术来源。源代码里102个常规目录、27份native契约、24个query登记、77个公告栏目和附件/正文/工作簿等补充目录存在交叉，不能相加当业务指标数。本地指标registry是214行；它不能替代HANDOFF里后续服务器243行的实测时点数据。

- 共820次已登记原件评估：标准库环境806通过、14阻断；复用现有可选库后816通过、4阻断；涉及779个不同源路径，775个不同源路径至少一次通过。多指标引用同一原件造成重复评估，这些评估不能当作同等数量的新文件，更不是新增网站请求。
- 复用现有可选库后221个技术来源具有本次形态的通过样本；49个缺原件、缺契约或当前拒绝（标准库环境分别211/59），逐项见 `evidence/shanxi_phase2.json`。214个registry行中112行（标准库环境106行）的全部登记ID都已有通过样本映射；其余并未标完成。
- 560条常规manifest原件逐项SHA绑定并全部用共用XLSX reader回放；本地物理工作簿目录有1054个文件，另外494个没有进入这份manifest快照。本次不凭“文件存在”将其当已消费/已确认任务或自动补记成功。
- 原生/公告/附件等另外260次评估包含4份历史40302，继续按permission拒绝；不因其他日期同来源有成功样本而解除旧拒绝或准许重发。其余10份在复用已有可选库后通过，库版本、模块路径/SHA及两种环境资格差异均在证据中保留。
- 18项新纯测试通过，覆盖分页连续性、同页/身份/哈希变化、0/负值/null、源码多表、缺字段、周期/主体漂移、二进制MIME业务40302、HTML原文/脚本隔离、机构归属、缺XLS依赖及坏文件。

所有“通过”均是本地源形态/容器/源单元格样本，`full_dataset_coverage_verified=false`，线上资格仍pending。原件只读，审计在解析前后复算SHA一致；证据仅保存路径/SHA/大小/行数/状态，不含源业务行。证据使用紧凑JSON控制体积。

## 旧队列兼容必须保留的语义

本地 `shanxi/state`（排除浏览器profile）没有保存native requests/work SQLite库，不能在本轮验证生产库内容或宣称已导出队列。只读兼容器应使用明确提供的快照，不能访问服务器补证。

- NativeJSONQueue.requests身份由method/path/body规范化而来；pageInfo的total/pageSizes/size是UI记账字段，从身份排除，pageNum/pageSize和真实筛选保留。status=leased/sent/failed不可再次lease，denied/denied_scope保留；已确认0发送的leased释放也需要精确四项证据，不能套用到sent/unknown。
- PublicationQueue.work的publication scope是menu.guid，attachment scope是fileId；它没有原生period字段。公告command.queryParam.data.startDate/endDate/operateTimeArr是发布查询窗口，不是业务统计期。
- 附件的aliases保留menuId/publication_guid/filename/source_query_sha256。同一fileId可来自多个栏目；字节去重不能抹去这些关联。
- complete先校验原件/绑定，随后在同一事务确认并生成真实下一页/附件；prior_attempt_requires_review、sent和failed都不是新的pending任务。
- 回滚或新身份映射不能用改namespace、改session/template版本、改名或重新seed绕开这些已消费状态。原件manifest单独不足以证明队列所有权。

## 尚需补证

1. 本地目录与最新服务器243行源ID映射，不能按标题猜匹配。
2. 原生XLS已记录可选库和离线原件证明，仍需复杂工作簿、PDF/Word/图片的业务字段语义复核。
3. 本地没有保存的真实原件/完整scope/旧队列快照；依赖/缺证来源保持blocked，不新请求补样本。
4. 全部线上owner交接、原生模板、实际旁路请求、保活和长稳验证，均由统一上线清单另行放行。
