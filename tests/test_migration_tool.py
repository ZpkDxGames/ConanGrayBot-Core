import copy
import json
import os

import pytest

from backend.config import DEFAULT_BOT_CONFIG
from scripts.google_drive_oauth_setup import set_env_values
from scripts.migrate_config import main, migrate_snapshot, private_destination


def test_migration_preserves_custom_persona_catalog_and_legacy_locations():
    original = copy.deepcopy(DEFAULT_BOT_CONFIG)
    original["ai"]["personality"] = "Customized private persona"
    original["games"]["guessSongRounds"] = ["Custom | alias | hint"]
    original["weather"]["userLocations"] = {"789": "Custom City"}
    before = copy.deepcopy(original)
    migrated = migrate_snapshot({"guildId": "123", "config": original})
    assert original == before
    assert migrated["config"]["ai"]["personality"] == before["ai"]["personality"]
    assert (
        migrated["config"]["games"]["guessSongRounds"]
        == before["games"]["guessSongRounds"]
    )
    assert migrated["config"]["weather"]["userLocations"]["789"] == "Custom City"
    assert migrate_snapshot(migrated) == migrated


def test_dry_run_never_writes_and_apply_preserves_private_backup(tmp_path, capsys):
    source, output, backup = (
        tmp_path / name for name in ["source.json", "output.json", "backup.json"]
    )
    source.write_text(json.dumps(DEFAULT_BOT_CONFIG))
    assert main([str(source)]) == 0
    assert not output.exists() and not backup.exists()
    arguments = [
        str(source),
        "--apply",
        "--output",
        str(output),
        "--backup",
        str(backup),
    ]
    assert main(arguments) == 0
    assert json.loads(backup.read_text()) == DEFAULT_BOT_CONFIG
    assert json.loads(output.read_text())["schemaVersion"] == 4
    assert os.stat(backup).st_mode & 0o777 == os.stat(output).st_mode & 0o777 == 0o600
    assert main(arguments) == 1
    assert "personality" not in capsys.readouterr().out


def test_migration_errors_never_print_input_values(tmp_path, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"secret": "fictional-confidential-value"}))
    assert main([str(source)]) == 1
    result = capsys.readouterr()
    assert "fictional-confidential-value" not in result.out + result.err


def test_repository_output_rejected():
    from scripts.migrate_config import ROOT

    with pytest.raises(ValueError):
        private_destination(ROOT / "public-export.json")
    assert private_destination(ROOT / "private" / "snapshot.json").is_relative_to(
        ROOT / "private"
    )


def test_oauth_env_write_is_atomic_private_and_preserves_other_settings(tmp_path):
    path = tmp_path / "environment"
    path.write_text(
        "# private file\nOTHER=value\nGOOGLE_DRIVE_AUTH_MODE=service_account\n"
    )
    os.chmod(path, 0o644)
    set_env_values(
        path,
        {
            "GOOGLE_DRIVE_AUTH_MODE": "oauth_user",
            "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN": "fictional-refresh",
        },
    )
    assert "OTHER=value" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".oauth-env-*"))
