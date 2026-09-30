# PublicDB2 06A Integrated Acceptance

## Revision and scope

- Baseline HEAD: `1d7b9f68fd263c9782540b4bb5f3f8ff716de154`
- 06A revision: this goal commit
- Migration head: `e8a106a06a01`
- Scope: auth/roles/CSRF, Agency/OrgUnit/Duty, Source/SourceBinding/import,
  three-way collection, RAW/extraction/runs, baseline/review, confirmed contacts,
  dashboard/settings/export, restart, interruption, concurrency, scale, and
  project-root relocation.
- Automated collection uses `httpx.MockTransport`; real external HTTP count is
  zero. Acceptance runtime lives only below `data/temp/acceptance/`.

## Recovery and concurrency contracts

- Stale `RUNNING` collection rows are finalized as `FAILED` with an interruption
  reason before a later explicit collection can start. No partial evidence is
  promoted to success.
- A DB claim plus a partial unique run index permits exactly one active
  collection per canonical Source across independent sessions.
- Source method editing shares the Source claim with collection.
- Baseline apply, review action, and import confirm are idempotent across
  duplicate/concurrent submissions. Approve-vs-keep and approve-vs-defer races
  produce at most one confirmed mutation path.
- Import retention, credential writes, RAW storage, and XLSX export use atomic
  finalization and are covered by controlled failure injection.

## Scale and portability result

The deterministic harness created an isolated DB with 500 Agencies, 5,000
OrgUnits, 5,000 canonical Sources, 10,000 SourceBindings, 50,000 confirmed
ContactPoints, 50,000 CrawlRuns, and 10,000 review candidates.

- Workspace page size: 100; maximum supported page size: 500.
- Observed query counts for one default page: agencies 8, sources 3, runs 8,
  contacts 7, review 5. Dashboard used 19 queries and returned recent 6 runs,
  seven trend days, and latest 5 unresolved reviews.
- A 10,000-row exact-duplicate import preview/confirm completed with 10,000
  duplicates, zero duplicate entities, one retained audit file, and idempotent
  repeat confirmation.
- Confirmed-contact XLSX exported all 50,000 rows using write-only streaming;
  formula protection and official/method/actual provenance columns passed.
- `PRAGMA integrity_check` returned `ok`; functional orphan count was zero.
  `foreign_keys=ON`, `busy_timeout=5000`, and `journal_mode=WAL` passed.
- Restart and copied-root relocation preserved the DB, project-relative RAW,
  session secret, credential reference/store, and readiness. Persisted developer
  absolute-path dependency count was zero.
- A real headless Uvicorn process passed `/health`, `/ready`, `/login`, the
  authenticated Dashboard and all seven workspaces, logout, and clean shutdown.

## Commands

```powershell
py -3.13 tools\acceptance\run_06a.py --run-id <new-run-id>
py -3.13 -m pytest -q
```

The harness refuses an existing or out-of-bound runtime root and does not make
real external HTTP requests.

## Acceptance metrics

```text
FULL_SUITE_PASS=PASS (105 passed)
ACCEPTANCE_SCENARIO_PASS=PASS
MIGRATION_HEAD=e8a106a06a01
DB_INTEGRITY=ok
FUNCTIONAL_ORPHAN_COUNT=0
PUBLICDB1_WRITE_COUNT=0
EXTERNAL_REAL_HTTP_COUNT=0
RUNTIME_SECRET_LEAK_COUNT=0
RUNTIME_ABSOLUTE_PATH_DEPENDENCY_COUNT=0
DUPLICATE_CONCURRENT_COLLECTION_COUNT=0
DUPLICATE_BASELINE_ENTITY_COUNT=0
DUPLICATE_REVIEW_MUTATION_COUNT=0
STALE_RUNNING_RECOVERY_PASS=PASS
RESTART_DURABILITY_PASS=PASS
PORTABLE_RELOCATION_PASS=PASS
SCALE_DATASET_PASS=PASS
UNBOUNDED_WORKSPACE_QUERY_COUNT=0
GIT_RUNTIME_FILE_COUNT=0
```
