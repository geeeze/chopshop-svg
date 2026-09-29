# Paint, colour budget and units

Everything here is enforced by `validate_svg.py` (Layer A). The rule names in
brackets are the literals that appear in the CLI output and in the manifest.

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
| `none`, `transparent`, `inherit`, `initial`, `unset`, `auto`, `''` | — | no |
| `currentColor` | resolves to the inherited `color`, or `#000000` (initial) | **yes** |
| `url(#grad)` | — | **the reference, not a colour** — see below |
| `var(--x)` | — | no (not resolved; not a paint server) |
| an unknown keyword | kept lowercased as its own key | yes — never drop it, or the budget under-counts |

`#abc` expands to `#aabbcc`, **not** `#abcdef`. Getting this wrong makes a
dedupe test that should pass fail, and vice versa; it is the classic test-side
bug in this domain.

Count the union of `fill`, `stroke` and `stop-color` — stopping at `fill`
misses every outlined and gradient-filled file. Report the count against the
limit and list the colours found, so the user can see which one to drop.

### Count declarations, not final pixels

Three shapes of "no colour in the attribute" are still ink, and a budget that
skips them reads smaller than the file really is — on a black-fill artwork it
read as *no ink at all*:

- **The implicit black fill.** A shape whose cascade never sets `fill` is
  painted black (the SVG initial value), so `#000000` is counted for it.
  Skipped when the fill in force is `none`/`transparent` or its fill-opacity is
  zero. `stats["implicit_fill_elements"]` counts them.
- **`currentColor`.** Resolved from the inherited `color` property — the
  initial value, black, when nothing in scope declares `color`. One renderer
  family this was measured against paints it exactly that way.
  `stats["currentcolor_elements"]` counts them. `fill="currentColor"` is a
  colour, not a reason to skip one.
- **Gradient `<stop>` declarations.** Counted whether or not anything
  references the gradient — that declaration is what `snap_colors` rewrites and
  what the palette check has to see. This is a deliberate over-report, and a
  colour alone can never *under*-report the budget.

A colour a later declaration overrides still occupies a slot: the budget counts
declarations, not what survives the cascade.

Paint that cannot ink is excluded: `display:none`, `opacity:0`,
`visibility:hidden` (honoured per element, so a descendant that sets
`visibility:visible` paints again) and content inside `<clipPath>`, `<filter>`,
`<style>`, `<metadata>`, `<title>`, `<desc>`, `<script>`, `<view>`, `<cursor>`.
`stats["hidden_elements"]` counts the hidden ones. The same exclusion applies to
the stroke-width check — a `stroke-width="0"` inside a `display:none` subtree is
not a hairline, it is nothing, and reporting it was a false failure.
`stroke-opacity="0"` likewise lays no ink.

Content inside `<defs>`, `<symbol>`, `<pattern>`, `<mask>` or `<marker>` is
checked **only when something references it** (`<use href="#id">` or a
`url(#id)`). When nothing does, it cannot print, so it is not failed or
counted; it is reported once as `UNCHECKED_DEFINITION` with a count, because
coverage of the file is then partial.

## Paint-server references (`url(...)`)

A `url(...)` in one of the paint properties — `fill`, `stroke`, `stop-color`,
`flood-color`, `lighting-color` — is a **paint-server reference**. It is
reported by name and never resolved to a colour:

- **`geometry.allow_gradients: false`** (the default): a declared
  `<linearGradient>`/`<radialGradient>` OR any paint-server reference is a
  **hard Layer A failure, `[GRADIENT_NOT_ALLOWED]`**. Both halves are reported
  separately: the gradient *elements* the document declares, and the sites that
  *paint* with one. A `url(...)` that resolves to nothing in this document, or
  to a `<pattern>`, or to an external/`data:` paint server is reported too —
  "this paints with something I cannot read" is never a silent pass. A
  presentation attribute, an inline `style`, and a rule in a `<style>` block
  are all equally declaration sites.
- **`geometry.allow_gradients: true`**: nothing here fails (CMYK process work
  is allowed to carry tone) and the `<stop>` colours are counted as above.

**This used to be a Layer B only concern and that was the bug.** The key was
parsed and then read by nothing, so a two-stop gradient with
`fill="url(#g)"` passed Layer A with zero findings — and `--layer-a-only`, or a
spec with `validation.run_preflight: false`, let continuous tone through the
gate that claims to enforce the spec. Only Layer B's rendered ink count caught
it. The source-level gate now enforces what the spec says; Layer B still
supplies the *evidence* (5 declared colours → 115 rendered, 33% of the sheet).

A `url(...)` paint with a CSS fallback colour counts as the reference, not as
the fallback.

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

Resolve the `pt` figure against the document's REAL user-unit scale
(`mm_per_unit = width_mm / viewBox_width`), not against an assumed 96dpi: a
`width="210mm" viewBox="0 0 595 842"` makes `stroke-width="1"` print at
1.0005pt while a naive px assumption says 0.75pt. A `%`, `em`, negative or
unparseable width is reported with the reason — never passed.

## Cascade resolution order

For every property, highest wins:

1. a declaration marked `!important` in the element's own `style` attribute
2. a declaration marked `!important` in a matching `<style>` rule — by
   specificity `(ids, classes, tags)`, later wins on a tie
3. an ordinary declaration in the element's `style` attribute
4. an ordinary `<style>` rule — by specificity, later wins on a tie, and it
   out-ranks a presentation attribute of any specificity
5. the presentation attribute

`!important` **is ranked**, and it is carried through the parse rather than
stripped: stripping it made the inline declaration win every time, so
`rect{fill:#f00 !important}` lost to `style="fill:#00f"`. The full CSS origin
ladder (user-agent / user / author) is deliberately not modelled — nothing in
this pipeline emits those origins.

Query the same resolver for every property, but note where inheritance differs:
`stroke` and `stroke-width` are inherited properties and must be carried down
the tree, whereas `fill` on a parent does apply too — so a scope walk is the
safer default for all three. Keep every threaded state slot one shape: a walker
that carries `(width, source)` in one place and a bare string in another fails
later inside string formatting with a message that points nowhere near the
walker.

## At-rules in `<style>`

Use a brace-counting scan, not a regex: strip comments, drop
`@charset`/`@import`/`@namespace`, inline the bodies of `@media print` and
`@media all` (a print validator *is* the print medium), and drop everything else
including `@media screen`. A regex `\{[^{}]*\}` stops at the first nested `}`
and leaves the tail of the block in the stylesheet, where it is then parsed as
ordinary rules — which applied `@media screen` rules to a print check.

Match descendant selectors right-to-left and *skip* what you do not model
(`>`, `+`, `~`, `[attr]`, `:` pseudo) rather than half-applying it. Index rules
by property and memoise per `(element, property)`: nested matching is
O(rules × elements × chain depth).

## Advisories: notes, never failures

`EFFECT_REFERENCE` (a `filter`/`mask`/`clip-path` reference),
`TRANSLUCENT_PAINT` (0 < opacity < 1) and `UNCHECKED_DEFINITION` are written to
the report's **notes**, never to its `failures`. That separation is load-bearing:
`validate_svg.main` exits 1 on *any* entry in `failures`, and
`preflight.classify()` fails **SAFE TO HARD** for a rule tag it does not
know — so an advisory routed through `failures` silently becomes a hard gate in
Layer B. Layer A cannot measure what these describe (a filter's blur or flood, a
tint's effect on the rendered ink count), so the honest report is a named note
and an advisory count, with Layer B's rendered proof as the measurement.
