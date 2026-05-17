from pathlib import Path
import re


def test_harness_artifacts_exist() -> None:
    root = Path(__file__).resolve().parent.parent

    assert (root / "docker-compose.test.yml").exists()
    assert (root / "scripts" / "generate_test_media.sh").exists()
    assert (root / "scripts" / "smoke_test.sh").exists()
    assert (root / "tests" / "harness" / "config" / "config.test.example.yaml").exists()
    assert (root / "tests" / "harness" / "README.md").exists()
    assert (root / "tests" / "fixtures" / "plex" / "item_merged.json").exists()
    assert (root / "tests" / "fixtures" / "jellyfin" / "item_with_metadata.json").exists()


def test_readme_documents_pinned_jellyfin_target_and_required_runbook_topics() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "10.10.x" in readme
    assert "Run Locally" in readme
    assert "Run With Docker" in readme
    assert "Integration Harness" in readme
    assert "Operational Runbook" in readme
    assert "Wrong path mapping" in readme
    assert "Expired Plex or Jellyfin tokens" in readme
    assert "Stuck sync job" in readme
    assert "Jellyfin scan not keeping up with new files" in readme


def test_harness_compose_pins_jellyfin_to_documented_version_line() -> None:
    root = Path(__file__).resolve().parent.parent
    compose = (root / "docker-compose.test.yml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    match = re.search(r"jellyfin/jellyfin:(\d+\.\d+\.\d+)", compose)
    assert match is not None
    pinned_version = match.group(1)

    assert pinned_version.startswith("10.10.")
    assert "10.10.x" in readme
