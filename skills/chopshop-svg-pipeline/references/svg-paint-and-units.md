# Paint, colour budget and units

## Normalising a colour to `#rrggbb`

| Input | Normalised | Counts as a colour? |
| --- | --- | --- |
| `#FFF`, `#fff` | `#ffffff` | yes |
| `#abc` | `#aabbcc` (each digit doubled) | yes |
| `#abcd` (4-digit) | double each → `#aabbcc`, alpha `dd` | yes unless alpha `00` |
| `#aabbccdd` (8-digit) | drop the alpha pair | yes unless alpha `00` |
| `rgb(255,0,0)`, `rgb(100%,0%,0%)` | `#ff0000` | yes |
| `rgba(255,0,0,0)` | — | no (fully transparent) |
| `hsl(0,100%,50%)` | `#ff0000` (via `colorsys.hls_to_rgb`) | yes |
| `red`, `rebeccapurple` | lookup table | yes |
| `none`, `transparent`, `currentColor`, `inherit`, `initial`, `unset` | — | no |
| `url(#grad)`, `var(--x)` | — | no (resolve the gradient's `<stop>`s separately) |
| an unknown keyword | kept lowercased as its own key | yes — never drop it, or the budget under-counts |

`#abc` expands to `#aabbcc`, **not** `#abcdef`. Getting this wrong makes a
dedupe test that should pass fail, and vice versa; it is the classic test-side
bug in this domain.

Count the union of `fill`, `stroke` and `stop-color` — stopping at `fill`
misses every outlined and gradient-filled file. Report the count against the
limit and list the colours found, so the user can see which one to drop.

## Length units

`1in = 96px = 72pt = 25.4mm = 2.54cm`. Hence:

| Unit | → pt |
| --- | --- |
| `px` (and unitless) | × 0.75 |
| `pt` | × 1 |
| `mm` | × 2.834645669 |
| `cm` | × 28.34645669 |
| `in` | × 72 |
| `pc` | × 12 |
| `Q` | × 0.708661417 |
| `%`, `em`, `ex`, `rem`, `ch` | not resolvable without the viewport/font cascade |

Accept a leading `+`/`-`, decimals without a leading zero (`.5`), and scientific
notation. Treat the unitless form as `px` per the SVG spec and say so in the
message; the alternative (assuming `pt`) silently redefines every threshold in
the file.

## Cascade resolution order

1. `style="..."` on the element itself (highest)
2. `<style>` rules matching the element — by specificity `(ids, classes, tags)`,
   later declaration wins on a tie
3. the presentation attribute

Query the same resolver for every property, but note where inheritance differs:
`stroke` and `stroke-width` are inherited properties and must be carried down
the tree, whereas `fill` on a parent does apply too — so a scope walk is the
safer default for all three.

## At-rules in `<style>`

Use a brace-counting scan, not a regex: strip comments, drop
`@charset`/`@import`/`@namespace`, inline the bodies of `@media print` and
`@media all` (a print validator *is* the print medium), and drop everything else
including `@media screen`. A regex `\{[^{}]*\}` stops at the first nested `}`
and leaves the tail of the block in the stylesheet, where it is then parsed as
ordinary rules.

`!important` is not modelled by a simple resolver — either implement it or say
so; silently ignoring it means a file can pass that a browser would fail.
