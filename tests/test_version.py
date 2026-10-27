"""The version must have exactly one meaning across pyproject, the package and
the frozen binary. A hand-kept constant that drifts is how /health ended up
reporting 0.2.0 while the package said 1.0.0."""
import re
from pathlib import Path

import steward

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no project version"
    return match.group(1)


def test_package_version_matches_pyproject():
    assert steward.__version__ == _pyproject_version()


def test_daemon_reports_the_package_version():
    from steward.server import daemon
    assert daemon.VERSION == steward.__version__
