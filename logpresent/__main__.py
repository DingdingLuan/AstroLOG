"""Run the read-only app as an ordinary user; no system services required."""

import argparse

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="AstroLOG read-only live log viewer")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    try:
        app = create_app(args.config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
