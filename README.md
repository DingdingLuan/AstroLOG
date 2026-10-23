# AstroLOG

A small, read-only web viewer for live logs and file inventories. Follow new output, scroll back through history, jump to a line or position, and download a log. Includes a folder explorer, global filename search, Markdown notes, light/dark themes and mobile layouts.

[Try the demo](https://dingdingluan.github.io/AstroLOG/) · [中文说明](README.zh-CN.md)

The public demo uses synthetic data entirely in your browser. Self-hosting adds real file access and SSE streaming.

![AstroLOG demo](assets/screenshot.png)

## Quick start

Requires Python 3.10+ with `venv`, Bash and Git. No root, Node.js or database needed.

```bash
git clone https://github.com/DingdingLuan/AstroLOG.git
cd AstroLOG
bash scripts/install.sh
cp config/demo.yaml config/config.yaml
bash scripts/start_demo.sh
bash scripts/start.sh
```

Open **http://127.0.0.1:8765/**. The sample producer updates three synthetic logs every 3 seconds.

```bash
bash scripts/stop.sh          # stop the viewer
bash scripts/stop_demo.sh     # stop the sample producer
```

## Your own logs

Copy `config/example.yaml` to `config/config.yaml`, set a source `root`, and list the logs to expose:

```yaml
display:
  title: AstroLOG
  server_name: my-server
  default_file: work.main
data:
  sources:
    work:
      type: local
      root: /path/to/project
      files:
        main: output/run.log
      inventory:
        patterns: ["snapshot_*"]
```

Restart after changing YAML. Relative source and announcement paths resolve from the YAML file's directory; log paths resolve from their source root. Only registered logs can be viewed/downloaded. Filename search covers all configured roots and subdirectories, regardless of inventory patterns.

The viewer never changes source files. SSH collection is not built in: sync remote logs to a local directory separately, then configure that directory as a source.

## Access and deployment

The server binds to `127.0.0.1` by default. To listen on your LAN, run `LOGPRESENT_HOST=0.0.0.0 bash scripts/start.sh`; `LOGPRESENT_PORT` and `LOGPRESENT_CONFIG` are also supported.

For passphrase access, run `.venv/bin/python scripts/set_token.py`, then set `security.enabled: true` and `security.token_file: ../var/auth/access-token`. Sessions use an HttpOnly cookie. Set `security.secure_cookie: true` when serving over HTTPS.

`deploy/` contains optional Nginx and user-systemd examples. For SSE, keep proxy buffering disabled. The detached launcher writes `var/log/server.log`; it does not start automatically after reboot.

Edit `content/announcement.md` to update the optional note without restarting. See the annotated YAML examples for display, refresh and file-list settings.

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -q
```

MIT license · Part of the [AstroSlacker](https://astroslacker.com) family.
