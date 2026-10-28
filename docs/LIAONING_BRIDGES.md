# 辽宁 11 条旧身份承诺的唯一 preimage（第五轮）

固定第四轮报告 SHA `61473f8a198eba8fc87bf54268a3688c657ce97983ec8bac73680196bea61983` 中的 11 个 source_ref＋old key hash＋facts，全部在旧源调度约束域中找到唯一身份 preimage，并与原 outcome body SHA 关联。原 5 empty、6 saved、11 consumed、facts 和非身份 blocker 均保持。

本轮恢复的是 **旧身份承诺的唯一 preimage**。没有找回原 native payload 字节、浏览器额外空字段或响应原件，没有增加原件、权限、业务接受或可发送任务。旧 body SHA 来自原冻结 outcome，本轮没有拿缺失的原件重新核验该 SHA。

## 封闭域依据

旧 `task_key` 的完整 dataset 命名空间后，是 query identity 使用 `ensure_ascii=False`、`sort_keys=True`、紧凑分隔符序列化后的 SHA-256。普通日 identity 只有 period，以及实际存在且类型保真的 selection。候选值没有来自标题、归档目录日期、文件名或任意字符串。

- 日历由旧 `periods`/`valid_day` 规则限定为 2026 日历年起点至冻结 queue 的 `seeded_date`，共 296 个明确日期；不使用本机当前日期。报告保存该边界的来源文件 SHA、JSON pointer、首尾值 hash 和完整日历域 hash，不输出旧查询参数。
- 选择域来自原 manifest 的已验证集合，与旧嵌套 journal 的 verified_selections 类型、顺序和值精确一致，且逐项属于其已披露 selections。realtime_node_price 是已披露 1,324 个选择中的 verified 1 个；dayahead_power_and_average_price 是已披露且 verified 的 2 个。其余四个报告没有选择维度，只允许源码规定的空占位，不把它加入 query identity。
- 每个已验证选择都由旧 journal 的 saved prior task、manifest proof 和相同 body SHA 关联。参数数字与字符串不互转，bool、未披露、重复或额外维度被拒绝。
- 每个保留 pending job 的 dataset、period、selection 类型、page、完整 params 和旧 key 都与同一源码构造规则一致；params 连额外空字段也不能忽略。

| 旧 dataset 域 | 候选 identity | 旧 pending | prior outcome | 本轮 11 个旧 outcome |
|---|---:|---:|---:|---:|
| dayahead_power_and_average_price | 592 | 588 | 2 | 2 |
| realtime_node_price | 296 | 294 | 1 | 1 |
| sitewide_2010f5bbae811586 | 296 | 293 | 1 | 2 |
| sitewide_90426958a40aec46 | 296 | 292 | 1 | 3 |
| sitewide_c1a062960820eff8 | 296 | 293 | 1 | 2 |
| sitewide_e814f8cf448bfafb | 296 | 294 | 1 | 1 |
| 合计 | 2,072 | 2,054 | 7 | 11 |

2,072 是这六个旧源的调度约束域上限，不是全站、全部节点或任意 native payload 的可能集合。程序核对三组 key 互不重叠，且 `pending ∪ prior ∪ current outcomes` 与候选 key 集合完全相等。每个目标完整 old key 都恰好命中一次；零命中、多命中、重复/碰撞候选、超界或域不闭合时拒绝生成映射。唯一性仍依赖 SHA-256 的抗碰撞假设。

## 完整 writer 链及其限制

不能仅因 `ReportAdapter.identity` 忽略未知字段，就把一个 hash preimage 说成任意原生 payload。这里另核对并 pin 了构造链：

1. `source_jobs` 仅从明确 period 和 verified selections 调用 `spec.params`，构造该 report 的确切 period/selection 字段。
2. 同一个 `p` 传给 `spec.identity`、`task_key`，再原样写入 pending job.params。
3. `dispatch` 将 job.params 传给 `AdvancedArchive.save`，archive 用相同 identity/key 算法及原响应 body 的 SHA 建立归档事实，dispatcher 再将该 SHA 写入同一 old key 的 outcome。
4. manifest 的 dataset、endpoint、POST method、report number、route 和实际 date/selection 规则必须全部一致。

当前相关函数的完整规范 AST 被固定，包括间接调用的 validate_period、expanded.adapter 和 sitewide.adapter；PREFIX 常量另作精确值校验。10/23 既有 release 归档中的 params、identity、selections、periods、task_key、valid_day、选择器、AdvancedTasks 和 AdvancedArchive 算法也通过相同 AST 校验；注册表的六个 binding 与当前留存源码一致。该归档没有 server_verified_sources，报告如实使用另存的 10/24 memory_candidate/before writer 源码作历史佐证，单列其 dispatch 指纹；make_manifest、source_jobs、seed_new_dates 与当前实现一致。

归档先列成员，只读取四个明确且有界的纯 Python 源码成员，不落盘提取、不导入或执行旧采集器，不读私密成员。源码校验与 2,054 个已保存 pending 参数实例共同支持这条旧 writer 规则；不声称证明运行进程加载了哪个版本，也不声称重建浏览器 Vue 内可能附加的空字段或其他 native payload 字节。

AST 规范化只在 deepcopy 的 FunctionDef/AsyncFunctionDef/ClassDef 节点上省略空 type_params 字段，非空类型参数直接拒绝，不对 dump 字符串做替换，字符串字面量保持不变。Python 3.12.14 与系统 3.9.6 真实 source 回放生成完全相同的报告，避免 3.12 新空字段导致原指纹全部失配。

manifest、冻结 queue 和两个原始 journal 的 SHA 另追溯到第四轮所引用的第三轮报告。修改后的新元数据不能替代旧域锚点。报告只保存 technical dataset、路径、function/JSON pointer、计数和 hash，真实 preimage、参数、选择值只留在 RAM。

## 接口、结果与守恒

`pmos/recovery/liaoning_bridges.py` 提供：

```python
baseline_queue_records(root)  # 仅固定第四轮 11 条 before，真实旧 key 在 RAM
resolve_queue_records(root)   # 相同 11 条，填入可表达的身份
recover(root)                 # 脱敏 evidence 报告，不自行写盘
```

source_ref 和 old key 必须同时匹配，不能把同 key 的另一份快照加入 cohort。只解除 identity_mapping_required 和已证实的 source_parameter_identity_mapping_required，其余 state、consumed、facts、owner/stage/source hold 保留。5 empty、6 saved 都仍是已消费事实，所有结果继续 dispatch_allowed=false。根层另做完整 13,032 条视图的叠加守恒，本模块不覆盖既有 13,021 个映射。

本轮还只读核对了已留存 10/28 checkpoint 中对应 11 个 outcome key 仍存在；该检查仅计 key，不暗示终态或 body 内容重新比对，也不增加身份或原件恢复数。原件缺证与技术来源别名资格继续沿用上一轮分层。

`evidence/liaoning_phase5_bridges.json` 保存 11 个旧 key hash、preimage commitment hash、PMOS 身份 hash、构造的旧 job.params hash、原 outcome body SHA、闭包域和 writer 证据链。本轮新增原件为 0。

19 项合成回归在指定 Python 3.12.14 通过：封闭域、typed 选择、固定日历、零/多命中、2072 上限、额外空参数、endpoint/method/number/PREFIX、间接 validate_period/selector 语义漂移、跨版本 AST/非空类型参数、坏 literal schema、同 key 跨快照、旧 sample commitment、历史输入 SHA、unknown/failed/denied/hold 保留。承诺 hash 的 None、空字符串、短值、大写和非字符串均拒绝，不会用 None==None 满足 sample 关联。真实 11 条在 3.12.14 也逐项核对身份外字段守恒；禁网、禁止 SQLite/源目录写和旧模块导入的 audit hook 计数全为 0。任一输入漂移直接拒绝，不返回混合报告。采集与保活未操作，phase2/3/4 报告和旧模块未改。
