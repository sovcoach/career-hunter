# Architecture

**In plain language:** career-hunter has two halves that never mix.

- **The code** lives in this git repository and contains nothing personal.
- **Your data** lives in one folder on an encrypted external drive: the databases, your real settings, saved copies of job ads, and exported reports.

Inside that folder, the data itself has two layers that never mix either. `search.db` holds everything career-hunter learns from: postings, fit ratings, skip reasons and the like. Each EI claim gets its own small record file, holding only what Service Canada may ask for. The two are linked by a random ID that means nothing on its own.

A small "storage guard" runs before every command and checks that the data folder is somewhere safe. Secrets, such as the future Telegram bot token, sit in the macOS Keychain, separate from both halves.

---

## Components

| Module | Responsibility |
|---|---|
| `cli.py` | `careerhunter` entry point. Runs the storage guard, then dispatches the command. Exit codes: 0 ok, 1 error, 2 refused by the guard. |
| `storage_guard.py` | Fail-closed checks on `CAREERHUNTER_DATA_DIR` (see below). Also creates the data folder safely. |
| `platform_checks.py` | `SystemProbe`: read-only wrappers around `diskutil info -plist`, `fdesetup status`, `git rev-parse` and `os.stat`. Tests swap in a fake. |
| `secrets_store.py` | Keychain access through `keyring`. Allowlisted names only, plaintext backends refused, and values never printed. |
| `config.py` | Validates `config.yaml` and `exclusions.yaml` and reports every problem at once. |
| `db.py` | SQLite connections (WAL, `busy_timeout=5000`, foreign keys on; or read-only), the migration runner with one track per kind of file, and a transaction helper. |
| `migrations/search/`, `migrations/ei_record/` | Schema history, one track per kind of file. Each migration is applied once, and its checksum is recorded so edits are detected. The runner in `db.py` also creates `schema_migrations` and `db_identity`, and their four no-update and no-delete triggers. A fresh `search.db` therefore has 6 triggers (2 from its `0001_init.sql`), and a fresh EI record has 32 (28 from its `0001_init.sql`). |
| `models.py` | Plain record values: `Posting`, `Application`, `Activity`, `Dating`. |
| `claims.py` | The `ei` config section, the per-precision claim check, creating each claim's file, the refusal to write while an earlier claim is unsealed, and sealing. |
| `ledger.py` | Writes decisions to both layers in a fixed order, builds the sanitized EI copy, and reconciles the two layers. |
| `ei_store.py` | Reads and writes on one EI record file. It imports no search code and opens nothing itself. |
| `search_store.py` | Reads and writes on `search.db`, including the posting identity rules. |
| `seed.py` | Validates `seed.yaml`, runs the claim check, then imports each record into its layer. Idempotent by natural keys. |
| `export.py` | Builds one claim's EI record for a date range as CSV and PDF, from that claim's file alone. |
| `bot.py` | The Telegram bot: the chat allowlist, commands, buttons and replies, writing through the ledger; fail-closed startup and the polling loop. |
| `telegram.py` | A minimal Bot API client (standard library plus `truststore`) whose errors never carry the token. |
| `fetch.py` | Fetches a link safely: public addresses only, pinned to the checked address on every redirect hop. Keeps a `Retry-After` header when the server sends one. |
| `filters.py` | The decision to keep, skip or flag a posting: exclusions, hard gates, location rules, role filters and schedule limits. A skip reason is a string. No fetching, scoring or alerts. |
| `score.py` | Scores one kept posting on a local loopback model. The prompt is lanes, location rules, hard gates and `gap_tiers`. Stores gaps, model text and the tier name. Leaves `fit_rating` null. Not `fetch.py`. |
| `watch.py` | One run of `careerhunter watch`: fetch Greenhouse, Lever and Ashby, filter, then score. Writes `search.db` only. No alerts or other ATS types. |
| `snapshots.py` | Job-ad snapshots: the page, an ATS's public JSON, or a PDF or screenshot sent to the bot. |
| `service.py` | The launchd LaunchAgent that keeps the bot running. |
| `scripts/check_no_datadir_paths.py` | Pre-commit hook that blocks staged content pointing into the data folder. |
| `scripts/pre_push_guard.py` | Pre-push hook that refuses any push updating or deleting `main` (ADR-011). |
| `scripts/verify_tree.py` | `make verify-tree`: every hook and gitleaks over every tracked and untracked file. |

## Where data lives, and why

```
/Volumes/ExampleDrive/ExampleFolder/        ← CAREERHUNTER_DATA_DIR (mode 0700)
├── search.db (+ -wal, -shm)              ← search and learning layer (0600)
├── ei_record_2026-27.db (+ -wal, -shm)   ← the active claim's EI record (0600)
├── ei_record_2025-26.db                  ← an earlier claim, sealed (0400, no -wal/-shm)
├── config.yaml                           ← your search settings and EI claims (from config.example.yaml)
├── exclusions.yaml                       ← companies/keywords to skip
├── seed.yaml                             ← one-off import of past history
├── exports/                              ← EI reports, timestamped, never overwritten
├── snapshots/<YYYY>/                     ← copies of job ads (0600; ADR-017)
├── logs/                                 ← bot.log and bot.heartbeat (0600; events only)
└── backups/
```

- **Outside the repo:** a git mistake can't leak data that isn't there. See DECISIONS ADR-001.
- **On an encrypted external volume:** the record is personal. The guard requires encrypted APFS and refuses anything else. See ADR-001 and ADR-002.
- **Paths inside the databases are relative** to the data folder (`snapshot_path` can't start with `/`), so renaming the drive doesn't break the record.
- **One EI file per claim**, so an earlier claim can be sealed read-only on its own and kept for 6 years from its own end date. See ADR-014.

## Storage guard sequence

```
CAREERHUNTER_DATA_DIR set and absolute?            ── no ──▶ refuse
resolve symlinks
inside this repo / contains this repo?             ── yes ─▶ refuse
inside ANY git work tree?                          ── yes ─▶ refuse
macOS?                                             ── no ──▶ refuse (can't verify)
nearest mount point == "/"? (drive not mounted)    ── yes ─▶ refuse
same st_dev as / or /System/Volumes/Data?          ── yes ─▶ refuse
diskutil: MountPoint matches, different APFS
  container from boot, Internal == false?          ── no ──▶ refuse
FilesystemType apfs, Encryption true, not Locked?  ── no ──▶ refuse
folder exists with mode 0700? (except `init`)      ── no ──▶ refuse
FileVault on boot disk?                            ── no ──▶ loud warning
```

`init` creates the data folder **without** creating missing parents. If `/Volumes/ExampleDrive` isn't mounted, the path would sit on the boot disk; the guard already refuses that, and the non-recursive `mkdir` is a second stop.

## Data model

**`search.db`**, the search and learning layer. It is editable, with no retention triggers:

```
companies 1──* postings 1──* applications 1──* decision_history
                    │              │
                    └──* skips     └── correlation_id, ei_claim ──▶ an EI record file
```

- Postings carry the learning fields: `fit_rating`, `gaps`, `llm_output`, `scout_metadata`. Applications carry `reasoning` and `notes`, and skips carry their `reason`.
- **Posting identity:** the URL, else company + requisition ID, else company + title + location + month first seen.
- An application's `correlation_id` never changes. Its `ei_claim` can be set once, and is NULL when no claim was active or the application fell outside the claim.

**`ei_record_<label>.db`**, one per EI claim. It is sanitized and append-only:

```
claim (one row: label, start, end, sealed_at)
applications 1──* status_history          activities
edits        (audit log of every permitted UPDATE, written by triggers)
```

- **Only EI fields:** employer, role, location, work model, posting URL, date and precision, method, the snapshot and resume references, and the void columns. There is no fit, gap, LLM, skip, scout or notes column anywhere, and tests hold the schema to an exact allowlist.
- **Dates:** ISO text. Applications and activities carry a `date_precision`:
  - `exact`: a real date;
  - `week`: `week_of`, always a Monday;
  - `unknown`: neither.
  A CHECK constraint enforces the right combination, and triggers refuse dates outside the claim period, by the same per-precision rule as the app.
- **Enumerations** (source, work model, method, status, activity type, ATS type) are CHECK-constrained in SQL. `enums.py` mirrors them for friendly validation messages. The status pipeline is shared by both layers: `applied`, `screening`, `assessment_submitted`, `interview_scheduled`, `offer`, `hired`, `rejected`, `no_response`, `withdrawn`.
- **Idempotency keys:**
  - in `search.db`: company name (case-insensitive), the posting identity above, and an application as posting + date;
  - in an EI record: an application as employer + role + location + posting URL + date, and an activity as type + description + date or week.
- **Retention and corrections** (EI records):
  - Every table has a `BEFORE DELETE` trigger that aborts, and nothing cascades.
  - `status_history` and `edits` are append-only. Status changes are new rows, and the current status is the latest row.
  - Mistaken applications and activities are **voided**: they need a reason, stay in the file, are left out of exports, and are frozen afterwards.
  - Every permitted UPDATE is written column-by-column to `edits` by triggers, so nothing can change without a trail.
  - `id`, `correlation_id` and `created_at` never change. The claim's label and start never change.
  - Once a claim is **sealed**, triggers refuse every INSERT and UPDATE, and the file is read-only.

## Writing to both layers

```
             1. open search.db's transaction and write its rows (not committed yet)
search.db ◀──────────────────────────────────────────────────── ledger / seed
                                                                    │
ei_record_<active>.db ◀── 2. write the sanitized copy and COMMIT ───┤
                                                                    │
search.db ◀────────────── 3. COMMIT ────────────────────────────────┘
```

- A problem on the search side stops everything before the EI record is touched.
- If step 3 fails, the EI record stands and the command reports a partial write. `doctor` then shows the gap.
- A skip writes `search.db` only. With no active claim, applications go to `search.db` only.
- Before any of this, `init`, `migrate` and `seed` refuse to run while an earlier claim's file is unsealed, or a claim file isn't in config.

`doctor` reconciles the two layers in both directions. It opens each file on its own connection, read-only, and never attaches one to another.

## Data flow

```
seed.yaml ──validate + claim check──▶ seed.py ──▶ search.db             (companies, postings,
                                         │                               applications, decisions)
                                         └─────▶ ei_record_<active>.db  (sanitized applications,
                                                                         statuses, activities)

ei_record_<label>.db ──read-only──▶ export --claim <label> --from --to
                                         │
                                         ├─▶ exports/ei_<label>_<from>_<to>_<ts>_applications.csv
                                         ├─▶ exports/ei_<label>_<from>_<to>_<ts>_activities.csv
                                         └─▶ exports/ei_<label>_<from>_<to>_<ts>.pdf
```

Export inclusion rules:
- exact dates within the range;
- weeks that overlap the range;
- undated records always listed, in a labelled "Date unknown" group so nothing silently drops out.

CSV cells starting with `= + - @` are prefixed with `'` to stop spreadsheet formula injection.

## The watch pipeline

`careerhunter watch` reads the watchlist once, then:

1. **Recheck.** Watcher postings that have no skip and no score are filtered again, so a config change can record a skip before any board is fetched.
2. **Score what is waiting.** A kept posting with no model text is scored before the boards are fetched. A down model or a reply that does not parse leaves it unscored.
3. **Fetch.** Greenhouse, Lever and Ashby list URLs are built from `ats_slug` and read through `fetch.py` (public addresses only). A failed board is a log line. Workday and the other ATS types are not fetched.
4. **Filter.** Each new posting is kept, skipped or flagged. The description is used only for that decision, and only when the list payload included one. It is not stored.
5. **Score what was just kept.** Same local model. A count at or above `exclude_from_gaps` is a `gap_tier` skip.

A skip is one `skips` row: `excluded_company`, `excluded_title`, `excluded_description`, `excluded_role`, `exclude_applied`, `required_language`, `credential`, `shift_pattern`, `pay_structure`, `seniority`, `min_salary`, `location_rule`, `role_filter` or `schedule_limit`. Seniority honors `title_exceptions` and `never_gated_families`. `min_salary` applies only when it is a number, and only when the posting states a yearly figure. `max_commute_km` and `exclude_work_models` are validated and not applied.

`over_limit: flag` keeps a posting whose stated weekend-day count is above the cap and sets `postings.schedule` to `over_weekend_limit`. `exclude` writes `schedule_limit` instead. An on-call mention sets `on_call` on that same column and keeps the posting, unless the weekend count already excluded it. The word "weekends" on its own does neither.

One `role_filters` block is AND only. It cannot express "this work model, or else that place." A missing work model does not fail `work_models`.

Scoring posts to `http://127.0.0.1:1234/v1/chat/completions` and nowhere else. The model returns gaps. The program applies `gap_tiers` and writes `gaps`, `llm_output` and `scout_metadata` (`{"tier": "<name>"}`). `fit_rating` stays null. A later run scores those still-unscored rows before it fetches again. Alerts are not part of this run yet. The run does not open an EI record.

## The Telegram bot (Phase 2)

```
your phone ──▶ Telegram's servers ◀── getUpdates (long poll) ── careerhunter bot run   (LaunchAgent)
                                                                  │  allowlist: one private chat
                                                                  │  buttons: single-use tokens (search.db)
                                                                  ├─▶ ledger ─▶ ei_record_<active>.db, then search.db
                                                                  ├─▶ fetch.py (public, pinned) ─▶ snapshots/
                                                                  └─▶ logs/bot.log, logs/bot.heartbeat
```

- No inbound port and no webhook: the bot only ever calls out to Telegram, and to the job-ad links it is given.
- Every write goes through the same ledger functions as the CLI, after the same claim checks (ADR-014). The bot re-reads config.yaml before each write.
- What it sends is minimal (ADR-015); what it trusts is limited (ADR-016); how it saves job ads is in ADR-017.
- `doctor` reports the chat setting, whether the service is running, and how recent its heartbeat is.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| 1, Foundation (v0.1.0) | Storage guards, Keychain helper, schema and migrations, seed import, EI export, CI, docs | done |
| 1.5, Data layer split (v0.2.0) | `search.db` and one EI record file per claim, a correlation ID, write order and reconciliation, claim config and sealing | done |
| 2, Telegram bot (v0.3.0) | The bot (alerts with Applied/Skipped, `/log`, `/status`, `/activity`, `/recent`), job-ad snapshots, the launchd service, the fit-criteria schema (validation only), and repo hygiene | **this release** |
| 3, Watcher | Fetch watchlisted companies' ATS boards; job-ad snapshots for the EI record; fit rating against the config rubric; skips with reasons | planned |
| 4, Discovery | Find new companies and postings beyond the watchlist | planned |
| 5, Release | License, polish | planned |

Each phase lands as a pull request against `main` with its own milestone, and gets a semver tag when merged. The repo goes public once Phase 1 is verified, and `main` is protected straight after (ADR-012).
