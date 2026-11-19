cask "steward" do
  version "0.2.0"

  # NEVER add `auto_updates true` here.
  #
  # Steward ships an in-app updater, but it refuses to run on a package-managed
  # install (install channel detection: dpkg/Homebrew/winget own the binary, so
  # the app does not replace it). For this cask, Homebrew owns updates: users
  # upgrade with `brew upgrade --cask steward`. Declaring auto_updates would
  # tell Homebrew the app self-updates and hide the cask from `brew outdated`
  # while the in-app updater is refusing to run, which strands the install on
  # an old version. One owner per install is the contract (see docs/RELEASE.md).

  # NOT MEASURED: no macOS build exists on this project yet, so this sha256 is
  # a placeholder. Notarization requires an Apple developer account and is a
  # manual step, also NOT MEASURED. Replace this value and the download host
  # with the real signed, notarized binary before publishing to Homebrew.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  url "https://github.com/dmltallen/steward/releases/download/v#{version}/steward-darwin-arm64"
  name "Steward"
  desc "The personal AI you own, served on hardware you already have."
  homepage "https://github.com/dmltallen/steward"

  # Installs the single-file macOS binary as `steward` in the Homebrew bin dir.
  binary "steward-darwin-arm64", target: "steward"

  caveats <<~EOS
    This cask is a template. On the LAN gitea instance the download host is
    not publicly reachable and the binary is not notarized, so `brew install`
    against the real release requires a public HTTPS host plus an Apple
    developer account for notarization. Both are NOT MEASURED.
  EOS
end
