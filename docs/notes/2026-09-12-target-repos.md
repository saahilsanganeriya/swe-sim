# Target repositories for the github-repo source (measured 2026-09-12)

Sample: up to 100 merged PRs per repo since 2025-09-01 across the 12 SWE-bench repos and the 91 DeepSWE upstream repos (103 repos, 6458 PRs), one GraphQL query per repo (`closingIssuesReferences`, comments, review threads, labels). Raw data: `data/repo_pr_sizes_2025-09_to_2026-09.json`.

## Size distribution

| statistic | additions |
|---|---|
| p50 | 28 |
| p75 | 122 |
| p90 | 450 |
| p99 | 5264 |
| max | 233622 |

PRs with 5,000+ additions: 71 of 6458. The ten largest are an alternation engine, JSDoc across a package, git processing for a line counter, a release-candidate branch, a docs hero revamp, MCP support, a user-guide intro, two Effect integrations, and a regex-inference prerelease: migrations, generated code, docs, or release rollups. None is a single negotiated feature with a contract.

Feature band (200 to 1,500 additions, 3+ files, linked issue, thread >= 8 comments): **108 candidates**. Bugfix band (20 to 300 additions, linked issue, thread >= 4): **381 candidates**.

## Repos ranked by feature-band yield (Python marked)

| repo | merged sampled | feature cands | bugfix cands | Python |
|---|---|---|---|---|
| dahlia/optique | 100 | 40 | 13 |  |
| numba/numba | 100 | 6 | 17 | yes |
| getarcaneapp/arcane | 100 | 5 | 13 |  |
| meriyah/meriyah | 97 | 5 | 13 |  |
| bombshell-dev/clack | 100 | 3 | 13 |  |
| fgmacedo/python-statemachine | 57 | 3 | 9 | yes |
| goreleaser/goreleaser | 100 | 3 | 7 |  |
| vadimdemedes/ink | 88 | 3 | 7 |  |
| kgateway-dev/kgateway | 100 | 3 | 2 |  |
| KaTeX/KaTeX | 100 | 2 | 19 |  |
| pytest-dev/pytest | 100 | 2 | 14 | yes |
| narwhals-dev/narwhals | 100 | 2 | 11 | yes |
| celery/kombu | 100 | 2 | 9 | yes |
| skrub-data/skrub | 100 | 2 | 8 | yes |
| vitest-dev/vitest | 100 | 2 | 8 |  |
| arktypeio/arktype | 43 | 2 | 4 |  |
| TomWright/dasel | 40 | 2 | 3 |  |
| wazero/wazero | 60 | 2 | 3 |  |
| wasmi-labs/wasmi | 100 | 2 | 2 |  |
| graphql-python/gql | 31 | 2 | 1 | yes |
| pylint-dev/pylint | 100 | 1 | 17 | yes |
| sympy/sympy | 100 | 1 | 17 | yes |
| pydata/xarray | 100 | 1 | 11 | yes |
| astropy/astropy | 100 | 1 | 9 | yes |
| boa-dev/boa | 100 | 1 | 9 |  |
| sphinx-doc/sphinx | 100 | 1 | 8 | yes |
| Gallopsled/pwntools | 96 | 1 | 5 | yes |
| Textualize/textual | 100 | 1 | 5 | yes |
| open-policy-agent/opa | 100 | 1 | 4 |  |
| jeffijoe/awilix | 12 | 1 | 3 |  |
| pallets/flask | 20 | 1 | 3 | yes |
| golang/geo | 68 | 1 | 2 |  |
| kysely-org/kysely | 100 | 1 | 1 |  |
| simonw/sqlite-utils | 49 | 1 | 1 |  |
| drizzle-team/drizzle-orm | 100 | 1 | 0 |  |
| platers/obsidian-linter | 62 | 0 | 11 |  |
| python-attrs/cattrs | 52 | 0 | 9 | yes |
| scikit-learn/scikit-learn | 100 | 0 | 8 | yes |
| ipython/ipython | 100 | 0 | 7 | yes |
| open-circle/valibot | 100 | 0 | 7 |  |

## Trial target list (Python, pytest, installable without system services)

In priority order for the `feature` kind: fgmacedo/python-statemachine, celery/kombu, graphql-python/gql, narwhals-dev/narwhals, pytest-dev/pytest, Textualize/textual, py-pdf/pypdf (demo repo, measured separately: 1 feature candidate, 13 bugfix candidates in its last 100). Excluded despite yield: numba and astropy (compiled extensions, long builds), skrub (heavy scientific stack), dahlia/optique (TypeScript).
