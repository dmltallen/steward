# Homebrew cask template for Steward, TEMPLATE ONLY.
#
# NOT MEASURED: Steward has no macOS build on this project yet, so the sha256
# is a placeholder and the url is not downloadable. The macos arm64 leg of
# .github/workflows/build.yml produces an unsigned steward-darwin-arm64; code
# signing and notarization need an Apple Developer account and are a manual,
# separate step that is also NOT MEASURED here. macOS refuses unsigned
# installs, so notarization is the gate before this cask can ever be real.
cask "steward" do
  version "1.2.0" # shipper: set to the released version

  # shipper: replace with the real sha256 of the signed and notarized binary.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  # shipper: replace with a public HTTPS URL for the notarized binary.
  url "https://example.invalid/steward-darwin-arm64"
  name "Steward"
  desc "The personal AI you own, served on hardware you already have."
  homepage "https://github.com/dmltallen/steward"

  # The frozen console binary bundles its own Python and has no brew formula
  # dependencies; depends_on only pins a floor macOS version. The real minimum
  # must be re-checked against an actual build, which is NOT MEASURED.
  depends_on macos: ">= :monterey"

  # Installs the single-file macOS binary as `steward` in the Homebrew bin dir.
  binary "steward-darwin-arm64", target: "steward"

  caveats <<~EOS
    This cask is a template. No Homebrew tap for Steward exists yet, so
    `brew install steward` fails today. A maintainer must create or adopt a
    tap, then fill the version, sha256 and url above with a signed and
    notarized binary hosted over public HTTPS. Notarization is NOT MEASURED.
  EOS
end
