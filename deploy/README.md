# 部署模板处于禁用状态

本版是离线交付。两个历史systemd模板都带 `ExecStartPre=/usr/bin/false`；CLI也拒绝run/capture/采集控制/解除保护/启用操作。不要安装、启动或移除门禁来做试验。

目标仍是浏览器owner与业务worker独立、每省账号独立profile与运行根目录；未来worker只能附加原已登录浏览器，不能随服务退出关闭Chrome。当前跨root同账号owner交接、完整真实请求计账、资源维护和模板资格尚未完成，不能照旧模板部署。

上线门槛、小预算和回滚见 `../docs/ACCEPTANCE.md`、`../docs/ARCHITECTURE.md`。本任务没有安装service、timer、cron或Codex自动任务，也没有修改现有生产服务。
