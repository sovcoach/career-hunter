# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/sovcoach/career-hunter/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sovcoach/career-hunter/releases/tag/v0.1.0
