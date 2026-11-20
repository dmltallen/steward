# Steward brand

The mark, the palette, and the name check behind them. Findings and rules,
not marketing copy.

## Palette

The identity draws on the existing light web client tokens in
`src/steward/webapp/style.css` (`:root`). The three values that define the
brand:

| Token | Hex | Use |
| --- | --- | --- |
| Brand teal | `#0f766e` | Primary: the icon tile, the accent in the web client. |
| Deep teal | `#115e59` | Depth: gradient end of the tile, accent-strong in the web client. |
| Paper | `#f7f6f3` | Light canvas: the web client background, recommended page background. |

Text and ink use the web client's `--ink`, `#1f2933`, which is a fourth token
outside the three that name the identity. The icon is the rounded square tile
in the teal gradient with a white `S`, matching the shape of the existing
`.brand-mark` in the web client top bar.

## Logo and icons

- `assets/brand/steward-logo.svg` and `.png` (312x64 and 624x128): mark plus
  wordmark, for headers and the landing page.
- `assets/brand/steward-icon.svg` and `.png` (512, 256, 128, 32): the app
  icon set. Light theme first, no photography, no emoji, one glyph.
- The PNG files are the canonical renders, produced from the SVG with Chrome
  at 1x. Regenerate them from `steward-icon.svg`, never hand-edit the PNGs.
- At 32 px the glyph is intentionally a single bold letterform; nothing
  smaller than 32 px is promised.

Rules: keep the palette to the three values above plus white and the ink
text color. No gradients outside the tile. No drop shadows. No new marks for
the same product; variants (dark tile, monochrome) are not produced yet.

## Name and trademark findings

Checked 2026-11-19 (PST) via web search, the USPTO Trademark Search
interface (tmsearch.uspto.gov), and the USPTO TSDR status pages. Findings
follow; the decision is Don's.

1. The exact wordmark is active in both relevant classes. USPTO TSDR
   (generated 2026-11-20 00:00:32 EST) for serial 99089667: "Mark: STEWARD",
   "US Registration Number: 8504487", "Registration Date: Oct. 13, 2026",
   "International Class(es): 042", "For: Software as a service (SAAS)
   services featuring software for the preparation of wills", status
   "LIVE/REGISTRATION/Issued and Active", standard-character mark, owned by
   STEWCO, INC.
2. USPTO TSDR (generated 2026-11-20 00:00:39 EST) for serial 88223032:
   "Mark: STEWARD", "US Registration Number: 6316127", "Registration Date:
   Apr. 06, 2021", Class 009, "For: Downloadable mobile application for
   monitoring plant health and agricultural conditions", status
   "LIVE/REGISTRATION/Issued and Active", standard-character mark, owned by
   STWRD, Inc. This is a live federal registration covering downloadable
   computer software under the exact word.
3. The official search interface (tmsearch.uspto.gov, queried "STEWARD")
   returned "309 results for STEWARD": Live 145, Dead 164. Quoted for the
   exact phrase "STEWARD" it returned "206 results": Live 107, Dead 99. The
   results include live class-042 records containing the word, for example
   MTC-STEWARD (serial 99732353, "IC 042: Data security consultancy",
   LIVE/PENDING).
4. Older and related records, from USPTO mirrors: STEWARD serial 74514098
   (filed 19 Apr 1994, class 009 card terminals) is Dead/Abandoned as of
   23 Feb 1996; STEWARD serial 85126566 / reg 4183816 (filed 10 Sep 2010) for
   Steward Health Care System covers medical education services.

Honest risk statement: the name is crowded. Two active, standard-character
federal registrations of the exact word STEWARD sit in the two classes a
local-AI software product would use, class 9 (registered 2021) and class 42
(registered October 2026), plus a pending class-42 record containing the
word. A new application for STEWARD in computer software or SaaS would
almost certainly draw a likelihood-of-confusion refusal citing the STWRD
registration, and the newer wills-SaaS registration is directly on point for
software services. None of this stops use of the name today, but it is the
difference between a name with a clean lane and a name that invites office
actions, oppositions and demand letters. The recommendation embedded in the
finding is plain: a same-class exact-mark registration is the highest-risk
outcome the check can return. The decision stays with Don.

## Content rules

- English only, in every surface, including alt text and image captions.
- No em dashes anywhere; a hyphen or a comma is the house style.
- No price, no tier name, and no call to action to buy anywhere in the
  public-facing docs or site until the A/B decision; the free-to-install
  sentence is the whole financial claim.
- Light palettes by default.
- Plain language. No vendor-voice superlatives.
