"""Steward: the personal AI you own, served on hardware you already have.

A personal-assistant application (Ghost-Core competitor) that turns a certified
set of hardware into an always-on local AI: automatic hardware detection,
automatic open-model selection and serving, phone and computer access, and a
perpetual-license business model.
"""

# One source of truth: the installed package metadata, written from
# pyproject.toml. The literal fallback only applies to an unpackaged tree.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("steward")
    except PackageNotFoundError:  # source tree or a frozen bundle
        # Kept equal to pyproject.toml; test_version_single_source enforces it.
        __version__ = "1.3.1"
except Exception:  # noqa: BLE001 - never fail an import over a version string
    __version__ = "0.0.0+unknown"
