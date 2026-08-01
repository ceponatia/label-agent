import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CONFIG_FILE = "config.toml"
DB_FILENAME = "labelagent.db"


@dataclass
class Config:
    data_dir: str = "data"
    db_path: str = ""
    poll_interval_min: int = 3
    printer_name: str = ""
    print_media: str = "na_index-4x6_4x6in"
    print_media_source: str = "rear"
    auto_print: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    label_retention_days: int = 90
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    anthropic_api_key: str = ""
    classifier_model: str = "claude-haiku-4-5"
    poshmark_sender_domain: str = "poshmark.com"
    vinted_sender_domain: str = "vinted.com"

    def __post_init__(self):
        if not self.db_path:
            self.db_path = str(Path(self.data_dir) / DB_FILENAME)


# field name -> (type, environment variable override)
FIELDS: dict[str, tuple[type, str]] = {
    "data_dir": (str, "LABELAGENT_DATA_DIR"),
    "db_path": (str, "LABELAGENT_DB_PATH"),
    "poll_interval_min": (int, "LABELAGENT_POLL_INTERVAL_MIN"),
    "printer_name": (str, "LABELAGENT_PRINTER"),
    "print_media": (str, "LABELAGENT_PRINT_MEDIA"),
    "print_media_source": (str, "LABELAGENT_PRINT_MEDIA_SOURCE"),
    "auto_print": (bool, "LABELAGENT_AUTO_PRINT"),
    "web_host": (str, "LABELAGENT_WEB_HOST"),
    "web_port": (int, "LABELAGENT_WEB_PORT"),
    "label_retention_days": (int, "LABELAGENT_LABEL_RETENTION_DAYS"),
    "imap_host": (str, "LABELAGENT_IMAP_HOST"),
    "imap_user": (str, "LABELAGENT_IMAP_USER"),
    "imap_password": (str, "LABELAGENT_IMAP_PASSWORD"),
    "imap_folder": (str, "LABELAGENT_IMAP_FOLDER"),
    "anthropic_api_key": (str, "ANTHROPIC_API_KEY"),
    "classifier_model": (str, "LABELAGENT_CLASSIFIER_MODEL"),
    "poshmark_sender_domain": (str, "LABELAGENT_POSHMARK_SENDER_DOMAIN"),
    "vinted_sender_domain": (str, "LABELAGENT_VINTED_SENDER_DOMAIN"),
}

TRUTHY = {"1", "true", "yes", "on"}


def _coerce(value, target: type):
    if target is bool:
        if isinstance(value, str):
            return value.strip().lower() in TRUTHY
        return bool(value)
    if target is int:
        return int(value)
    return str(value)


def load_config(path: str | None = None) -> Config:
    load_dotenv()

    config_path = Path(path) if path else Path(DEFAULT_CONFIG_FILE)
    file_values: dict = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            file_values = tomllib.load(fh)

    values = {}
    for name, (target, env_name) in FIELDS.items():
        raw = file_values.get(name)
        if os.environ.get(env_name):
            raw = os.environ[env_name]
        if raw is not None:
            values[name] = _coerce(raw, target)

    return Config(**values)
