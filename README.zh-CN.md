# AstroLOG

轻量、只读的网页日志查看器：实时跟随输出、向上加载历史、跳转行号或文件位置、下载日志；支持目录树、全局文件名搜索、Markdown 公告、明暗主题和手机布局。

[在线 Demo](https://dingdingluan.github.io/AstroLOG/) · [English](README.md)

在线 Demo 完全在浏览器内使用合成数据；实际读取文件和 SSE 实时推送需要自行运行服务。

## 安装与试用

需要 Python 3.10+（含 `venv`）、Bash 和 Git，无需 root、Node.js 或数据库。

```bash
git clone https://github.com/DingdingLuan/AstroLOG.git
cd AstroLOG
bash scripts/install.sh
cp config/demo.yaml config/config.yaml
bash scripts/start_demo.sh
bash scripts/start.sh
```

打开 **http://127.0.0.1:8765/**，三个合成日志每 3 秒更新一次。

```bash
bash scripts/stop.sh       # 停止网页服务
bash scripts/stop_demo.sh  # 停止示例日志生成
```

## 接入自己的日志

将 `config/example.yaml` 复制为 `config/config.yaml`，修改数据源的 `root` 和 `files`，然后重启服务。`display.server_name` 设置连接状态旁的服务器名；`display.default_file` 使用 `数据源ID.文件ID`。

相对数据源路径、公告路径以 YAML 所在目录为基准；日志路径以数据源 `root` 为基准。只有登记的日志可供查看和下载。全局搜索覆盖所有配置根目录及子目录中的文件名，不受清单通配规则限制。

编辑 `content/announcement.md` 可动态更新公告，无需重启。软件不会修改源文件。尚未内置 SSH 采集，可自行同步远程日志到本地目录后接入。

## 访问配置

默认仅监听 `127.0.0.1:8765`。局域网访问可用：

```bash
LOGPRESENT_HOST=0.0.0.0 bash scripts/start.sh
```

也支持 `LOGPRESENT_PORT`、`LOGPRESENT_CONFIG`。服务后台运行，输出在 `var/log/server.log`；重启机器后需要重新启动。

需要口令时先运行 `.venv/bin/python scripts/set_token.py`，再设置 `security.enabled: true`、`security.token_file: ../var/auth/access-token`。登录状态保存在 HttpOnly Cookie 中；使用 HTTPS 时设置 `security.secure_cookie: true`。

`deploy/` 提供可选的 Nginx 和用户级 systemd 示例；SSE 反向代理需要关闭缓冲。

MIT 许可 · [AstroSlacker](https://astroslacker.com) 系列。
