from __future__ import annotations

from pathlib import Path

from plex_jellyfin_sync import __main__
from plex_jellyfin_sync.config import load_config


def test_main_uses_configured_webhook_port(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
webhook:
  listen_port: 8091
""".strip()
    )
    calls = []

    def fake_run(app, host: str, port: int) -> None:
        calls.append((host, port, app))

    monkeypatch.setattr(__main__.uvicorn, "run", fake_run)
    monkeypatch.setattr(__main__, "build_application", lambda config: {"port": config.webhook.listen_port})
    monkeypatch.setattr(__main__, "load_config", load_config)
    monkeypatch.setattr(__main__.argparse.ArgumentParser, "parse_args", lambda self: type("Args", (), {"config": str(config_path)})())

    __main__.main()

    assert calls[0][0] == "0.0.0.0"
    assert calls[0][1] == 8091
