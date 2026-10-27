# Steward PyInstaller spec: one-file console binary named steward.
#
# Build: make build-linux (or from the repo root)
#   pyinstaller --noconfirm --clean --distpath dist --workpath build \
#     packaging/pyinstaller.spec
#
# The package version is read from pyproject.toml at build time. On Linux the
# version is stamped through the bundled pyproject.toml data file so
# `steward version` reports the package version from the frozen binary.
# The Windows EXE version resource is intentionally not stamped: PyInstaller
# does not cross-compile and there is no Windows build on this project yet.
#
# No models are bundled. Steward serves models through the local Ollama
# backend at runtime; model binaries stay out of the application package.

from pathlib import Path

# The .spec file lives in packaging/, so the repo root is two levels up.
ROOT_DIR = Path(SPEC).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"


def _project_version() -> str:
    import tomllib

    with open(ROOT_DIR / "pyproject.toml", "rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


VERSION = _project_version()

# steward.webapp does not exist yet. If it is added later, it is bundled
# automatically (no spec change needed).
HIDDEN_IMPORTS = ["steward.server", "steward.server.daemon"]
if (SRC_DIR / "steward" / "webapp.py").exists() or (SRC_DIR / "steward" / "webapp").exists():
    HIDDEN_IMPORTS.append("steward.webapp")

a = Analysis(
    # Entry point is packaging/launcher.py, not cli.py directly: PyInstaller
    # runs a script path as __main__, which breaks steward.* relative imports.
    # The launcher imports steward.cli as a package instead.
    [str(ROOT_DIR / "packaging" / "launcher.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    # Bundle pyproject.toml so `steward version` reports the real version from
    # the frozen binary (cli.py falls back to importing it when importlib
    # metadata is absent, which is always true in a one-file binary). It is
    # placed at both the extraction root and the steward/ package dir because
    # the exact virtual path of __file__ differs between one-file builds.
    datas=[
        (str(ROOT_DIR / "pyproject.toml"), "."),
        (str(ROOT_DIR / "pyproject.toml"), "steward"),
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc",
        "distutils",
        "setuptools",
        "pip",
        "pytest",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="steward",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
