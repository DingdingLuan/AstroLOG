"""Run the read-only app as an ordinary user; no system services required."""

import argparse

import uvicorn

from .app import create_app
from .config import resolve_server


def main():
    parser = argparse.ArgumentParser(description="AstroLOG read-only live log viewer")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--host", help="Listen address (overrides LOGPRESENT_HOST and server.host)")
    parser.add_argument("--port", type=int, help="Listen port (overrides LOGPRESENT_PORT and server.port)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    try:
        app = create_app(args.config)
        server = resolve_server(app.state.settings.server, args.host, args.port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=server.host, port=server.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
