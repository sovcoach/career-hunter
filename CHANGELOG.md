# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - Phase 1.5: data layer split

### Added
- Two data layers in the data folder (ADR-014):
  - `search.db`, the editable search and learning layer: postings, fit ratings, gaps, LLM output, scout metadata, applied and skipped decisions with skip reasons, and the full decision history;
  - `ei_record_<label>.db`, one sanitized, append-only EI record per claim. It keeps the 0.1.0 protections: no deletes, voiding, the audit log and honest dates.
- An opaque, random correlation ID links each application across the two layers. The EI record never reads or attaches `search.db`, and `export` reads only the claim's file.
- One migration track per kind of file, each with checksums, and a permanent identity recorded in every file, so one kind can't be migrated as the other.
- EI claims in `config.yaml` (`ei.active_claim` and `ei.claims`), validated for label format, dates and overlap. The active claim is set, never computed. With no active claim, nothing is written to any EI record and `search.db` keeps learning.
- A fixed write order (EI record first, then `search.db`). A failure between the two commits is reported as a partial write.
- `careerhunter claim seal <label>`: prints what it will do, asks for the label again, then permanently closes an earlier claim's record and makes it read-only (0400). Sealing is never automatic. `init`, `migrate` and `seed` refuse to run while an earlier claim is unsealed, and name the command to run.
- `seed` routes each record to its layer, and checks every application and activity against the active claim by date precision: an exact date must fall within the claim, a week must overlap it, and an unknown date is accepted with a warning. Re-running `seed` fills in a side that is missing.
- `seed --route-out-of-claim-to-search` records out-of-claim applications in `search.db` only, and lists each one. Activities outside the claim are always refused.
- `doctor` checks:
  - the claim config;
  - the active claim's file and its 0600 modes;
  - that earlier claims are sealed and read-only;
  - both schema versions;
  - reconciliation between the layers, in both directions;
  - leftover v0.1.0 `careerhunter.db` files.
- The status pipeline gains `hired` (offer accepted).

### Changed
- `export` now takes `--claim <label>`, and the export file names include the label.
- Statuses are renamed `assessment_submitted` and `interview_scheduled` (they were `assessment` and `interview`). EI status rows no longer carry notes; notes stay in `search.db`.
- `doctor` no longer migrates. It reports a schema that is behind and says to run `careerhunter migrate`.
- The posting `source` can also be `self_sourced`.

### Removed
- The single `careerhunter.db` and its migration. 0.2.0 never opens it for writing. No real data was ever seeded into it, so there is nothing to migrate.

## [0.1.0] - 2026-10-31

### Added
- Fail-closed storage guard. It refuses to start unless `CAREERHUNTER_DATA_DIR` is set, outside any git repository, on a mounted external volume separate from the boot disk, and that volume is encrypted, unlocked APFS. It warns loudly if FileVault is off on the boot disk.
- `careerhunter init`: creates the private data folder (mode 0700) without ever creating missing parents, copies the config templates, and creates the database.
- SQLite schema `0001_init` (WAL, busy_timeout, foreign keys) for companies, postings, applications, status history, activities, skips and an `edits` audit log. It includes:
  - CHECK-constrained enums, including ATS types (SuccessFactors, iCIMS, Taleo, Jobvite and more) and the `recruiter_inbound` method;
  - date-precision rules;
  - no-delete triggers on every table, with append-only status history and audit log.
- Posting identity by URL, else requisition ID, else company + title + location + month first seen, so re-posted roles are new postings. Re-applying to the same posting on another date is supported.
- Voiding for applications and activities: a required reason, retained in the database, excluded from exports, and frozen afterwards.
- An audit trail: triggers log every changed column of every permitted update to `edits`.
- The database, WAL and SHM files are created owner-only (0600). `doctor` checks and fixes database file permissions, and flags config files readable by others.
- A versioned migration runner with checksum verification.
- `careerhunter seed`: validated, transactional, idempotent import of past applications and activities from `seed.yaml`.
- `careerhunter export`: EI job-search record for a date range as PDF and CSV, never overwriting an earlier export, with protection against spreadsheet formula injection.
- `careerhunter secret set|verify`: macOS Keychain helper that never echoes values and refuses plaintext backends.
- `careerhunter doctor`: setup check.
- Config validation for target titles, locations, hard gates, fit rubric, schedule and watchlist. Example templates use generic values.
- Pre-commit hooks: ruff, mypy (strict), gitleaks (pinned upstream hook), and a custom check that blocks anything resolving into the data folder.
- GitHub Actions CI: lint, type check, tests with an 85% coverage floor, and a full-history gitleaks scan with a checksum-pinned binary.
- Documentation: README, SETUP, ARCHITECTURE, DECISIONS, SECURITY and CONTRIBUTING, plus issue and PR templates.

[Unreleased]: https://github.com/sovcoach/career-hunter/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/sovcoach/career-hunter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sovcoach/career-hunter/releases/tag/v0.1.0
