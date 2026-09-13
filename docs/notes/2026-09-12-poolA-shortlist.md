# Pool A shortlist — reconnaissance (2026-09-12)

Source: `data/annotations/pool_A_underspecified_valid_tests.csv` (109 rows total).
Eligible repos (pytest-based runners, django/sympy excluded): sphinx-doc/sphinx,
pytest-dev/pytest, matplotlib/matplotlib, scikit-learn/scikit-learn, pydata/xarray,
psf/requests, mwaskom/seaborn, astropy/astropy → **45 eligible instances**.

All 45 eligible rows already have `other_major_issues = 0.0`, so criterion 3
(no other major issues) does not discriminate within this pool — every candidate
below and every candidate considered passes it.

Ranking method: read all 45 `underspecified_notes` values in full. Instances were
ranked highest when the note names a **specific either/or (or A/B/C) decision**
a stakeholder could resolve in one or two sentences (e.g. "which hook
overwrites the other," "should the error be suppressed or the reduction fixed"),
rather than describing general vagueness ("hard to know what a successful
solution looks like," "no code sample provided"). Among similarly concrete
notes, difficulty "15 min - 1 hour" or "1-4 hours" was preferred over
"<15 min fix" (trivial fixes are less likely to need a real stakeholder
back-and-forth). Several strong concrete-decision candidates were passed over
for the top 10 specifically because they were `<15 min fix` (e.g.
`pytest-dev__pytest-5550`, `scikit-learn__scikit-learn-13485`,
`sphinx-doc__sphinx-9231`, `pydata__xarray-5131`) — these are good backups if
the pipeline needs more than 10.

## Top 10

| # | instance_id | repo | difficulty | underspecified score | open decision (from notes) | FAIL_TO_PASS | PASS_TO_PASS |
|---|---|---|---|---|---|---|---|
| 1 | psf__requests-1657 | psf/requests | 15 min - 1 hour | 2.0 | Request-level hooks and session-level hooks are merged by simple dict overwrite, but which one should win is unstated | 3 | 83 |
| 2 | scikit-learn__scikit-learn-15094 | scikit-learn/scikit-learn | 15 min - 1 hour | 2.0 | MaxAbsScaler upcasts pandas float32→float64; unclear whether to add a `dtype` param (like OneHotEncoder), fix it via ColumnTransformer, or just require `df.values` | 1 | 84 |
| 3 | pydata__xarray-6882 | pydata/xarray | 15 min - 1 hour | 2.0 | Opening a NetCDF file with a zero-length time coordinate raises ValueError during CF-datetime decode; unclear whether to suppress with a meaningful message or fix the underlying reduction | 1 | 432 |
| 4 | matplotlib__matplotlib-24189 | matplotlib/matplotlib | 15 min - 1 hour | 2.0 | `width_ratios`/`height_ratios` break with nested `subplot_mosaic()`; issue proposes two alternatives — actually support ratios with nested mosaics, or raise a clearer error explaining the limitation | 2 | 106 |
| 5 | pytest-dev__pytest-9681 | pytest-dev/pytest | 15 min - 1 hour | 2.0 | `conftest.py` in a package breaks `--import-mode=importlib` + `pythonpath` discovery in 7.0; no expected behavior given, several viable fixes (ignore/bypass vs. revert vs. rewrite discovery logic) | 1 | 132 |
| 6 | pydata__xarray-4759 | pydata/xarray | 1-4 hours | 2.0 | `xr.concat` silently upcasts coordinate dtype to `object`; issue links a second related report and doesn't say whether to fix one, the other, or both | 19 | 2234 |
| 7 | psf__requests-1944 | psf/requests | 1-4 hours | 2.0 | A redirect response has a false `Content-Encoding: gzip` header; unclear whether Requests should ignore/skip decoding the body on redirects or handle the decode error some other specific way | 5 | 120 |
| 8 | mwaskom__seaborn-3407 | mwaskom/seaborn | 15 min - 1 hour | 2.0 | `pairplot` raises KeyError on a MultiIndex-column DataFrame; unclear whether this should be treated as a bug to fix directly, or whether flattening the MultiIndex (the reporter's workaround) should happen automatically inside `pairplot` | 1 | 120 |
| 9 | sphinx-doc__sphinx-9246 | sphinx-doc/sphinx | 15 min - 1 hour | 2.0 | `missing-reference` handling raises `NotImplementedError: Unknown node: pending_xref_condition`; unclear whether `contnode` should be resolved when the `missing-reference` event fires in `post_transforms/__init__.py` | 1 | 2 |
| 10 | scikit-learn__scikit-learn-12421 | scikit-learn/scikit-learn | 15 min - 1 hour | 2.0 | OPTICS `core_distances_` returns finite values for non-core points, contradicting docs (which promise `inf`) and R's implementation; open question is whether to fix the implementation to match the docs or fix the docs to match the implementation | 2 | 24 |

Hidden-test sanity check (from the cached HF `princeton-nlp/SWE-bench` test split):
every one of the top 10 has a `test_patch` that only touches ordinary `.py` test
files (or, for sphinx-9246, two Sphinx test-root fixture files plus a
`test_transforms_post_transforms.py`) under each repo's standard test tree
(`test_requests.py`, `lib/matplotlib/tests/test_figure.py`,
`sklearn/.../tests/test_*.py`, `xarray/tests/test_*.py`,
`tests/test_axisgrid.py`, `testing/test_collection.py` +
`testing/test_pathlib.py`, `tests/test_transforms_post_transforms.py`). All are
plausible to collect and run with a plain `pytest`/`pytest -k` invocation — no
non-pytest runners or unusual test discovery involved.

## Why a stakeholder conversation matters (top 5)

**1. psf__requests-1657 — hook merge order.** The bug report itself states the
mechanism (two hook dicts, one overwrites the other) but never says which
should win: request-specific hooks overriding session defaults, or vice versa.
Both are defensible API designs and the "right" answer is a product decision,
not something derivable from the code. A one-line stakeholder answer ("request
hooks should extend, not replace, session hooks — per-event") fully
disambiguates the fix and gives an agent something concrete to implement
against, rather than guessing at API semantics.

**2. scikit-learn__scikit-learn-15094 — where the dtype fix belongs.** The
issue names three plausible integration points (a new `dtype` parameter on
`MaxAbsScaler` mirroring `OneHotEncoder`, a `ColumnTransformer`-level fix, or
just telling users to pass `.values`). Each implies a materially different
patch surface and touches different tests. This is exactly the kind of
"pick-an-approach" question a maintainer would resolve in a sentence during
review, making it a strong simulated-stakeholder scenario.

**3. pydata__xarray-6882 — silence vs. fix.** Whether the zero-length
time-coordinate case should raise a clear, documented error (i.e., "this isn't
supported, tell the user why") or actually be made to work is a genuine product
tradeoff between correctness/robustness and effort. The notes explicitly flag
that reasonable people could go either way, so a short stakeholder directive
determines which of two very different diffs is "correct."

**4. matplotlib__matplotlib-24189 — feature vs. better error.** The original
reporter offers both options themselves (support ratios on nested mosaics, or
raise a clear error). This is a scope decision more than a bug fix: the "fix
the bug" interpretation requires nontrivial new layout logic, while the
"clarify the error" interpretation is a much smaller patch. A stakeholder
choosing between these materially changes both effort and the resulting test
assertions.

**5. pytest-dev__pytest-9681 — how defensively to fix a regression.** The
9.0-vs-7.0 regression could be addressed by reverting the behavioral change,
adding a bypass/ignore path for this discovery case, or reworking the
`importlib` + `pythonpath` discovery logic more generally. These have very
different blast radii (narrow patch vs. broader refactor), so a stakeholder's
one-sentence risk tolerance ("just fix this reported case, don't refactor
discovery") is exactly the kind of steering an automated pipeline should be
testing an agent's ability to solicit and follow.
