from pathlib import Path

import pytest
from pydantic import ValidationError

from jobaplier.config import Config, Paths, load_config, save_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_loads():
    config = load_config(Path("config.example.yaml"))
    assert isinstance(config, Config)
    # Platshållarsajten skulle ge ett källfel vid första körningen: bara ett exempel.
    assert config.sources.careers == []
    text = Path("config.example.yaml").read_text(encoding="utf-8")
    assert '#   - { type: teamtailor, company: "Exempel AB", id: "exempel" }' in text


def test_defaults(tmp_path):
    config = load_config(write(tmp_path, 'profile: {name: "A", email: "a@x.se"}\n'))
    assert config.scoring.threshold == 60
    assert config.schedule == ["08:00", "12:00", "17:00"]
    assert config.ai_monthly_budget_usd == 10.0
    assert config.ai.backend == "claude_code"
    assert config.ai.claude_bin == "claude"


def test_old_config_with_removed_keys_loads(tmp_path):
    path = write(
        tmp_path,
        'profile: {name: "A", email: "a@x.se"}\n'
        "sources:\n"
        "  platsbanken: {enabled: true}\n"
        "  linkedin_alerts: {enabled: true}\n"
        "apply: {dry_run: false, max_emails_per_day: 3}\n",
    )
    config = load_config(path)  # okända nycklar ignoreras
    assert config.sources.platsbanken.enabled is True

    out = tmp_path / "saved.yaml"
    save_config(config, out)
    text = out.read_text(encoding="utf-8")
    for removed in ("linkedin_alerts", "dry_run", "max_emails_per_day"):
        assert removed not in text


def test_invalid_backend_raises(tmp_path):
    path = write(
        tmp_path,
        'profile: {name: "A", email: "a@x.se"}\nai: {backend: "openai"}\n',
    )
    with pytest.raises(ValidationError):
        load_config(path)


def test_invalid_schedule_time_raises(tmp_path):
    path = write(
        tmp_path,
        'profile: {name: "A", email: "a@x.se"}\nschedule: ["25:00"]\n',
    )
    with pytest.raises(ValidationError):
        load_config(path)


def test_save_roundtrip(tmp_path):
    original = load_config(Path("config.example.yaml"))
    path = tmp_path / "out.yaml"
    save_config(original, path)
    assert load_config(path) == original


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    original = load_config(Path("config.example.yaml"))
    path = tmp_path / "data" / "config.yaml"
    save_config(original, path)  # skapar katalogen
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("jobaplier.config.os.replace", boom)
    changed = original.model_copy(update={"ai_monthly_budget_usd": 99.0})
    with pytest.raises(OSError):
        save_config(changed, path)

    assert path.read_text(encoding="utf-8") == before  # gamla filen orörd
    assert [p.name for p in path.parent.iterdir()] == ["config.yaml"]


def test_paths_from_data_dir(tmp_path):
    paths = Paths.from_data_dir(tmp_path)
    assert paths.cv == tmp_path / "cv.pdf"
