from __future__ import annotations

import argparse

import uvicorn

from plex_jellyfin_sync.app import build_application
from plex_jellyfin_sync.config import load_config
from plex_jellyfin_sync.logging_config import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    configure_logging(config)
    app = build_application(config)
    uvicorn.run(app, host="0.0.0.0", port=config.webhook.listen_port)


if __name__ == "__main__":
    main()
