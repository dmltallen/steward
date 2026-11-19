# Homebrew packaging (template only)

`steward.rb` is a Homebrew cask template that would install the single-file
macOS binary as `steward`.

## Status: the tap does not exist

There is no Homebrew tap for Steward, so `brew install steward` fails today.
The cask is a template only. Creating or adopting a tap and adding this cask
to it is a maintainer step outside this repository and is NOT MEASURED.

To publish, a maintainer must:

1. build the macOS binary on the macos-14 leg of `.github/workflows/build.yml`
   (or on a real Apple Silicon Mac),
2. sign and notarize it with an Apple Developer account. Notarization is
   NOT MEASURED and is the standard gate for casks, because macOS refuses
   unsigned installs,
3. host the notarized binary over public HTTPS,
4. fill in `version`, `sha256` and `url` in the cask,
5. create or adopt a tap and add the cask to it.

Ruby syntax and `brew audit` validation are NOT MEASURED: no Ruby or
Homebrew toolchain is installed on the build host (U1), and no tap exists to
audit against.
