"""Steward: the personal AI you own, served on hardware you already have.

A personal-assistant application (Ghost-Core competitor) that turns a certified
set of hardware into an always-on local AI: automatic hardware detection,
automatic open-model selection and serving, phone and computer access, and a
perpetual-license business model.
"""


def _source_tree_version() -> str | None:
    """The version in the pyproject.toml beside this source tree, if any.

    A source checkout wins over installed metadata on purpose. The build venv
    in the repository (.venv) installs the project at whatever version was
    current when it was created, so after a version bump the metadata still
    reports the old number and every check that reads __version__ disagrees
    with pyproject until that venv is rebuilt. Reading pyproject directly means
    a source tree always tells the truth about itself. A wheel install has no
    pyproject beside the package and a frozen bundle has neither, so both fall
    through to metadata and then to the literal below.
    """
    try:
        import re
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        match = re.search(r'^version = "([^"]+)"',
                          pyproject.read_text(encoding="utf-8"), re.MULTILINE)
        return match.group(1) if match else None
    except Exception:  # noqa: BLE001 - a version string is never worth an import error
        return None


# One source of truth: pyproject.toml in a source tree, else the installed
# package metadata. The literal fallback only applies to an unpackaged tree.
__version__ = _source_tree_version()
if not __version__:
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version

        try:
            __version__ = _pkg_version("steward")
        except PackageNotFoundError:  # frozen bundle
            # Kept equal to pyproject.toml; test_version_single_source enforces it.
            __version__ = "1.3.3"
    except Exception:  # noqa: BLE001 - never fail an import over a version string
        __version__ = "0.0.0+unknown"
