# Design decisions

**In plain language:** this page explains *why* career-hunter is built the way it is. Each entry records the situation, what was decided, what else was considered, and what the choice costs. If you're wondering "why doesn't it just…?", the answer is probably here.

Entries follow a lightweight ADR (architecture decision record) format. New phases add entries; superseded ones are marked, never deleted.

| # | Decision | Status |
|---|---|---|
| 001 | Data lives outside the repo, on an encrypted external volume | Accepted (Phase 1) |
| 002 | A mount guard verifies the data volume, not just the path | Accepted (Phase 1) |
| 003 | Secrets live in the macOS Keychain | Accepted (Phase 1) |
| 004 | SQLite is the record store | Accepted (Phase 1) |
| 005 | The schema is designed around EI record-keeping | Accepted (Phase 1) |
| 006 | gitleaks via the pinned upstream pre-commit hook, not brew | Accepted (Phase 1) |
| 007 | A dedicated Telegram bot for notifications | Accepted (for Phase 2) |
| 008 | An always-on Mac mini as the host | Accepted (Phase 1) |
| 009 | Guards fail closed, with no override flag | Accepted (Phase 1) |
| 010 | Repo workflow conventions | Accepted (Phase 1) |
| 011 | Claude Code permission rules as a guard-rail | Accepted (Phase 1) |
| 012 | The repo goes public after Phase 1 verification; `main` is protected then | Accepted (Phase 1) |
| 013 | No exemptions in the data-path hook, not even for its own tests | Accepted (Phase 1) |

---

## ADR-001: Data lives outside the repo, on an encrypted external volume

**Context.**
- The record holds personal details: where you applied, outcomes, notes, and eventually saved job ads.
- The code is meant to be shown publicly as a portfolio piece.
- Putting data in a gitignored folder inside the repo is common, but one wrong `git add -f`, a mis-edited `.gitignore`, or a tool that ignores ignore files can publish it.

**Decision.**
- All personal data lives in one folder named by `CAREERHUNTER_DATA_DIR`, on the encrypted external drive (`/Volumes/X10-Pro/CareerHunterData`).
- The repo commits only `*.example.yaml` templates with generic values.
- `.gitignore`, gitleaks, and a custom pre-commit check are extra layers, not the primary defence.

**Alternatives.**
- A gitignored `data/` folder in the repo. Rejected: a single mistake leaks it.
- `~/CareerHunterData` on the internal disk. FileVault protects it, but the operator wants the record on the dedicated external drive alongside other large data.
- An encrypted sparse bundle. More moving parts for the same result.

**Trade-offs.**
- The drive must be mounted for the app to run (see ADR-002).
- Backups of the drive are the operator's responsibility; `backups/` is reserved for later automation.

## ADR-002: A mount guard verifies the data volume, not just the path

**Context.** If an external drive isn't mounted, `/Volumes/X10-Pro` is just a path. A naive `mkdir -p` would silently create it on the **boot disk**. Data would then be written to the wrong, possibly unencrypted, place, and would appear to vanish when the drive comes back.

**Decision.** Before every command, the guard:
- walks up to the data folder's real mount point and refuses if that is `/`;
- compares `st_dev` with the boot volume's;
- asks `diskutil info -plist` to confirm the mount point, a different APFS container from the boot volume, `Internal == false`, `FilesystemType == apfs`, `Encryption == true`, and `Locked == false`.

`init` creates the data folder without creating missing parents.

**Alternatives.**
- Checking that the path exists. Rejected: it can't tell a real mount from a stray folder.
- Checking the volume name only. Rejected: it doesn't prove separation or encryption.

**Trade-offs.**
- macOS only. On other platforms the guard can't verify anything, so it refuses (ADR-009).
- A friend running without an external drive must use one, or change this decision in their fork.

## ADR-003: Secrets live in the macOS Keychain

**Context.** Phase 2 needs a Telegram bot token. Tokens in `.env` files or config files are the most common way secrets end up in git history, backups and screenshots.

**Decision.**
- Use the `keyring` library with the macOS Keychain backend, under the service name `career-hunter`.
- Only allowlisted names can be stored.
- The CLI reads values with hidden input (twice, to confirm) and only ever reports "present (N chars)" or "missing".
- Plaintext and null keyring backends are refused. There is no env-var or file fallback.

**Alternatives.**
- A `.env` file with gitignore. Rejected: plaintext on disk and easily leaked.
- 1Password CLI. Strong, but an extra dependency and login for a single token.
- Environment variables. They leak into process listings, logs and child processes.

**Trade-offs.**
- The Keychain must be unlocked, which on an always-on Mac means a logged-in user session.
- Tests use an in-memory backend, so they never touch the real Keychain.

## ADR-004: SQLite is the record store

**Context.** One user, one machine, a few thousand rows over years. The record must survive for 6+ years and be easy to back up.

**Decision.**
- Use SQLite in WAL mode with `busy_timeout=5000` (the watcher and CLI may overlap in later phases) and foreign keys on.
- Versioned SQL migrations live in the repo. Each is applied once in a transaction, with its checksum recorded; editing an applied migration is detected and refused.

**Alternatives.**
- Postgres. Rejected: a server to run for a single-user tool.
- CSV or YAML files. Rejected: no constraints and no transactions.
- An ORM. Rejected: it hides the CHECK constraints and triggers that do the real enforcement.

**Trade-offs.**
- Enum values are duplicated in `enums.py` for friendly validation. The database stays the source of truth.

## ADR-005: The schema is designed around EI record-keeping

**Context.**
- EI claimants can be asked by Service Canada for a detailed record of their job search: employers contacted, dates, how they applied, and outcomes, plus other activities such as job fairs, networking and agency registrations.
- This project designs for keeping that record for **6 years**, and for keeping **copies of the job ads** applied to.
- Check current rules on canada.ca; this tool records evidence but doesn't interpret eligibility.

**Decision.**
- **No deletes anywhere.** Every table has a `BEFORE DELETE` trigger that aborts, and there are no cascades. `status_history` is append-only: status changes are new rows.
- **Voiding instead of deleting.** A mistaken application or activity is voided: `voided = 1` with a required `void_reason` and a `voided_at` timestamp. Voided rows stay in the database and are left out of exports. They are frozen, so a void can't be undone and the row can't be edited afterwards.
- **Every edit is audited.** Triggers write one row to `edits` for every column changed by any permitted UPDATE on companies, postings, applications, activities and skips, recording the table, row, column, old value, new value and time. `edits` is append-only. `id` and `created_at` can never change.
- **Posting identity, strongest first:**
  1. the posting URL;
  2. else company + requisition ID;
  3. else company + title + location + the month the posting was first seen.

  A role re-posted in a later month is therefore a new posting.
- **Re-applying is allowed.** An application is identified by posting + date, so a second application to the same posting on a different date is recorded as its own row.
- **Honest dates.** Applications and activities record `date_precision` (`exact`, `week`, `unknown`), so reconstructed history isn't given false precision. Exports show "week of …" or a separate "Date unknown" group rather than inventing dates.
- **Job-ad copies.** `postings.snapshot_path` points into `snapshots/` (filled from Phase 3), stored relative to the data folder.
- **Export.** CSV and PDF for any date range: applications with full status history, and activities in their own section. Files are timestamped and never overwritten.

**Alternatives.**
- A plain soft-delete flag. Rejected on its own because it could hide records silently. Voiding is that idea, made safe: it requires a reason, can't be reversed, and leaves a trail in the audit log.
- Company + title + location as the posting key. Rejected: re-posts of the same role months later would collide, and the later application would be lost.
- A required exact date. Rejected: it would force made-up dates for past applications.

**Trade-offs.**
- Mistakes can't be deleted. They are voided or corrected by an audited update, so the database only grows.
- Without a URL or requisition ID, two genuinely different postings with the same title and location in the same month would be treated as one. Recording the URL avoids this.
- An undated (`unknown`) seed entry is matched to its earlier import by company, title and location, because its first-seen month is the import date and so changes between runs.

## ADR-006: gitleaks via the pinned upstream pre-commit hook, not brew

**Context.** Secret scanning should run on every commit for anyone who clones the repo, at a known version.

**Decision.**
- Use the upstream `gitleaks` pre-commit hook pinned to an exact release tag (`v8.30.1`), chosen at least two weeks after release.
- pre-commit builds it with a Go toolchain it manages, so nothing is installed system-wide.
- CI runs the same version over the **full history**, from the release binary verified against a pinned SHA-256.
- Every other hook and GitHub Action is pinned the same way (Actions by commit SHA).

**Alternatives.**
- `brew install gitleaks` with a system hook. Rejected: version drift between machines, and a global install for one project.
- Docker. Rejected: heavy for a pre-commit hook.

**Trade-offs.**
- The first `pre-commit run` downloads Go and builds gitleaks (a minute or two).
- Pins must be bumped deliberately.

## ADR-007: A dedicated Telegram bot for notifications

**Context.** Phase 2 will push alerts and digests to the operator's phone.

**Decision.**
- Create a bot used only by career-hunter, with its token in the Keychain (ADR-003).
- It talks only to the operator's own chat ID, stored in config.

**Alternatives.**
- Reusing an existing bot. Rejected: shared tokens mean shared blast radius, and revoking one would break the others.
- Email. Rejected: slower, and noisier to filter.

**Trade-offs.** One more token to manage, which is revocable on its own without affecting anything else.

## ADR-008: An always-on Mac mini as the host

**Context.** The watcher (Phase 3) needs to run on a schedule, and the data needs to stay local.

**Decision.**
- Host on an always-on Mac mini, with the data drive attached by USB.
- The boot disk uses FileVault, and the app warns loudly at startup if it is off.
- The data volume is encrypted APFS, with its password saved to the Keychain so it unlocks at login.

**Alternatives.**
- A cloud VM. Rejected: personal data off-machine and an ongoing cost.
- A Raspberry Pi. Rejected: no Keychain, and different encryption tooling.

**Trade-offs.**
- macOS-specific (diskutil, fdesetup, Keychain).
- After a reboot, a user login is needed before the Keychain and the encrypted drive are available.

## ADR-009: Guards fail closed, with no override flag

**Context.** A guard that can be skipped "just this once" will eventually be skipped by mistake, or by a script.

**Decision.**
- Every storage check either verifies the condition or refuses, with a plain-language explanation and the fix.
- "Couldn't determine" counts as "no": an unreadable `diskutil`, an unknown platform, or a missing `Internal` key all refuse.
- There is no `--force` flag. The only soft check is boot-disk FileVault, which warns rather than refuses because the data isn't stored there.

**Alternatives.** An `--i-know-what-im-doing` flag. Rejected for the reason above.

**Trade-offs.** Unusual-but-safe setups need a code change, which is reviewed in a PR.

## ADR-010: Repo workflow conventions

**Context.** The repo is a portfolio piece, so it should show how a project is run, not just what it does.

**Decision.**
- **Branches and PRs:** feature branches, with one PR per phase or logical change and a clear description, even solo. `main` is protected once the remote exists: no direct pushes and no force-pushes.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `ci:`).
- **Releases:** [Keep a Changelog](https://keepachangelog.com/) and semver tags at the end of each phase (Phase 1 = `v0.1.0`).
- **Planning:** GitHub Issues with bug and feature templates, and a milestone per phase.
- **Quality gates:** pre-commit (ruff lint + format, mypy strict, gitleaks, data-path check) and CI on every PR (lint, types, tests with an 85% coverage floor, full-history gitleaks). CI uses synthetic fixtures only.
- **Task runner:** a `Makefile`, because `make` ships with the macOS developer tools so there's nothing to install.

**Trade-offs.**
- Ceremony for a solo project, which is accepted because demonstrating it is part of the goal.
- GitHub Free can't protect branches on a **private** repo. Until the repo goes public (ADR-012), "no direct pushes to `main`" is followed by convention.

## ADR-011: Claude Code permission rules as a guard-rail

**Context.** AI coding assistants are used on this repo. History-changing git commands should always need a human yes.

**Decision.**
- `.claude/settings.json` (committed) puts `git add/commit/push/rm/tag/merge/rebase/reset` on **ask**.
- It **denies** `rm -rf` (and flag-order variants), `sudo`, `docker run`, and force pushes (`--force`, `-f`, `--force-with-lease`).
- `.claude/settings.local.json` is gitignored, so personal grants never get committed.

**Trade-offs.** This is a guard-rail, not a security boundary: prefix rules can be sidestepped by unusual command shapes. The real protections are branch protection, pre-commit hooks and review.

## ADR-012: The repo goes public after Phase 1 verification; `main` is protected then

**Context.**
- The repo is a portfolio piece, so it is meant to be visible.
- GitHub Free doesn't offer branch protection or rulesets on private repositories, so `main` can't be protected while the repo is private.
- Going public is only safe once the safety rails are proven: the storage guards, the pre-commit hooks, the full-history gitleaks scan in CI, and no real data anywhere in history.

**Decision.**
1. Bootstrap `main` with one commit and push it, without branch protection.
2. Land Phase 1 through a pull request, with CI green, including the full-history gitleaks scan.
3. Once the operator has verified Phase 1, make the repo **public**.
4. **Immediately afterwards**, protect `main`: require pull requests and passing CI, and block force pushes and deletion.

Until step 4, no direct pushes to `main` is a convention, not something GitHub enforces.

**Alternatives.**
- A paid GitHub plan, to protect `main` while private. Rejected: not worth the cost for a solo portfolio project.
- Staying private until Phase 5. Rejected: it delays the portfolio value, and `main` would go unprotected for longer.

**Trade-offs.**
- There is a short window where `main` is unprotected. It is covered by the per-push approval rule in ADR-011 and by making step 4 the very next action.
- Being public means every commit is visible forever. That is why the history must be clean before going public, and why the gitleaks scan covers all history, not only new commits.
- Until a license is added in Phase 5, the code is visible but **all rights reserved**: others can read it, but have no grant to reuse it.

## ADR-013: No exemptions in the data-path hook, not even for its own tests

**Context.**
- During the Phase 1 commits, the `check-no-datadir-paths` hook blocked the test-suite commit.
- One test checks that the hook still works when `CAREERHUNTER_DATA_DIR` is unset. Its source held the default data folder's absolute path followed by `/seed.yaml`, written out in full.
- That text was synthetic, but the hook can't tell a test string from a genuinely leaked path, and it shouldn't try to.
- The same commit showed a smaller flaw: the reported path included a trailing `\n` escape from the source code.
- The next commit, the docs, was blocked as well: `docs/SETUP.md` showed example command output containing full paths to folders and files inside the data folder.
- That second block exposed a verification gap. The hook was configured with `pass_filenames: false` and only ever read the *staged* files. So `pre-commit run --all-files` or `--files …` reported "Passed" without scanning the files it was given, and earlier claims that all hooks passed on every file were not true for this hook.

**Decision.**
- Fix the test, not the rail. The test now reads `DEFAULT_DATA_DIR` from the hook script and builds the file path when it runs, so no full path to a file inside the data folder appears in the source.
- The hook has no allowlist: it scans tests and docs like any other file.
- The hook now scans the files pre-commit passes it. A staged file is checked as staged, from the index, which is what a commit records. Any other file is checked as it is in the working tree. With no filenames, it checks every staged file. This makes `pre-commit run --all-files` and `--files …` truthful. Tests cover both modes, and fail against the old hook. Note that `--all-files` covers *tracked* files only; to include untracked files, pass them with `--files`.
- `docs/SETUP.md` now writes example output with `$CAREERHUNTER_DATA_DIR`, plus a note that real output shows the full path.
- The upstream gitleaks hook is staged-only by design (`--staged`), so it too ignores the files `--all-files` and `--files` hand it. Committed content is covered by CI's full-history gitleaks scan, and untracked files can be checked with `gitleaks dir`.
- A path match now also ends at a backslash, with a regression test that fails against the old pattern.
- Each hook fix landed as its own `fix(hooks)` commit, with tests in separate `test` commits.

**Alternatives.**
- Exempting `tests/` or that one file. Rejected: exemptions erode the rail. Each one is a place real data could later slip through unnoticed, and exemption lists tend to grow.
- Inline "ignore" markers. Rejected for the same reason: a marker copied into the wrong file silently disables the check there.

**Trade-offs.**
- Tests and docs that need such a path must build it at runtime, or describe it in words (as this entry does). That's a small inconvenience, accepted as the cost of a rail with no holes.
