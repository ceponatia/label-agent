from labelagent.config import Config, load_config


def test_default_web_host_is_local_only():
    """Public installs must be local-only until someone opts in (issue #17)."""
    assert Config().web_host == "127.0.0.1"


def test_default_web_access_token_is_empty():
    assert Config().web_access_token == ""


def test_load_config_web_host_env_override(tmp_path, monkeypatch):
    # load_config() calls dotenv's load_dotenv(), which walks *up* from
    # labelagent/config.py's directory looking for a .env -- that search is
    # not anchored to cwd, so monkeypatch.chdir() alone would not stop a
    # real .env sitting above the repo from leaking into this test. Disable
    # it outright so this test only ever sees the env vars set below.
    monkeypatch.setattr("labelagent.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LABELAGENT_WEB_HOST", raising=False)
    monkeypatch.delenv("LABELAGENT_WEB_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LABELAGENT_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("LABELAGENT_WEB_ACCESS_TOKEN", "secret123")

    config = load_config(str(tmp_path / "missing-config.toml"))

    assert config.web_host == "0.0.0.0"
    assert config.web_access_token == "secret123"


def test_load_config_defaults_when_unset(tmp_path, monkeypatch):
    # See comment above: block dotenv's upward .env search so a real .env
    # on this machine can't silently override the defaults under test.
    monkeypatch.setattr("labelagent.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LABELAGENT_WEB_HOST", raising=False)
    monkeypatch.delenv("LABELAGENT_WEB_ACCESS_TOKEN", raising=False)

    config = load_config(str(tmp_path / "missing-config.toml"))

    assert config.web_host == "127.0.0.1"
    assert config.web_access_token == ""
