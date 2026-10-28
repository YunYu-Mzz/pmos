# 第五轮剩余原件与证据跟进

本轮固定三省phase4缺证cohort，从已有目录、索引和精确SHA承诺向原件查找。结果见`evidence/three_province_phase5_followup.json`。网站请求、生产写入、数据库连接和新增消费证明均为0；没有重复全量回放、解包归档、读取活动库或执行省份生产代码。旧phase2/3/4报告与模块未改。

这是原件与资格跟进报告；辽宁11条、安徽10条旧任务身份桥由独立第五轮模块审计，不能把本报告的缺证数量当作主身份桥结果。

## 结果

| 固定cohort | 本轮查找 | 结果边界 |
|---|---|---|
| 辽宁51个缺own来源，其中34个无own或明确alias链 | 复核catalog/coverage索引，定向列出五个原件/retained/repair/export根的目标metadata | 索引SHA与phase4相同，未找到目标新增own metadata；既有17条typed alias关系不计为17个新原件或独立指标 |
| 安徽3次捕获的2种精确binding承诺 | 先列绑定文件名录，再检查98份明确命名的native table binding元数据 | 未找到任何raw或历史canonical承诺SHA匹配；3次捕获仍缺精确copy，不能拿当前可变binding替代 |
| 安徽52个未准入候选 | 复核inventory/worklist及新增既存10/28 pending33三份审查索引 | 49项仍无明确dataset映射，3项仅明确技术dataset；与新增33项索引无精确dataset重合，不按标题借证。52项资格不变 |
| 山西19项缺业务原件及26项空/别名/拒绝边界 | 复核目录/披露索引及旧回执、原件路径SHA | 关键索引未变，仍无新的绑定业务原件；缺失的空selector路径没有用别处同空字节替代，26项边界原样保留 |

安徽匹配的是每次旧parsed已承诺的binding摘要，不是“同dataset就可用”的当前状态。两个当前可变binding的canonical SHA均与旧承诺不同；即使将来找到候选copy，也必须再验证同source/parsed/period/typed selectors及精确原投影，不能据文件存在直接升级。

新10/28 pending33的材料确实已在本地，但属于另一个明确dataset集合，未与52个固定候选建立精确关联。没有将新材料变成旧任务consumed，未改旧pending、拒绝或unknown。

山西前轮的三条补强仍分别是：独立回执内嵌空selector可核SHA、同一静态form技术关联且相关selector拒绝、仅月区间查询form；都不是新增业务原件。原4次历史拒绝、14个拒绝来源及7个unknown集合保持。

## 可复跑与只读约束

入口：`pmos.recovery.evidence_followup.recover(workspace, *, provinces=None)`。默认三省，可指定`("liaoning",)`、`("anhui", "shanxi")`等唯一省份子集。未选省份不会读取其baseline或原件。顶层`summary`可供runner展示；函数不写文件。

三份phase4成品SHA固定。读取前拒绝控制路径及省份内每级软链、私密/凭据/profile/auth/HAR/config等目录；只读受控大小的JSON/JSONL，数据库和归档字节在打开前拒绝。JSON重复键、NaN/Infinity拒绝。首读SHA保持，末尾二读全部已用输入；扫描中改变会留下不可清除的本次漂移标记，文件随后恢复也拒绝结果。

provenance外层dataset/sha/expected必须与内层metadata一致。dictionary/filter/period及数据权限未证实的材料不会标为业务样本。安徽inventory/worklist任一SHA较phase4改变即hold候选数组位置映射，不能用两个新数组彼此吻合重解释旧cohort。山西alias即使无原件receipt，也核对其alias父文件SHA；完全没有绑定参考时标不适用，不把空集合当验证成功。

报告仅含技术dataset、cohort摘要、路径、SHA、资格和计数，不含原始请求字段、参数、主体/文章真实ID或业务原文。文件名、标题相同和相同空响应均不独立构成证据。

测试：

```sh
python3.11 -m unittest discover -s tests -p test_evidence_followup.py -v
```

19项脱敏测试覆盖省份子集隔离、固定baseline SHA、父级软链/私密路径、首读SHA及sticky漂移、JSON类型边界、同空字节不能借路径或跨dataset、拒绝不变消费、布尔size拒绝、metadata查询dataset冲突、provenance外层绑定、非业务stage、变动数组索引hold、alias父SHA/空参考、同标题不同menu以及数据库/归档不开。
