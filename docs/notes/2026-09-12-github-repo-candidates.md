# GitHub repo candidate recon — 2026-09-12

Filter recap: merged PR since 2026-06-01; title/body references a real issue in-repo
(#N, Fixes/Closes #N, or full URL); PR diff has 20-1500 additions in non-test source
files, plus >=1 changed test file; combined thread (PR issue-comments + PR review
comments + linked issue's comments) >= 4; >=2 changed source files preferred.

Excluded from "source" accounting: tests/, test_*, *_test.py, docs/, *.md, *.rst,
resources/, fixtures/.

## py-pdf/pypdf

Search: `is:pr is:merged merged:>=2026-06-01` -> 188 merged PRs since cutoff.
Of these, 68 have at least one `#N`/issue-URL reference in title+body (rest are
plain merges, dependency bumps referencing dozens of old issues in changelog text,
or PRs with no issue reference at all). Checked the top 11 by comment volume in
detail (files + review_comments + linked-issue comments).

**pypdf clears the bar (6/11 checked candidates pass) — stopping here, no fallback
repos needed.**

### Passing candidates

| PR | Issue | Title | Source additions | Source files | Test files | Thread size | Merged at |
|----|-------|-------|------:|------:|------:|------:|-----------|
| 3738 | 3737 | ENH: Check whether image is displayed on a given page | 136 | 3 | 2 | 152 (31 PR comments + 109 review + 12 issue) | 2026-06-18 |
| 3831 | 3514 | ENH: AppearanceStream: Add support for RTL appearance streams with Arabic characters | 67 | 6 | 3 | 70 (7 + 33 + 30) | 2026-06-25 |
| 3905 | 3318 | ENH: AppearanceStream: Consider more encodings for Type1 core fonts | 169 | 6 | 3 | 47 (4 + 40 + 3) | 2026-08-12 |
| 3853 | 3398 | ROB: Improve RC4 fallback when cryptography can't provide it | 61 | 1 | 1 | 23 (5 + 7 + 11) | 2026-08-20 |
| 3929 | 3924 | BUG: Decode low-bit DeviceRGB images as RGB instead of palette | 33 | 1 | 1 | 10 (6 + 1 + 3) | 2026-08-04 |
| 3969 | 3963 | DEV: Detect minor version bumps in make_release.py | 32 | 1 | 1 | 5 (4 + 1 + 0) | 2026-08-19 |

Notes:
- 3738's "source files" includes a 1-line `sample-files` submodule-pointer bump and
  a 1-line `pyproject.toml` change alongside the real `pypdf/_page.py` edit; even
  excluding those, `_page.py` alone (134 additions) is within range.
- 3853, 3929, 3969 have only 1 non-test source file each (fails the ">=2 files"
  preference, not required per spec — recorded, not excluded).

### Reject histogram (11 candidates checked in detail)

- Linked reference is actually a PR, not a real issue: 1 (PR 4023 -> "issue" 4018 is a PR)
- Non-test source additions < 20 (too small a diff): 4 (PR 3841: 1 line; PR 4062: 2 lines; PR 3954: 12 lines; PR 3819: 8 lines)
- Passed all checks: 6

(57 remaining referenced-issue candidates and 120 no-reference PRs were not checked
in detail; budget was stopped once pypdf cleared the >=2-candidate bar with margin.)

## Fallback repos

Not checked — pypdf yielded 6 passing candidates (>= 2), so per instructions the
fallback list (fgmacedo/python-statemachine, celery/kombu, graphql-python/gql,
narwhals-dev/narwhals, Textualize/textual, pytest-dev/pytest) was not consulted.
