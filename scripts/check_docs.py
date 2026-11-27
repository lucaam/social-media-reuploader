#!/usr/bin/env python3
"""Check that the settings of the application are documented.

src/config.py is the source of truth for the settings. Every environment
variable it reads must be:

- described in README.md (written as `NAME`, e.g. in the configuration table);
- a key of the `env` map of the Helm chart (charts/.../values.yaml), except
  the variables the chart sets itself or takes from Secrets.

README.md must also document REQUESTS_DB (src/db.py) and the settings that
other modules read from an environment mapping with the `_env` helpers: the
GUI settings of src/gui_auth.py (`_env(env, "ADMIN_TOKEN")`,
`_env_int(env, "SESSION_MAX_AGE_SECONDS", ...)`, ...). Other variables read
directly with os.environ / os.getenv are reported as warnings when README.md
does not mention them.

Nothing is imported from src/: the sources are scanned as text.
Exit status: 0 when everything is documented, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "src" / "config.py"
README = ROOT / "README.md"
VALUES = ROOT / "charts" / "social-media-reuploader" / "values.yaml"

CONFIG_READ = re.compile(
    r"\b(?:env_str|env_bool|env_int|env_float|env_list|_raw)\(\s*\"([A-Z][A-Z0-9_]*)\""
)
DIRECT_READ = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*\"([A-Z][A-Z0-9_]*)\"|os\.environ\[\s*\"([A-Z][A-Z0-9_]*)\"\s*\]"
)
# `_env(env, "NAME")`, `_env_bool(env, "NAME", ...)`, `_env_int(...)`: settings
# read from a mapping (src/gui_auth.py), calls possibly split over lines.
MAPPING_READ = re.compile(r"\b_env(?:_[a-z]+)?\(\s*\w+\s*,\s*\"([A-Z][A-Z0-9_]*)\"")

# Not in the chart `env` map, on purpose.
CHART_MANAGED = {
    "PORT": "containerPort / gui.port",
    "BOT_TOKEN": "secret.name / secret.botToken",
    "WEBHOOK_SECRET": "secret.webhookSecretKey / secret.webhookSecret",
}
# Legacy aliases of other settings (documented next to them in README.md).
LEGACY_ALIASES = {
    "YTDLP_COOKIES": "YTDLP_COOKIES_PATH",
    "WORKER_PERIODIC_REHYDRATE_SECONDS": "WORKER_POLL_SECONDS",
}
# Read with os.environ by modules other than config.py and required in README.
REQUIRED_DIRECT = {"REQUESTS_DB"}


def config_settings(text: str) -> list[str]:
    return sorted(set(CONFIG_READ.findall(text)))


def _reads(paths: list[Path], pattern: re.Pattern[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = next(group for group in match.groups() if group)
            found.setdefault(name, []).append(str(path.relative_to(ROOT)))
    return found


def direct_reads(paths: list[Path]) -> dict[str, list[str]]:
    return _reads(paths, DIRECT_READ)


def mapping_reads(paths: list[Path]) -> dict[str, list[str]]:
    return _reads(paths, MAPPING_READ)


def documented(readme: str, name: str) -> bool:
    return f"`{name}`" in readme


def chart_env_keys(values_text: str) -> set[str]:
    values = yaml.safe_load(values_text) or {}
    env = values.get("env") or {}
    return set(env)


def main() -> int:
    readme = README.read_text(encoding="utf-8")
    settings = config_settings(CONFIG.read_text(encoding="utf-8"))
    env_keys = chart_env_keys(VALUES.read_text(encoding="utf-8"))
    others = [p for p in sorted((ROOT / "src").glob("*.py")) if p != CONFIG]
    direct = direct_reads(others)
    mapped = mapping_reads(others)

    errors: list[str] = []
    for name in settings:
        if not documented(readme, name):
            errors.append(f"README.md does not document `{name}` (src/config.py)")
        if (
            name not in CHART_MANAGED
            and name not in LEGACY_ALIASES
            and name not in env_keys
        ):
            errors.append(
                f"{VALUES.relative_to(ROOT)}: `env` has no key {name} "
                "(add it, null = application default)"
            )
    for name in sorted(REQUIRED_DIRECT):
        if not documented(readme, name):
            errors.append(f"README.md does not document `{name}`")
    for name, where in sorted(mapped.items()):
        if name not in settings and not documented(readme, name):
            errors.append(
                f"README.md does not document `{name}` ({', '.join(sorted(set(where)))})"
            )

    warnings = [
        f"`{name}` is read by {', '.join(sorted(set(where)))} but README.md "
        "does not mention it"
        for name, where in sorted(direct.items())
        if name not in settings and not documented(readme, name)
    ]

    for line in warnings:
        print(f"warning: {line}")
    for line in errors:
        print(f"error: {line}")
    if errors:
        return 1
    print(
        f"OK: {len(settings)} settings of src/config.py documented in README.md "
        f"and in the Helm values, {len(mapped)} GUI settings in README.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
