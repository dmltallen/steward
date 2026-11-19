# Homebrew packaging (dry run)

`steward.rb` is a Homebrew cask that installs the single-file macOS binary as
`steward`.

## Who owns updates

One owner per install. When Steward is installed through this cask, Homebrew
owns the binary, so updates come from Homebrew (`brew upgrade --cask steward`)
and the cask MUST NOT declare `auto_updates true`. Steward's in-app updater
detects the install channel at runtime and refuses to replace a
package-managed binary (it returns the exact command a person runs instead),
so declaring auto_updates would claim the app self-updates when it refuses to,
hiding the cask from `brew outdated`. Manual installs and source checkouts
self-update through Steward's own updater. The rule lives in `steward.rb` and
in docs/RELEASE.md.

## Status

NOT MEASURED. There is no macOS build on this project yet, so the cask is a
validated template: the `sha256` is a placeholder and the download host is the
LAN gitea instance. To publish to a Homebrew tap a maintainer must:

1. build the macOS binary on an arm64 (and optionally an x64) Mac,
2. sign and notarize it with an Apple developer account.
   Notarization is NOT MEASURED and is the standard gate for casks, because
   macOS refuses unsigned installs,
3. host the artifact over public HTTPS,
4. fill in the real `sha256` and `url`,
5. open a PR to the target tap.

Publishing is a manual maintainer step outside this repository and is
NOT MEASURED. Ruby syntax/audit validation is documented as NOT MEASURED in
the main packaging README because no Ruby toolchain is installed on U1.
