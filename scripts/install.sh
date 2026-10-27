#!/bin/sh
#
# Steward install script. POSIX sh only. Works with the cURL one-liner form:
#
#   sh -c "$(curl -fsSL https://github.com/dmltallen/steward/raw/branch/master/scripts/install.sh)"
#
# Downloads the release asset matching this machine architecture from gitea,
# verifies its sha256 checksum, installs it to $HOME/.local/bin (override with
# STEWARD_PREFIX), and prints the next command.
#
# Environment overrides:
#   STEWARD_GITEA_INSTANCE   gitea root, default https://github.com
#   STEWARD_REPO             owner/repo, default don/steward
#   STEWARD_VERSION          explicit release tag, default latest release
#   STEWARD_INSTALL_BASE     base URL with <version>/<asset>[.sha256],
#                            default <instance>/<repo>/releases/download
#   STEWARD_PREFIX           install prefix, default $HOME/.local
#   STEWARD_GITEA_TOKEN      token for a private repository (alias STEWARD_TOKEN).
#                            Required while the repo is private: anonymous API and
#                            asset URLs answer 404, not 401.
#
set -eu

GITEA_INSTANCE=${STEWARD_GITEA_INSTANCE:-https://github.com}
REPO=${STEWARD_REPO:-don/steward}
VERSION=${STEWARD_VERSION:-}
PREFIX=${STEWARD_PREFIX:-"$HOME/.local"}
INSTALL_BASE=${STEWARD_INSTALL_BASE:-"$GITEA_INSTANCE/$REPO/releases/download"}
BINDIR="$PREFIX/bin"
TOKEN=${STEWARD_GITEA_TOKEN:-${STEWARD_TOKEN:-}}

say() { printf '%s\n' "$*"; }
die() { say "error: $*" >&2; exit 1; }

# curl with the token when one is configured. Kept as a function so every
# request in this script carries the same auth (or none, in public mode).
fetch() {
  if [ -n "$TOKEN" ]; then
    curl -H "Authorization: token $TOKEN" "$@"
  else
    curl "$@"
  fi
}

private_repo_hint() {
  say "hint: this repository is private, so anonymous URLs answer 404." >&2
  say "      set STEWARD_GITEA_TOKEN=<gitea token> and run again," >&2
  say "      or install a release asset directly." >&2
}

# --- detect architecture --------------------------------------------------
case "$(uname -m)" in
  aarch64 | arm64)
    ARCH=arm64
    ;;
  x86_64 | amd64)
    ARCH=amd64
    ;;
  *)
    die "unsupported architecture: $(uname -m)"
    ;;
esac

# --- resolve release version ----------------------------------------------
if [ -z "$VERSION" ]; then
  api_url="$GITEA_INSTANCE/api/v1/repos/$REPO/releases/latest"
  say "resolving latest release from $api_url"
  if ! latest=$(fetch -fsSL --max-time 30 "$api_url" 2>/dev/null); then
    if [ -z "$TOKEN" ]; then private_repo_hint; fi
    die "could not query the latest release; set STEWARD_VERSION (and STEWARD_GITEA_TOKEN for a private repo) and retry"
  fi
  VERSION=$(printf '%s' "$latest" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [ -n "$VERSION" ] || die "could not parse the latest release tag; set STEWARD_VERSION and retry"
fi

ASSET="steward-linux-$ARCH"
BASE="$INSTALL_BASE/$VERSION"
URL="$BASE/$ASSET"
SHA_URL="$BASE/$ASSET.sha256"

# --- download and verify --------------------------------------------------
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/steward.XXXXXX") || die "mktemp failed"
trap 'rm -rf "$tmpdir"' EXIT HUP INT TERM

say "downloading $URL"
if ! fetch -fsSL --max-time 300 -o "$tmpdir/$ASSET" "$URL"; then
  if [ -z "$TOKEN" ]; then private_repo_hint; fi
  die "download failed"
fi
say "downloading checksum $SHA_URL"
if ! fetch -fsSL --max-time 60 -o "$tmpdir/$ASSET.sha256" "$SHA_URL"; then
  if [ -z "$TOKEN" ]; then private_repo_hint; fi
  die "checksum download failed"
fi

expect=$(awk '{print $1}' "$tmpdir/$ASSET.sha256" | tr -d '[:space:]')
[ -n "$expect" ] || die "empty checksum from $SHA_URL"
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum "$tmpdir/$ASSET" | awk '{print $1}')
else
  got=$(shasum -a 256 "$tmpdir/$ASSET" | awk '{print $1}')
fi
if [ "$expect" != "$got" ]; then
  die "checksum mismatch for $ASSET (expected $expect, got $got)"
fi
say "checksum ok: $expect"

# --- install --------------------------------------------------------------
mkdir -p "$BINDIR"
install -m 0755 "$tmpdir/$ASSET" "$BINDIR/steward"
say "installed steward $VERSION to $BINDIR/steward"

# --- next command ---------------------------------------------------------
say ""
say "Steward $VERSION is installed. Next:"
case ":$PATH:" in
  *":$BINDIR:"*) : ;;
  *) say "  export PATH=\"$BINDIR:\$PATH\"" ;;
esac
say "  steward doctor"
