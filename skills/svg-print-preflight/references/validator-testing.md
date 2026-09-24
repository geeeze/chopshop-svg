# Testing an SVG validator

## Suite shape

Drive the real CLI entry point, not internal functions:

```python
def run(capsys, svg_path, spec_path):
    code = main([svg_path, spec_path])
    return code, capsys.readouterr().out
```

Assert on the contract, in this order: exit code, the `VALIDATION FAILED for
<path>:` header, then the exact rule tag `[RULE_TAG]`, then the numbers in the
message. A suite that only checks the exit code passes while every message
becomes useless.

Cover: one passing document, one case per named rule, malformed XML, missing
files, an invalid spec, and a multi-failure document asserting that **all** tags
are reported (a validator that stops at the first failure is a workflow bug).

## Test-side traps that look like script bugs

- **Units.** `stroke-width="1.5"` is 1.5**px** = 1.125pt. A "thin stroke"
  fixture written as `1.5` and expected to pass a 1.5pt minimum is testing the
  wrong thing; write `1.5pt` when you mean points.
- **Colour expansion.** `#abc` → `#aabbcc`, so a dedupe case must use values
  that are genuinely equal (`#abc`, `#ABC`, `#aabbcc`, `rgb(170,187,204)`), not
  `#abcdef`.
- **Open paths in stroke fixtures.** A fixture with `d="M0,0 L10,10"` also
  trips the open-path rule; close it (`... Z`) when the case is about stroke
  width, or the assertion fails for an unrelated reason.

## Thresholds need both directions

Parametrise the unit table and assert both sides of every limit:

- values at or just above the minimum **pass** (`1.5pt`, `2px`, `1mm`, `0.1cm`)
- values just below **fail**, each asserting its converted figure
  (`0.5`, `0.5px`, `0.1mm`, `0.005in`)
- the boundary itself is inclusive: minimum 1.5pt accepts exactly 1.5pt

A one-sided test cannot tell "correct comparison" from "always fails".

## Fixture helpers

Generate documents through a helper that injects the SVG namespace (and `xlink`
when rasters are tested) so no case forgets it, and write fixtures under the
pytest `tmp_path` — a validator that resolves paths relative to CWD will pass
locally and fail in a pipeline otherwise.

## Derived defaults are tested by ABSENCE

A helper that writes a *complete* base config makes derivation untestable: the
code sees the key present, correctly skips the derivation, and the assertion
fails for a reason that has nothing to do with the rule. To test a derived
default, delete the key in that case's mutator:

```python
def mutate(s):
    s["print"]["dark_garment_underbase"] = True
    s["print"].pop("count_white_as_ink", None)   # absent, not False
```

`False` and "missing" are different inputs wherever one key's value is derived
from another's presence.

## Every rejection needs a control

A rejection proves nothing alone: the fixture may be tripping an earlier rule, or
failing to parse. Pair each negative case with a control — the same document
against a permissive spec, or with the single offending property removed — and
assert the exit code flips. Fixtures should be well-formed and otherwise clean,
with geometry chosen so the asserted numbers are meaningful (1 user unit = 1 px
when the case is about units). Check assertions against the real constant rather
than a retyped string: an expected rule tag spelled by hand fails on a rename for
no useful reason.
