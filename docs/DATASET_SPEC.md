> 本文件是保留的历史规格格式参考。当前435个定义均无统一运行时线上资格；CLI已禁用所有线上和启用动作。最新适配约束以ADAPTING.md和ACCEPTANCE.md为准。

# 报表规格格式（`provinces/<省>/datasets/<id>.json`）

文件名必须与 `id` 相同。未知字段会被拒绝，避免拼写错误被静默忽略。

## 通用字段

```json
{
  "id": "queryRqxtfhyc",
  "name": "日前系统负荷预测",
  "status": "enabled",
  "period": {"kind": "day", "start": "2026-01-01", "end": "today", "lag_days": 0},
  "request": {"transport": "fetch", "...": "见下文"},
  "response": {"kind": "xlsx"},
  "keepalive": true,
  "scopes": [],
  "source": {"legacy": "...", "evidence_sha256": "..."},
  "history": [{"at": "...", "event": "sample_ok", "period": "2026-10-11", "sha256": "..."}]
}
```

| 字段 | 取值 |
|---|---|
| `id` | 字母开头，可含字母、数字、下划线，最多 64 字符；迁移报表保留旧 id |
| `status` | `candidate` `sampled` `verified` `enabled` `denied` `quarantined` `retired` |
| `period.kind` | `day`（`2026-10-11`）、`month`（`2026-10`）、`year`（`2026`）、`snapshot`（无统计期，按采样日记一次） |
| `period.start` / `end` | 同类周期值；`end` 可为 `today` |
| `period.lag_days` | 网站晚发布时向前顺延的天数 |
| `period.complete_only` | 月/年报表只采已结束的周期 |
| `response.kind` | `json`、`xlsx`、`xls`、`excel`（两者皆可）、`pdf`、`binary`、`document`（按文件内容识别 pdf/docx/doc/xlsx/xls/png/jpg/zip，不信任文件名） |
| `response.data_field` | JSON 中数据字段名，默认 `data`，可写嵌套路径如 `data.objectList`；为空数组/空对象时记为“已核实为空” |
| `keepalive` | 只有 `enabled` 的有日期报表可以设为 `true` |

## 范围（`scopes`）：同一周期的多个筛选条件

有些报表同一天要按多个筛选值各取一次（断面、机组、申报单位、数据类型等）。这时用 `scopes` 列出全部组合：

```json
"scopes": [
  {"分类": "机组实际出力", "节点名称": "临涣厂#1机"},
  {"分类": "机组实际出力", "节点名称": "临涣厂#2机"}
]
```

- 任务按（报表, 周期, 范围）建立，范围的键是这组取值的稳定哈希（`s` + 15 位十六进制），原件归档在 `original/<报表>/<周期>/<范围键>/`，manifest 里记录 `scope_values`。
- `ui` 传输会把每个范围值自动变成同名标签的下拉选择；`fetch`/`component` 用 `{scope[标签]}` 占位符引用，例如 `"strName": "{scope[strName]}"`。
- 带范围的报表不会因为某个范围连续为空而暂停整个报表（某个机组某天无数据是正常的）。
- 保活报表不能带范围。
- 查看范围键：`python -m pmos <省> datasets --scopes`；对指定范围取样：`sample <报表> <周期> --scope <键>`。

## 占位符

字符串里可使用以下占位符（以周期 `2026-10` 为例；按日报表时 start 与 end 相同）：

| 占位符 | 示例值 |
|---|---|
| `{period}` | `2026-10` |
| `{date}` | `2026-10-01` |
| `{date_compact}` | `20261001` |
| `{start}` / `{end}` | `2026-10-01` / `2026-10-31` |
| `{start_compact}` / `{end_compact}` | `20261001` / `20261031` |
| `{month}` | `2026-10` |
| `{month_compact}` | `202610` |
| `{year}` | `2026` |

## 传输方式

### `fetch`：重放页面请求模板（山西）

```json
"request": {
  "transport": "fetch",
  "method": "POST",
  "path": "/px-phbsx-retail-shop/infoPub/exportShadowPrice",
  "body": {"strName": "龙小线+龙轩线", "infoDate": "{date}"},
  "template": "/px-phbsx-retail-shop/infoPub/exportShadowPrice"
}
```

- 网关请求头（X-Token、CurrentRoute、ClientTag 等）取自页面自己发出的同一路径请求，只保存在内存。每次浏览器会话中需要人或页面先触发一次该请求（例如点一次导出），之后才能用新的请求体重放。
- `template` 默认就是 `path`。
- `query` 用于 GET 请求的查询参数。
- 可选 `max_template_age`（秒）：要求模板足够新。
- 模板还没出现时，该报表**等待**而不是阻断整省：其他报表照常采集，状态显示 `waiting_templates` 和缺哪些模板；在页面上点一次该报表的“导出”即可。
- 可选 `bootstrap`：模板没出现时改用的页面调用（`component`/`native`/`ui`），由页面自己发出同一个请求，原件照常保存，同时页面的这次请求成为模板，之后的周期回到快速重放。山西信息披露页共用同一个页面模板，`exportData()` 用页面自己的令牌把 `queryConfig.data` 发到 `exportUrl`：

  ```json
  "bootstrap": {"transport": "component", "frame": "/pxf-phbsx-infomation/riqiantongtiaofuhe",
                "open_url": "/pxf-phbsx-infomation/#/pxf-phbsx-infomation/riqiantongtiaofuhe",
                "component": "*", "match": {"exportUrl": "/px-phbsx-retail-shop/infoGrid/rqtdxtfhycExcel"},
                "set": {"date": "{date}", "queryConfig.data": {"date": "{date}"}},
                "call": "exportData", "path": "/px-phbsx-retail-shop/infoGrid/rqtdxtfhycExcel"}
  ```

  页面驱动失败（找不到组件等）只暂停该报表（`bootstrap_<原因>`）；保活报表的续期引导失败后不再重试，只等待模板。

### `component`：调用页面组件（辽宁、两省公告）

```json
"request": {
  "transport": "component",
  "method": "POST",
  "path": "/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport017",
  "frame": "/pdbln/report/PdblnReport017",
  "open_url": "/pxf-dif-settlement-outnetpub/#/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport017",
  "component": "pdblnReport017",
  "call": "initData",
  "set": {"queryParam.date": "{date}", "queryParam.name": "风电"}
}
```

- 在 URL 含 `frame` 的 iframe 里查找名为 `component` 的 Vue 组件（不区分大小写），按 `set` 设置参数后调用 `call` 方法，读取它触发的、路径为 `path` 的网络响应。令牌和签名都由网站前端自己计算。
- 报表 iframe 未打开时，用 `open_url` 在页面内插入一个隐藏的同源 iframe，或用 `menu` 逐级点击左侧菜单。同一个前端应用只保留一个隐藏 iframe，换报表时改它的路由（每个应用实例都会在后台轮询网站）。打开报表时网站可能自动查询一次，这次查询计入同一次尝试。
- `match`：同名组件有多个实例时（例如每个公告栏目一个），按字段值选定唯一一个，如 `{"viewId": "<栏目 guid>"}`。`"component": "*"` 表示不限组件名、只按 `match` 找（如山西页面按 `{"queryUrl": "<查询路径>"}`）。
- `args`：调用方法时传入的参数列表。
- `set` 按书写顺序执行，可以先整体替换对象（`"queryParam": {...}`）再改其中字段。

### `native`：调用页面自带的前端函数（公告附件）

```json
"download": {
  "transport": "native",
  "frame": "/pxf-settlement-outnetpub/",
  "module": "63a1", "export": "a",
  "args": ["{attachment[id]}", null],
  "path": "/px-settlement-infpubmeex/fileService/download",
  "method": "GET"
}
```

- 通过页面的 webpack 运行时取到模块 `module` 的导出函数 `export` 并调用，读取它触发的 `path` 响应。附件下载前的 SSO 换票和签名由网站自己完成。
- 模块编号随网站版本可能变化；找不到时报 `native_module_missing`（本地故障，整省挂起等人工处理），不会误存文件。
- `component` 和 `native` 调用期间，页面自己的“另存为”链接点击和 `window.open` 被拦截，文件不会落到浏览器下载目录，也不会弹出新标签页；原件取自网络响应。

### `prepare`：调用前的准备步骤（`component` / `native`）

```json
"prepare": [
  {"component": "chHeader", "call": "setConfig", "await": true,
   "unless": {"component": "chHeader", "equals": {"marketHeader": "PHBSX"}}},
  {"wait": {"component": "chHeader", "equals": {"marketHeader": "PHBSX"}}},
  {"component": "chHeader", "emit": "sendDataToMain", "payload": {"viewId": "..."},
   "unless": {"component": "operationOrganizationQuery", "match": {"viewId": "..."}}},
  {"wait": {"component": "operationOrganizationQuery", "match": {"viewId": "..."}}},
  {"sleep": 0.5},
  {"script": "open_column_menu", "args": {"source": "...", "guid": "..."}}
]
```

| 步骤 | 作用 |
|---|---|
| `{component, match?, set?, emit?, payload?, bus?, call?, args?, await?}` | 在组件上设字段、在事件总线（默认 `$EventBus`）上发事件、调用方法（`await` 时等待其完成） |
| `{wait: 条件}` | 等到条件成立（超时报 `prepare_wait_timeout`） |
| `{sleep: 秒}` | 固定等待，最多 5 秒 |
| `{script: 名称, args?}` | 运行省份目录 `provinces/<省>/scripts/<名称>.js` 中的页面脚本 |
| `unless: 条件` | 条件已成立时跳过该步，避免每页都重新初始化 |

条件写作 `{component, match?, equals?}`：恰好找到一个匹配的组件且 `equals` 中字段全部相等。

**页面脚本**是声明式步骤表达不了时的唯一出口（例如辽宁要在加密的菜单树里找到栏目节点再打开）。每个文件是一个异步函数表达式 `async args => {...}`，出错时 `throw new Error('简短代码')`，该代码会成为故障原因。脚本随省份配置一起审阅，规格只按名称引用；`pmos <省> check` 会检查被引用的脚本是否存在。

## 公告与附件（`documents`）

三省的信息披露栏目共用同一套后台：列表接口返回 `{guid, title, operateDate, attachment: "[{id,name}]", fyear, fmonth, fday, marketId}`，附件经 `fileService/download` 下载。一个栏目写成一个规格：`request` 是分页的列表请求，`documents` 描述如何从记录里取出附件、如何下载：

```json
"period": {"kind": "snapshot"},
"request": {"transport": "component", "...": "列表请求", "pagination": {"page": "queryParam.pageInfo.pageNum", "size": "queryParam.pageInfo.pageSize", "page_size": 20, "records": "data.list", "total": "data.total"}},
"response": {"kind": "json"},
"documents": {
  "fields": {"id": "guid", "title": "title", "published": "operateDate"},
  "published_format": "epoch_ms",
  "expect": {"marketId": "PDBLN"},
  "filter": {"publishType": "公开"},
  "attachments": {"path": "attachment", "json_string": true, "id": "id", "name": "name"},
  "period": [
    {"title_regex": "(20\\d{2})年\\s*(1[0-2]|0?[1-9])月", "kind": "month"},
    {"fields": {"year": "fyear", "month": "fmonth", "day": "fday"}}
  ],
  "stop_at_known": true,
  "download": {"transport": "native", "...": "见上"},
  "download_response": {"kind": "document"}
}
```

- **列表任务**：每天一个（`snapshot` 周期），逐页取列表，每页原件归档为 `p0001`、`p0002`……；列表按发布时间倒序，`stop_at_known` 时遇到一整页都是已知记录就停止，不再翻旧页。
- **附件任务**：每个附件一个任务（范围键 `f` + 文件 id 哈希），记录和附件信息存入台账 `items` 表，重启后可重建下载请求。原件归档在 `original/<栏目>/<业务周期>/<f…>/`，manifest 记录记录 id、标题、发布日期、附件名、周期来源。
- **业务周期**按 `period` 规则依次推断：标题里的“2026年9月”、记录的 fyear/fmonth/fday；都没有时记为 `pub-<发布日期>`（发布日期不是统计期）。规则 `kind` 可为 `day` `month` `year` `week` `quarter`（支持“第三季度”）和 `week_of_day`（按日期范围命名的周报，取起始日所在 ISO 周）。迁移工具里的 `TITLE_PERIODS` 是三省共用的一套规则。
- `expect` 不符说明网站数据变了，该栏目隔离；`filter` 只是筛掉不需要的记录。
- `record_only: true`：记录本身就是要取的对象（每条记录一个任务，`{record_id}` 下载详情）。
- 下载请求可用的占位符：`{attachment}`、`{attachment[id]}`、`{attachment[name]}`、`{record}`、`{record[字段]}`、`{row}`（记录按页面显示的样子，附件字段只保留这一个文件，供 `fileDownload(row)` 这类组件方法使用）、`{title}`、`{record_id}`、`{published}`、`{period}`。整串恰好是 `{名称}` 且值为对象时，传入的是对象本身。
- 适配：`sample <栏目> <日期>` 取第一页列表（有附件则 `candidate → sampled`）；`sample <栏目> <日期> --scope file` 下载最近一次样本里最新的一个附件，通过文件校验后 `sampled → verified`。

### `ui`：模拟人工填表（安徽）

```json
"request": {
  "transport": "ui",
  "frame": "infoCode=xnyzclly_r",
  "menu": ["现货市场", "省内现货市场", "市场预测信息", "新能源总出力预测(日)"],
  "fields": [
    {"kind": "date", "placeholder": "选择日期", "value": "{date}"},
    {"kind": "select", "label": "数据类型", "value": "风电总出力预测数值"}
  ],
  "action": "export"
}
```

- 依次填写 `fields`，点击“查询”（`query_button` 可改文字），等待加载结束。
- `action: "query"` 返回查询响应；`"export"` 再点“导出”（`export_button`），保存下载的文件。
- 可选 `path`：只接受该路径的查询响应。
- 一次 ui 操作可能触发多个网站请求（切换日期自动查询、查询、导出），它们一起计为一次尝试，并共用响应后间隔。
- `menu` 按完整路径（从顶部栏目到叶子）定位左侧菜单树节点，同名标签出现在不同栏目下也不会点错；叶子已是当前项时不再点击。被悬浮菜单遮挡的栏目加 `"menu_hover_exit": true`。

文章列表页（安徽公告）用另外两种动作，配合 `documents` 使用：

- `"action": "list"`：读取当前菜单下表格的文章行（标题、发布日期两列，`columns` 可改列号），用分页框的页码输入或上一页/下一页按钮翻页，返回 `{"data": {"list": [{id, title, published, page}], "total", "page", "last"}}`。`id` 是标题+日期的哈希（页面没有记录 id）。分页写 `"pagination": {"page": "page", "records": "data.list", "total": "data.total", "last": "data.last"}`。
- `"action": "record"`（`documents.download`，需要 `record_only`）：回到记录所在页（找不到时在前 5 页里找），点标题打开文章弹窗，核对标题和日期，逐个点击附件按钮（`skip_buttons` 默认排除“预览”）保存下载；没有附件时保存正文图片；都没有时保存正文文字和表格（`body.json`）。一篇文章的多个文件作为一次尝试，分别校验、分别归档（范围键后缀 `.01`、`.02`……）。只有标题的空文章报 `record_detail_unavailable`。
