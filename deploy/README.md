# 部署

每个省份运行两个服务：

- `pmos-browser@<省>`：有界面的 Chrome，由人登录，调试端口只监听 127.0.0.1。
- `pmos@<省>`：采集进程，通过本机 CDP 附加到上面的浏览器，`Restart=no`。

步骤：

1. 准备虚拟显示和 VNC（可沿用旧系统的 display/desktop/vnc 服务）。
2. 安装 `pmos-browser@.service`，按实际调整用户、`DISPLAY`、配置目录。
3. 启动浏览器服务，人工登录，打开目标页面。
4. 在运行目录写 `browser.json`，例如：

   ```json
   {"profile_dir": "/home/pmos/pmos-browser/shanxi"}
   ```

5. 安装并启动 `pmos@.service`，然后执行 `python -m pmos <省> start`。
6. 把 `needrestart-pmos.conf` 放进 `/etc/needrestart/conf.d/`，防止系统升级时自动重启浏览器。

不要让新旧两套采集器同时对同一账号发请求。
