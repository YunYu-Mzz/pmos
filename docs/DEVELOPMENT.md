# 本地Git、配置和开发验证

## 仓库选择与提交

根目录已是Git仓库，原分支 `liaoning-session-keepalive`，原HEAD `ae8139b`。安徽/山西是根忽略的独立历史项目；本次不进入其Git提交、不改其分支，也不创建嵌套仓库或远端。

统一项目使用根仓库的专门 `pmos/` 目录，工作分支 `codex/pmos-offline-unification`，从原HEAD创建并保留原未提交工作。原根`.gitignore`已有未提交的reporting-private规则，本次没有将其纳入提交。`pmos/.gitignore`独立排除runtime/private、历史live_test_reports、数据库、profile类输出、捕获及原件。

提交按阶段：

1. `86c5cc3 chore(pmos): preserve existing experimental unified framework`：审阅并保存此前未追踪的框架/历史规格基线，482个明确路径，不含live_test_reports、var或生产数据。它是实验历史，不是可部署版本。
2. `9695f13 feat(pmos): add guarded three-province offline adapters and recovery tests`：共用引擎保护、三省离线形态、脱敏夹具及回归测试。
3. 证据、审查/汇总方案、验收预算和开发说明。

只使用显式路径清单暂存（含 `git add --pathspec-from-file=...`），没有 `git add .`、reset、clean、历史改写或公网发布。提交前核对暂存路径、大小、疑似秘密字面量位置、diff和测试；扫描仅报规则/位置，不打印匹配的秘密值。扫描不是凭据轮换或完整安全审计。

本任务不使用远端，也没有创建账号/仓库；未提供远端不阻碍本地完成。以后若需要远端，由用户指定私有目的地和发布范围后再审查，不能整根历史项目直接推送。

## 配置与隔离

`examples/instance.example.json`是非敏感目标契约，不会被当前CLI自动加载；生产instance布局/owner交接未完成前不能直接部署。密码/Cookie/token/短信码不进入配置样例、规格、日志或测试；邮箱授权/HMAC只由原私密sender读取，不迁入统一仓库。

当前 `PMOS_DATA/<province>` 仍是旧实现的实例布局；同省不同账号必须给不同PMOS_DATA，并补齐跨root owner门禁。不要指向任何旧省state/data目录。offline-demo总是使用独立临时目录，无需PMOS_DATA；status改为短只读，不为不存在的实例建库。

## 开发和测试

Python >=3.11；本次用已安装3.12，没有安装依赖。`playwright`、`olefile`是历史实验浏览器/CFB路径的可选需求，离线原生JSON核心不依赖它们。当前项目以源码checkout运行；历史pyproject尚未把外部provinces/fixtures做发行包资源，不把pip wheel当完整可部署发行物。

```sh
cd /Users/yunyu/Desktop/sd_crawler/pmos
python3 tools/test_offline.py
python3 -m pmos offline-demo all
python3 -m pmos liaoning check
python3 -m pmos anhui check
python3 -m pmos shanxi check
```

测试入口禁止IPv4/IPv6 connect/connect_ex，并移除本地UI opt-in；原有UI夹具单独1项跳过。不要为了去掉skip启动浏览器、改变当前profile或安装自动任务。新fixture必须完全脱敏；真实原件只登记受控路径/SHA与摘要，replay只读，不输出业务行。

回归包括：三个decoder和同一Engine；响应后间隔；分页重启；同实例单worker；HTTP200内层错误/二进制MIME错误；缺模板0派发；超时unknown；归档checkpoint崩溃后的已消费保护；坏bytes隔离；待结请求优先于local hold保活；显式40302 scope；有界模板引用；SQLite DELETE journal长游标阻写复现与短读释放；原件路径逃逸；schema2指标计数；旧manifest只读校验。新增测试不访问网站、不读凭据，不写旧队列。

`evidence/validation.json`登记本轮实际运行的版本、测试数、跳过原因、演示摘要及源原件未变核验；无原始业务值。测试通过只表示所测分支，不表示真实浏览器SDK回收、模板、全目录或线上长期运行通过。
