# 第五轮：辽宁11条、安徽10条精确身份桥

2026-10-28。固定第四轮剩余的辽宁11条、安徽10条旧记录，21条精确身份桥全部补齐，剩余未映射为0。随后核查三省剩余原件与资格证据；没有新增业务资格或消费证明。真实补证入口的网站请求、生产写入、数据库连接、旧采集器导入均为0，没有操作现有采集、保活、浏览器、服务或旧队列。

## 本轮结果

| 固定对象 | 身份结果 | 保留的旧事实 |
|---|---|---|
| 辽宁11条旧query | 11条均找到旧身份承诺的唯一preimage，余0 | 5 empty、6 saved、consumed11、facts及非身份blocker保持 |
| 安徽10条旧公告 | 10条均建立同菜单原生listing身份桥，余0 | 5 pending、5 saved、consumed5保持；不继承当前新增saved |
| 辽宁完整历史视图叠加 | 原1,552个缺映射均可表达，13,032条视图无剩余unmapped | 原11,480个身份及前三轮已补身份保持；12,951 pending、74 saved、6 empty、1 unknown、consumed81不变 |
| 安徽最后20条旧记录叠加 | 第四轮10条与本轮10条全部可表达 | 第四轮已有10个身份SHA不变；pending8、saved12、consumed12保持 |

这些是固定历史记录视图，可能与其他快照重叠，不是当前生产队列、独立业务指标或全站迁移结果。所有报告仍`dispatch_allowed=false`，没有导入可执行任务。

## 辽宁：从旧身份摘要精确复原

旧expanded key包含完整dataset命名空间与规范query identity的SHA。六个来源的旧源码和已存队列共同限定了2,072个候选：296个旧日历日期，结合manifest/journal中类型保真的已验证选择。它们正好等于2,054个pending、7个prior与11个旧outcome的互斥并集。11个目标旧key各自唯一命中，不从标题、目录日期、同响应字节或任意字符串猜测。

另核验完整writer链：`spec.params → spec.identity → task_key → pending.job.params → AdvancedArchive/outcome body SHA`，包括间接日期校验、adapter选择器、registry、endpoint/method/number与PREFIX。当前源码、10/23已存release中的四个纯源码成员和10/24保留writer分别有字节SHA及函数AST指纹；没有执行旧采集模块。

AST指纹对Python版本新增的空`type_params`作结构化规范化，非空参数拒绝，不修改字符串字面量。缺失、非字符串、非小写64位hex的必需SHA均拒绝；零/多命中、域不闭合、类型混淆或额外native字段也拒绝。真实Python3.12回放及跨版本比对通过。

**找回的是旧身份承诺的preimage，不是原native payload或响应原件。** 原body SHA仅与冻结outcome关联，没有用缺失原件重新验SHA，不增加原件、权限、语义或发送资格。详见[LIAONING_BRIDGES.md](LIAONING_BRIDGES.md)。

## 安徽：旧事实与新留存listing分开

三个目标来源已经留存各3页原生listing。逐条按旧算法重算原key，同菜单source/parsed双SHA与唯一`(guid, informationId)`相互印证；直接/嵌套业务错误、假布尔、非法分页、跨菜单和冲突ID均拒绝。新身份保持`article`、`snapshot=undated`，发布日期只核对旧公告，不作统计期。

9条旧记录来自第四轮已承诺整文件SHA的旧snapshot，1条来自整份已漂移队列中facts完全未变的旧record。旧菜单同时受已存文件SHA与第四轮`menu_scope_sha256`约束。当前采集已把其中5个旧pending变成saved；本轮仍保留原审计cohort的pending，不据此宣称现在未采，更不会重发。

叠加检查直接使用第四轮20条记录/scope承诺和旧snapshot，不让已变化的当前队列提供旧菜单。旧阶段审计遇引用漂移仍可保守拒绝，未放松其门禁。详见[ANHUI_BRIDGES.md](ANHUI_BRIDGES.md)。

## 三省原件和资格缺证仍分层保留

| 固定缺证cohort | 本轮只读查找 | 结果 |
|---|---|---|
| 辽宁51个来源自身原件，其中34个无own/明确alias链 | catalog/coverage及原件、retained、repair、export的目标metadata | 无新own原件链；原17条typed alias复用关系不增加独立原件或指标 |
| 安徽3次捕获的精确binding与52候选 | 明确命名的binding元数据、旧承诺SHA、inventory/worklist及新留存pending33索引 | 3次仍缺精确copy；52候选不准入，不借其他dataset材料补证 |
| 山西19项业务原件及26项空/别名/拒绝边界 | 目录、回执、原件路径及alias父文件SHA | 19项仍缺；空selector、静态form、拒绝和unknown不升级或消除 |

跟进器拒绝provenance内外层冲突、非业务stage、未证实的数据权限、变化后重新排列的候选数组；没有绑定参考时不把空集合当验证成功。任何扫描内变化都会留下sticky标记，文件恢复也拒绝混合报告。详见[三省缺证跟进](PHASE5_EVIDENCE_FOLLOWUP.md)。

## 复跑与验证

在PMOS源码目录、Python3.11+：

```sh
python3 tools/test_offline.py
python3 tools/recover_existing_evidence.py all --stage bridges
```

`bridges`先核辽宁既有Monday身份，再检查辽宁/安徽固定剩余桥，最后执行所选省份的原件跟进。单省选择不会读取未选省份的baseline或原件；默认不写文件，可选输出仍限PMOS var下不存在的新目录。

离线回归共355项，354通过、1项可选浏览器测试跳过。真实入口、21条before/after、辽宁13,032条叠加及安徽20条叠加，在禁止网络、子进程、全部文件/目录变更、SQLite和旧producer导入的审计环境下通过。三份新报告与独立真实复跑结果完全相同。

- [辽宁逐条身份桥](../evidence/liaoning_phase5_bridges.json)
- [安徽逐条身份桥](../evidence/anhui_phase5_bridges.json)
- [三省缺证跟进](../evidence/three_province_phase5_followup.json)
- [独立验证及代码指纹](../evidence/phase_five_validation.json)

第二至四轮报告不改写。原件、真实参数、选择项和文章ID只在RAM；Git仅存技术ID、路径、资格、状态、计数及SHA。现有采集持续运行，不为审计冻结它；线上验收、owner交接和全部字段语义仍未完成。
