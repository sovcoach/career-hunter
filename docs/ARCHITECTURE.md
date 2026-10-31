# Architecture

**In plain language:** career-hunter has two halves that never mix.

- **The code** lives in this git repository and contains nothing personal.
- **Your data** lives in one folder on an encrypted external drive: the database, your real settings, saved copies of job ads, and exported reports.

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
| `db.py` | SQLite connection (WAL, `busy_timeout=5000`, foreign keys on), the versioned migration runner, and a transaction helper. |
| `migrations/NNNN_*.sql` | Schema history. Each file is applied once, and its checksum is recorded so edits are detected. `schema_migrations` and its two triggers (`no_delete_schema_migrations`, `no_update_schema_migrations`) are created by the migration runner in `db.py`, not by `0001`. A fresh database therefore has 23 triggers: 21 from `0001_init.sql` plus those 2. |
| `seed.py` | Validates, then imports `seed.yaml` in one transaction. Idempotent by natural keys. |
| `export.py` | Builds the EI record for a date range as CSV and PDF. |
| `scripts/check_no_datadir_paths.py` | Pre-commit hook that blocks staged content pointing into the data folder. |

## Where data lives, and why

```
/Volumes/X10-Pro/CareerHunterData/        ← CAREERHUNTER_DATA_DIR (mode 0700)
├── careerhunter.db (+ -wal, -shm)        ← the record
├── config.yaml                           ← your search settings (from config.example.yaml)
├── exclusions.yaml                       ← companies/keywords to skip
├── seed.yaml                             ← one-off import of past history
├── exports/                              ← EI reports, timestamped, never overwritten
├── snapshots/                            ← copies of job ads (Phase 3+)
├── logs/
└── backups/
```

- **Outside the repo:** a git mistake can't leak data that isn't there. See DECISIONS ADR-001.
- **On an encrypted external volume:** the record is personal. The guard requires encrypted APFS and refuses anything else. See ADR-001 and ADR-002.
- **Paths inside the DB are relative** to the data folder (`snapshot_path` can't start with `/`), so renaming the drive doesn't break the record.

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

`init` creates the data folder **without** creating missing parents. If `/Volumes/X10-Pro` isn't mounted, the path would sit on the boot disk; the guard already refuses that, and the non-recursive `mkdir` is a second stop.

## Data model

```
companies 1──* postings 1──* applications 1──* status_history
                    │
                    └──* skips
activities  (independent; related_company is free text)
edits       (audit log of every permitted UPDATE, written by triggers)
```

- **Dates:** ISO text. Applications and activities carry a `date_precision`:
  - `exact`: a real date;
  - `week`: `week_of`, always a Monday;
  - `unknown`: neither.
  A CHECK constraint enforces the right combination.
- **Enumerations** (source, work model, method, status, activity type, ATS type) are CHECK-constrained in SQL. `enums.py` mirrors them for friendly validation messages.
- **Idempotency keys:**
  - company name (case-insensitive);
  - posting: URL, else company + requisition ID, else company + title + location + month first seen;
  - application: posting + date, so re-applying on another date is a new row;
  - activity (type, description, date or week).
- **Retention and corrections:**
  - Every table has a `BEFORE DELETE` trigger that aborts, and nothing cascades.
  - `status_history` and `edits` are append-only. Status changes are new rows, and the current status is the latest row.
  - Mistaken applications and activities are **voided**: they need a reason, stay in the database, are left out of exports, and are frozen afterwards.
  - Every permitted UPDATE is written column-by-column to `edits` by triggers, so nothing can change without a trail.
  - `id` and `created_at` never change.

## Data flow (Phase 1)

```
seed.yaml ──validate──▶ seed.py ──one transaction──▶ SQLite
                                                       │
                       export --from --to  ◀───────────┘
                             │
                             ├─▶ exports/ei_<from>_<to>_<ts>_applications.csv
                             ├─▶ exports/ei_<from>_<to>_<ts>_activities.csv
                             └─▶ exports/ei_<from>_<to>_<ts>.pdf
```

Export inclusion rules:
- exact dates within the range;
- weeks that overlap the range;
- undated records always listed, in a labelled "Date unknown" group so nothing silently drops out.

CSV cells starting with `= + - @` are prefixed with `'` to stop spreadsheet formula injection.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| 1, Foundation (v0.1.0) | Storage guards, Keychain helper, schema and migrations, seed import, EI export, CI, docs | **this release** |
| 2, Notifications | Dedicated Telegram bot; token read from Keychain; daily digest and alerts | planned |
| 3, Watcher | Fetch watchlisted companies' ATS boards; job-ad snapshots for the EI record; fit rating against the config rubric; skips with reasons | planned |
| 4, Discovery | Find new companies and postings beyond the watchlist | planned |
| 5, Release | License, polish | planned |

Each phase lands as a pull request against `main` with its own milestone, and gets a semver tag when merged. The repo goes public once Phase 1 is verified, and `main` is protected straight after (ADR-012).
