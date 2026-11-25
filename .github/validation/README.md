# .github/validation — files used by `staging-validation.yml`

Staging repository only. Every file here is pinned by SHA-256 in the workflow; changing one without a reviewed change to
the workflow's `VALIDATION_FILES_SHA256` list stops every job.

| File | Used by | What it is |
| --- | --- | --- |
| `history.pack` | repo-tests | Git objects for 8 old file versions (6 old commits) that four regression suites test against as "mutants": search-authority, upgrade-navigation, premium-profile, validation-units-imp018. 22 objects, about 2.4 MB, no secrets. Imported into the job's own clone only. |
| `history.txt` | repo-tests | `revision commit path blob` for each of the 8 versions; the workflow checks every one resolves to its pinned blob. |
| `site_check.mjs` | site | A1/A2 deployment integrity, A3 live connection, S3 anonymous web-API probes. Publishable key only. |
| `db-counts.sql` | database | Read-only counts and migration history, before and after. |
| `db-audit.sql` | database | Read-only protection audit, run on staging and on a replica built from the migration files. |
| `db-matrix.sql` | database | Two-user access matrix and formula-version checks in ONE transaction that always ends in a deliberate error (rolled back). |
| `db_check.py` | database | Judges the three database outputs; exact comparison, no warning level. |
| `expected.json` | site, database, repo-tests | Every expected result, exactly. |

The old file versions come from the production repository's history as it stood on 2026-11-18 (local copy). They are
public website files from earlier releases (`final-ui-fix.js`, `premium-calculator-gating.js`, `premium/shell.js`, the
F1.12.4 engine); none contains a project address or key. Nothing is fetched from any repository when the workflow runs.
