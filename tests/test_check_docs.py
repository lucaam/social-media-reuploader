"""scripts/check_docs.py: every setting (bot and GUI) is documented in README.md."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALUES = Path("charts") / "social-media-reuploader" / "values.yaml"

GUI_SETTINGS = {
    "ADMIN_TOKEN",
    "SECRET_KEY",
    "SESSION_COOKIE_SECURE",
    "SESSION_MAX_AGE_SECONDS",
    "GUI_TRUSTED_ORIGINS",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
    "OAUTH_SERVER_METADATA_URL",
    "OAUTH_AUTHORIZE_URL",
    "OAUTH_TOKEN_URL",
    "OAUTH_USERINFO_URL",
    "OAUTH_JWKS_URI",
    "OAUTH_SCOPE",
    "OAUTH_REDIRECT_URI",
    "OAUTH_ADMIN_GROUPS",
    "OAUTH_ISSUER",
    "OAUTH_TIMEOUT_SECONDS",
    "OAUTH_ASSUME_EMAIL_VERIFIED",
}


@pytest.fixture
def check_docs():
    spec = importlib.util.spec_from_file_location(
        "_check_docs_under_test", ROOT / "scripts" / "check_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def layout(tmp_path, monkeypatch, check_docs):
    """A copy of src/, README.md and the Helm values that check_docs scans."""
    shutil.copytree(
        ROOT / "src",
        tmp_path / "src",
        ignore=shutil.ignore_patterns("static", "__pycache__"),
    )
    shutil.copy(ROOT / "README.md", tmp_path / "README.md")
    (tmp_path / VALUES).parent.mkdir(parents=True)
    shutil.copy(ROOT / VALUES, tmp_path / VALUES)
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    monkeypatch.setattr(check_docs, "CONFIG", tmp_path / "src" / "config.py")
    monkeypatch.setattr(check_docs, "README", tmp_path / "README.md")
    monkeypatch.setattr(check_docs, "VALUES", tmp_path / VALUES)
    return tmp_path


def drop_readme_lines(root, name):
    readme = root / "README.md"
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if f"`{name}`" not in line]
    assert len(kept) < len(lines)
    readme.write_text("".join(kept), encoding="utf-8")


def test_the_repository_documents_every_setting(check_docs, capsys):
    assert check_docs.main() == 0
    out = capsys.readouterr().out
    assert "error:" not in out
    assert f"{len(GUI_SETTINGS)} GUI settings" in out


def test_gui_settings_read_by_gui_auth_are_found(check_docs):
    found = check_docs.mapping_reads([ROOT / "src" / "gui_auth.py"])

    assert set(found) == GUI_SETTINGS
    assert found["SESSION_MAX_AGE_SECONDS"] == ["src/gui_auth.py"]


@pytest.mark.parametrize(
    "name", ["SESSION_MAX_AGE_SECONDS", "GUI_TRUSTED_ORIGINS", "OAUTH_JWKS_URI"]
)
def test_gui_setting_missing_from_the_readme_is_an_error(
    check_docs, layout, capsys, name
):
    drop_readme_lines(layout, name)

    assert check_docs.main() == 1
    out = capsys.readouterr().out
    assert f"error: README.md does not document `{name}` (src/gui_auth.py)" in out


def test_new_gui_setting_must_be_documented(check_docs, layout, capsys):
    gui_auth = layout / "src" / "gui_auth.py"
    gui_auth.write_text(
        gui_auth.read_text(encoding="utf-8")
        + '\n\ndef _audience(env):\n    return _env(\n        env, "OAUTH_AUDIENCE"\n    )\n',
        encoding="utf-8",
    )

    assert check_docs.main() == 1
    assert "`OAUTH_AUDIENCE`" in capsys.readouterr().out


def test_config_setting_missing_from_the_readme_is_still_an_error(
    check_docs, layout, capsys
):
    drop_readme_lines(layout, "TRANSCODE_PRESET")

    assert check_docs.main() == 1
    assert "`TRANSCODE_PRESET` (src/config.py)" in capsys.readouterr().out
