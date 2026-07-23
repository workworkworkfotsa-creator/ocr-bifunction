"""The repo-relative directories, derived ONCE.

Before this module, 28 files each did `PROJECT_ROOT = Path(__file__).parent` — correct
only while every one of them sat at the repo root. Moving a file one folder down made
`templates/` resolve to a directory that does not exist, and a missing templates folder
does not raise: it loads zero templates and the extraction quietly returns nothing. So
the derivation lives in one place, and it is checked.

The directories themselves are POC proxies (`templates/` stands in for a future DB
table, `spool/` for object storage) — the paths move to the adapter when the IT team
integrates, the callers do not.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIRECTORY.parent

# Fail LOUD rather than resolve to a plausible-looking wrong root. The assumption is
# that the package sits in the repo (uv installs this project editable, so it does).
# If that ever stops holding, every path below is silently wrong — better a crash here
# than zero templates read with no error anywhere.
if not (PROJECT_ROOT / "pyproject.toml").is_file():
    raise RuntimeError(
        f"PROJECT_ROOT resolved to {PROJECT_ROOT}, which holds no pyproject.toml — "
        "the package is no longer inside the repo it derives its data paths from."
    )

ADAPTERS_DIRECTORY = PACKAGE_DIRECTORY / "adapters"
PROOFS_DIRECTORY = PROJECT_ROOT / "proofs"

TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
UI_DIRECTORY = PROJECT_ROOT / "ui"
INPUTS_DIRECTORY = PROJECT_ROOT / "inputs"
OUTPUTS_DIRECTORY = PROJECT_ROOT / "outputs"
