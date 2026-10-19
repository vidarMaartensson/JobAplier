import plistlib
from pathlib import Path

import pytest

from jobaplier import cli
from jobaplier.cli import (
    PROJECT_DIR,
    TEMPLATE,
    build_parser,
    install_launchd,
    launchd_path,
    render_plist,
)


@pytest.mark.parametrize("command", ["serve", "gmail-login", "run-once", "install-launchd"])
def test_cli_parses_commands(command):
    assert build_parser().parse_args([command]).command == command


def test_cli_without_command_defaults_to_serve():
    assert build_parser().parse_args([]).command is None
    assert cli.command_of(build_parser().parse_args([])) == "serve"


def test_cli_rejects_unknown_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bogus"])


def _fake_claude(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "tools" / "bin"
    bin_dir.mkdir(parents=True)
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    return claude


def test_plist_render(tmp_path):
    claude = _fake_claude(tmp_path)
    project = tmp_path / "Mitt projekt & co"
    text = render_plist(
        uv="/opt/homebrew/bin/uv",
        project_dir=project,
        path_env=launchd_path(str(claude)),
        home="/Users/test",
    )

    assert str(project.resolve()).replace("&", "&amp;") in text
    assert "se.jobaplier" in text
    assert "<key>KeepAlive</key>" in text
    plist = plistlib.loads(text.encode())
    assert plist["Label"] == "se.jobaplier"
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/uv", "run", "--project", str(project.resolve()), "jobaplier", "serve",
    ]  # fmt: skip
    assert plist["WorkingDirectory"] == str(project.resolve())
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["StandardOutPath"] == f"{project.resolve()}/data/logs/jobaplier.log"
    assert plist["StandardErrorPath"] == f"{project.resolve()}/data/logs/jobaplier.err.log"
    env = plist["EnvironmentVariables"]
    assert env["HOME"] == "/Users/test"
    assert env["PATH"].split(":")[0] == str(claude.parent)
    assert env["PATH"].split(":")[1:] == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def test_launchd_path_without_claude_falls_back_to_homebrew():
    path = launchd_path("/does/not/exist/claude")
    assert path == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def test_install_launchd_writes_plist_with_configured_claude(tmp_path, monkeypatch, capsys):
    claude = _fake_claude(tmp_path)
    home = tmp_path / "home"
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(
        "profile: {name: Test, email: test@example.se}\n"
        f"ai: {{backend: claude_code, claude_bin: '{claude}'}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBAPLIER_DATA_DIR", str(data))

    project = tmp_path / "proj"
    target = install_launchd(home=home, project_dir=project, uv="/opt/uv/bin/uv")

    assert (project / "data" / "logs").is_dir()
    assert target == home / "Library" / "LaunchAgents" / "se.jobaplier.plist"
    plist = plistlib.loads(target.read_bytes())
    assert plist["ProgramArguments"][0] == "/opt/uv/bin/uv"
    assert plist["WorkingDirectory"] == str(project.resolve())
    assert plist["EnvironmentVariables"]["PATH"].startswith(f"{claude.parent}:")
    assert plist["EnvironmentVariables"]["HOME"] == str(home)
    out = capsys.readouterr().out
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/se.jobaplier.plist" in out


def test_install_launchd_without_config_uses_default_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAPLIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # inget `claude` att hitta

    target = install_launchd(
        home=tmp_path / "home", project_dir=tmp_path / "proj", uv="/opt/uv/bin/uv"
    )

    env = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
    assert env["PATH"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def test_template_lives_in_project():
    assert TEMPLATE == PROJECT_DIR / "launchd" / "se.jobaplier.plist.template"
    assert TEMPLATE.is_file()


def test_readme_warns_against_run_once_while_serve_runs():
    """Låset för "en körning åt gången" gäller bara inom en process."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "Kör inte `jobaplier run-once` medan `jobaplier serve`" in readme
