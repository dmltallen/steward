# Steward build and packaging.
#
#   make build-linux   one-file console binary for the native host arch
#   make build-deb     .deb package (nfpm when available, else dpkg-deb)
#   make build-win     documented only, see target comment
#   make clean         remove build output
#
# Builds in a local venv (.venv, gitignored) so nothing pollutes the repo or
# host site-packages. Version and arch are derived from pyproject.toml and
# uname so the same Makefile produces linux/arm64 or linux/amd64 on matching
# hosts. PyInstaller cannot cross-compile, so amd64 and arm64 each need
# matching build hardware (or a runner for that arch).

VENV        := .venv
PYTHON      := $(VENV)/bin/python
PYINSTALLER := $(VENV)/bin/pyinstaller
NFPM_VENV   := $(VENV)/bin/nfpm
NFPM_SYS    := $(shell command -v nfpm 2>/dev/null)

VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
UNAME_M := $(shell uname -m)
ifeq ($(UNAME_M),aarch64)
ARCH := arm64
else ifeq ($(UNAME_M),arm64)
ARCH := arm64
else ifeq ($(UNAME_M),x86_64)
ARCH := amd64
else
ARCH := $(UNAME_M)
endif

.PHONY: build-linux build-deb deb-dpkg build-win clean venv-nfpm publish-assets

# --- environment ----------------------------------------------------------
# Venv bootstrap. Debian's python3 -m venv can produce a venv without pip
# (ensurepip is a separate package), which failed a fresh build here; uv does
# the same job without that dependency, so prefer it when present.
UV := $(shell command -v uv 2>/dev/null)
$(PYINSTALLER):
	@if [ -n "$(UV)" ]; then \
		echo "==> creating $(VENV) with uv"; \
		$(UV) venv --quiet $(VENV); \
		$(UV) pip install --quiet --python $(PYTHON) pyinstaller cryptography; \
	else \
		echo "==> creating $(VENV) with python3 -m venv"; \
		python3 -m venv $(VENV); \
		$(PYTHON) -m pip install --quiet --upgrade pip; \
		$(PYTHON) -m pip install --quiet pyinstaller cryptography; \
	fi

$(NFPM_VENV):
	@mkdir -p $(VENV)/bin
	@echo "==> fetching nfpm into $(VENV)/bin"
	@curl -fsSL -o /tmp/nfpm-$(ARCH).tar.gz \
		https://github.com/goreleaser/nfpm/releases/download/v2.45.0/nfpm_2.45.0_linux_$(ARCH).tar.gz
	@tar -xzf /tmp/nfpm-$(ARCH).tar.gz -C $(VENV)/bin nfpm

venv-nfpm: $(NFPM_VENV)
	@$(NFPM_VENV) --version | tail -1

# --- Linux one-file binary ------------------------------------------------
build-linux: $(PYINSTALLER)
	rm -rf dist build
	$(PYINSTALLER) --noconfirm --clean --distpath dist --workpath build \
		packaging/pyinstaller.spec
	@echo "==> built:"
	@ls -l dist/steward
	@echo "==> version from the frozen binary:"
	@./dist/steward version

# --- Debian package -------------------------------------------------------
build-deb: build-linux
	@if [ -n "$(NFPM_SYS)" ]; then \
		echo "==> using system nfpm: $(NFPM_SYS)"; \
		STEWARD_ARCH=$(ARCH) STEWARD_VERSION=$(VERSION) $(NFPM_SYS) pkg \
			--config packaging/nfpm.yaml --packager deb --target dist/; \
	elif [ -x "$(NFPM_VENV)" ]; then \
		echo "==> using venv nfpm: $(NFPM_VENV)"; \
		STEWARD_ARCH=$(ARCH) STEWARD_VERSION=$(VERSION) $(NFPM_VENV) pkg \
			--config packaging/nfpm.yaml --packager deb --target dist/; \
	else \
		echo "==> nfpm not found, falling back to dpkg-deb"; \
		$(MAKE) deb-dpkg; \
	fi
	@ls -1 dist/*.deb

# dpkg-deb fallback with a hand-rolled control dir, mirroring packaging/nfpm.yaml.
deb-dpkg: build-linux
	@echo "==> building .deb with dpkg-deb (nfpm unavailable)"
	@rm -rf build/deb
	@mkdir -p build/deb/steward_$(VERSION)_$(ARCH)/usr/bin
	@mkdir -p build/deb/steward_$(VERSION)_$(ARCH)/usr/lib/systemd/user
	@cp dist/steward build/deb/steward_$(VERSION)_$(ARCH)/usr/bin/steward
	@cp packaging/systemd/steward.service \
		build/deb/steward_$(VERSION)_$(ARCH)/usr/lib/systemd/user/steward.service
	@mkdir -p build/deb/steward_$(VERSION)_$(ARCH)/DEBIAN
	@printf '%s\n' \
		"Package: steward" \
		"Version: $(VERSION)" \
		"Section: utils" \
		"Priority: optional" \
		"Architecture: $(ARCH)" \
		"Maintainer: Steward Project (don/steward)" \
		"Description: Steward, the personal AI you own, served on hardware you already have." \
		> build/deb/steward_$(VERSION)_$(ARCH)/DEBIAN/control
	@dpkg-deb --build --root-owner-group \
		build/deb/steward_$(VERSION)_$(ARCH) dist/ >/dev/null

# --- Windows (documented, runs in CI only) --------------------------------
# PyInstaller does not cross-compile, so the .exe must be built on a Windows
# host or a windows-amd64 runner. u1-runner is linux/arm64 and the build
# workflow never queues a windows-amd64 job, so this target only documents the
# intent here and in .gitea/workflows/build.yaml.
build-win:
	@echo "==> build-win is documented for CI only."
	@echo "    PyInstaller cannot cross-compile; build the .exe on a"
	@echo "    Windows host or a windows-amd64 runner, then run:"
	@echo "        pyinstaller --noconfirm --onefile --console --distpath dist"
	@echo "            --workpath build packaging/pyinstaller.spec"
	@echo "    The winget manifest lives at packaging/winget/ and is a"
	@echo "    validated template; publishing needs a Microsoft account"
	@echo "    and is NOT MEASURED."

# --- cleanup ---------------------------------------------------------------
clean:
	rm -rf dist build
