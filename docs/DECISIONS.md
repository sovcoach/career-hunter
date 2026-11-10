# Design decisions

**In plain language:** this page explains *why* career-hunter is built the way it is. Each entry records the situation, what was decided, what else was considered, and what the choice costs. If you're wondering "why doesn't it just…?", the answer is probably here.

Entries follow a lightweight ADR (architecture decision record) format. New phases add entries; superseded ones are marked, never deleted.

| # | Decision | Status |
|---|---|---|
| 001 | Data lives outside the repo, on an encrypted external volume | Accepted (Phase 1) |
| 002 | A mount guard verifies the data volume, not just the path | Accepted (Phase 1) |
| 003 | Secrets live in the macOS Keychain | Accepted (Phase 1) |
| 004 | SQLite is the record store | Accepted (Phase 1); amended by 014; amended (#51) |
| 005 | The schema is designed around EI record-keeping | Accepted (Phase 1); amended by 014 |
| 006 | gitleaks via the pinned upstream pre-commit hook, not brew | Accepted (Phase 1); amended (lockfile) |
| 007 | A dedicated Telegram bot for notifications | Accepted (for Phase 2) |
| 008 | An always-on Mac mini as the host | Accepted (Phase 1) |
| 009 | Guards fail closed, with no override flag | Accepted (Phase 1) |
| 010 | Repo workflow conventions | Accepted (Phase 1); amended (local merges); amended (web merges) |
| 011 | Claude Code permission rules as a guard-rail | Accepted (Phase 1); amended (local merges); amended (web merges) |
| 012 | The repo goes public after Phase 1 verification; `main` is protected then | Accepted (Phase 1); amended (local merges); amended (web merges) |
| 013 | No exemptions in the data-path hook, not even for its own tests | Accepted (Phase 1) |
| 014 | Two data layers: `search.db`, and one EI record file per claim | Accepted (Phase 1.5) |
| 015 | Telegram is a cloud service: bot messages are kept minimal | Accepted (Phase 2); amended by 019; amended (#38, #33, #39) |
| 016 | The bot's trust boundary: long polling, an allowlist in code, opaque single-use buttons, fail-closed startup | Accepted (Phase 2); amended (#30) |
| 017 | A snapshot of every job ad applied to | Accepted (Phase 2) |
| 018 | One setting for the time zone | Accepted (Phase 2) |
| 019 | Local embedding vectors live in `search.db` beside the LLM tier | Accepted (Phase 3.5); amended (#30); amended by 023 |
| 020 | Discovery asks a local SearXNG and queues companies for approval | Accepted (Phase 4); amended; amended (#30) |
| 022 | A watch run isolates failures, logs as it goes, and has a time budget | Accepted (#30); amended by 023; amended (#38, #39, #32) |
| 023 | Scoring is grounded in an imported resume, and re-scored when it changes | Accepted (#37) |

---

## ADR-001: Data lives outside the repo, on an encrypted external volume

**Context.**
- The record holds personal details: where you applied, outcomes, notes, and eventually saved job ads.
- The code is meant to be shown publicly as a portfolio piece.
- Putting data in a gitignored folder inside the repo is common, but one wrong `git add -f`, a mis-edited `.gitignore`, or a tool that ignores ignore files can publish it.

**Decision.**
- All personal data lives in one folder named by `CAREERHUNTER_DATA_DIR`, on the encrypted external drive (`/Volumes/ExampleDrive/ExampleFolder`).
- The repo commits only `*.example.yaml` templates with generic values.
- `.gitignore`, gitleaks, and a custom pre-commit check are extra layers, not the primary defence.

**Alternatives.**
- A gitignored `data/` folder in the repo. Rejected: a single mistake leaks it.
- `~/ExampleFolder` on the internal disk. FileVault protects it, but the operator wants the record on the dedicated external drive alongside other large data.
- An encrypted sparse bundle. More moving parts for the same result.

**Trade-offs.**
- The drive must be mounted for the app to run (see ADR-002).
- Backups of the drive are the operator's responsibility; `backups/` is reserved for later automation.

## ADR-002: A mount guard verifies the data volume, not just the path

**Context.** If an external drive isn't mounted, `/Volumes/ExampleDrive` is just a path. A naive `mkdir -p` would silently create it on the **boot disk**. Data would then be written to the wrong, possibly unencrypted, place, and would appear to vanish when the drive comes back.

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

> **Amended by [ADR-014](#adr-014-two-data-layers-searchdb-and-one-ei-record-file-per-claim) (Phase 1.5).** There are now two kinds of SQLite file, each with its own migration track: `search.db`, and one `ei_record_<label>.db` per EI claim. Everything below still applies to each file.

> **Amended: one migration transaction (#51).** Two processes opening a file with a pending migration could both try it, and the second failed. Now:
> - When the file has its identity and every migration is applied, `migrate` returns without taking a write lock. Checksums are still checked on that path.
> - Otherwise all pending migrations run in one `BEGIN IMMEDIATE` transaction. The identity is claimed or checked and the applied versions are re-read inside the lock, so a process that waited applies only what is still pending, or nothing. There is one `COMMIT`, and any error rolls the whole run back, the identity claim on a new file included.
> - Each migration runs statement by statement through `execute`. Its text is split at `;` and joined again until `sqlite3.complete_statement` says a statement is complete, so a `;` in a comment, a string or a trigger body does not split it. `executescript` is not used, because it commits a held transaction.
> - The connection must be plain autocommit with no transaction open. A process waits up to 60 seconds for another one's lock, and then fails with a `MigrationError` that says nothing was applied.
> - SQLite ignores `PRAGMA foreign_keys` inside a transaction, so a migration cannot switch foreign keys off. A table rebuild uses `PRAGMA defer_foreign_keys = ON`, or a design that needs neither.

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

> **Amended by [ADR-014](#adr-014-two-data-layers-searchdb-and-one-ei-record-file-per-claim) (Phase 1.5).** This record now lives in one file per EI claim, without the learning fields: fit ratings, gaps, notes and skips moved to `search.db`. Companies and postings, and so the posting identity rules, are in `search.db` too. Retention, voiding, the audit log and honest dates are unchanged in the EI files.

> **Amended for reposts (#34).** Posting identity is unchanged: a role re-posted with a new URL, requisition ID or month is still a new posting. What changes is the alert. A posting at the same company row whose title has the same repost key (`filters.title_key`: NFKC, case folded, a bracketed or trailing requisition code dropped, punctuation and spaces folded) as a posting the operator logged or skipped is skipped as `already_handled` and never alerted. A requisition code is one token of letters, digits, `-` or `_` with at least three digits, such as `(R12345)`, `[JR-2041]` or `- 104332`; `(Level 2)` and `(Remote)` are kept, so `Analyst (Level 2)` and `Analyst (Level 3)` are different roles. Logged means the posting has an application, from any source. Skipped means a skip the operator chose: search migration 0016 adds `skips.by_operator`, set by the bot's Skipped button. A watcher skip (a filter, the posting age, a gap tier) does not suppress, so a changed config can still let a repost through. **Skips stored before 0016 are not filled in**: their reason text can't tell an operator skip from a filter skip (`seniority` is both), so they all keep `by_operator` 0, and only skips recorded after 0016 suppress reposts. The watcher skips a repost when it stores it, and at the start of every run skips any unalerted posting that became a repost since; the bot leaves such postings, and any posting with an application, out of its digest in the meantime. A company spelled differently in the two places is a different company row and does not match.

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

> **Amended: the lockfile (#29).** The two-week rule below now covers every version in `uv.lock` (direct and transitive, runtime and dev), as well as every pre-commit hook revision and GitHub Action pin. For the lockfile, two weeks is measured from a version's earliest upload time to the commit that first put that version in `uv.lock`. `scripts/check_lock_age.py` enforces it in CI and through `make check-lock-age`, and fails closed when it cannot tell, such as on a shallow clone or a missing upload time. `[tool.uv] exclude-newer` in `pyproject.toml` holds an absolute cutoff, moved forward deliberately at each re-lock, so `uv lock` cannot pick a newer release. A single version that the cutoff would keep but that breaks the rule, and fixes no advisory, is evicted with a per-package cutoff in `[tool.uv] exclude-newer-package`. The one exception: a version younger than 14 days is allowed only when it fixes a known advisory, and only when `scripts/lock_age_allowlist.toml` lists its package, exact version and advisory IDs. The check fails on any entry whose package and version are no longer in `uv.lock`. The ruff and mypy hook revisions must match their locked versions, and a test checks that. The age of hook revisions and Action pins stays a review item, because checking it needs the network.

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

Phase 2 built it: see ADR-015 (what the bot sends), ADR-016 (how it decides what to trust) and ADR-017 (job-ad snapshots).

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

> **Amended: web merges allowed (#44).** Supersedes the local-merge note below. Web merges are allowed for operator convenience. The operator merges each pull request on GitHub with "Create a merge commit" once its CI checks pass. A local merge with `git merge --no-ff` stays optional; the steps are in AGENTS.md under "Merging". Commits and tags no longer need UTC timestamps.

> **Amended: local merges (#44).** Pull requests are no longer merged through GitHub's web UI. A web-UI merge commit records the local time zone offset of whoever clicked the button. The operator now merges each pull request locally, after its CI checks pass, with `TZ=UTC git merge --no-ff <branch>` and a "Merge pull request #N from …" message, runs `make check` on the result, and pushes `main` as described in ADR-011 and ADR-012. The steps live in AGENTS.md under "Merging". Branches, Conventional Commits, releases and the quality gates below are unchanged.

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

> **Amended: web merges allowed (#44).** Supersedes the local-merge note below. Web merges are allowed for operator convenience. The pre-push guard's manual override is again for a deliberate, by-hand fix, and for the optional local merge described in AGENTS.md. It stays operator-only. Nothing changes for agents: the deny rules still refuse any command that mentions the override, any push that targets `main`, and both ways to skip the hook.

> **Amended: local merges (#44).** The pre-push guard's manual override is now also the operator's normal step for pushing a local merge commit to `main` (ADR-010). It stays operator-only and is typed by hand for that one push, never put in a script or alias. Nothing changes for agents: the deny rules still refuse any command that mentions the override, any push that targets `main`, and both ways to skip the hook, so an agent can neither merge into `main` nor push it.

**Context.** AI coding assistants are used on this repo. History-changing git commands should always need a human yes.

**Decision.**
- `.claude/settings.json` (committed) puts `git add/commit/push/rm/tag/merge/rebase/reset` on **ask**, in all three spellings: plain (`git add`), with a working directory (`git -C <dir> add`), and with a config override (`git -c <key=val> add`). `tests/test_claude_settings.py` loads the file and fails if any guarded subcommand loses a form. The `-C` and `-c` forms were added in Phase 1.5; before that, only the plain forms asked.
- It puts `gh api`, `gh pr merge`, `gh repo edit` and `gh release` on **ask**, so branch-protection changes, merges, repo settings and releases always need a human yes.
- It **denies** `rm -rf` (and flag-order variants), `sudo`, `docker run`, and force pushes (`--force`, `-f`, `--force-with-lease`).
- It **denies** any push whose refspec targets `main` (`main`, `HEAD:main`, `<ref>:main`, `refs/heads/main`, including `git -C <dir> push …`). Administrators may bypass branch protection on GitHub (ADR-012); this rule keeps agents from using that bypass.
- `.claude/settings.local.json` is gitignored, so personal grants never get committed.
- The same ask rules also cover a guarded command run after environment assignments, such as `TZ=UTC git commit` (Phase 2). `tests/test_claude_settings.py` checks this form too.
- **Pushes to `main` have three layers** (Phase 2, issue #4), because the deny rules above only see command text:
  1. `push.default=current`, set by `make setup` / `make hooks`. A bare `git push` then always targets a branch of the same name, never the upstream, so a branch that tracks `origin/main` can't push there by accident. `doctor` warns if it isn't set.
  2. A **pre-push hook** (`scripts/pre_push_guard.py`), installed by the same targets. It reads the refs git passes on stdin and refuses any push that would update or delete `refs/heads/main`, whatever the command looked like: a bare push, `HEAD:main`, an alias, a script, `git -C`, or a push of several refs at once. `doctor` warns if it isn't installed.
  3. One **manual override**, `CAREERHUNTER_ALLOW_PUSH_MAIN=1`, for the operator only. The deny rules block any command that mentions it, and also `--no-verify` and `core.hooksPath`, the two ways to skip the hook altogether.

**Trade-offs.** This is a guard-rail, not a security boundary: prefix rules can be sidestepped by unusual command shapes. For example, a bare `git push` on a branch that tracks `main` names no refspec, so no rule can tell where it goes; it still needs a yes under the `git push` ask rule, and since Phase 2 the pre-push hook refuses it anyway. The hook is local: it doesn't protect a checkout where `make hooks` was never run, and anyone at the keyboard can delete it. GitHub API calls (`gh api`) don't go through git at all, which is why they stay on ask. The real protections remain branch protection, the hooks and review.

During Phase 1.5, an agent ran `git add -N .` and `git reset -q` as parts of one compound command (after `cd <repo> &&`, joined with `;`), and no approval prompt appeared. Neither used `-C` or `-c`, so the change above would not have caught it. The agent now runs guarded git commands on their own, with no `cd` prefix and no chaining.

Ask rules only work while Claude Code's auto mode is off, so anything that must never happen unattended belongs in **deny**, not ask.

## ADR-012: The repo goes public after Phase 1 verification; `main` is protected then

> **Amended: web merges allowed (#44).** Supersedes the local-merge note below. Web merges are allowed for operator convenience. Pull requests merge on GitHub with "Create a merge commit". The branch protection settings are unchanged. Administrator bypass is again an exception: it is used only for an optional local merge or a by-hand fix, where GitHub reports "Bypassed rule violations" for the pull request and status check rules. Force pushes to `main` are never part of the flow.

> **Amended: local merges (#44).** The branch protection settings are unchanged: a pull request and the `Lint, types, tests` and `gitleaks (full history)` checks are required, and administrators may bypass. That bypass is now the normal merge path, not only an exception for a by-hand fix. The operator pushes a local merge commit (ADR-010) to `main`, and GitHub reports "Bypassed rule violations" for the pull request and status check rules. That is expected only on that push, after the pull request's own checks have passed. Force pushes to `main` are still never part of the flow.

**Context.**
- The repo is a portfolio piece, so it is meant to be visible.
- GitHub Free doesn't offer branch protection or rulesets on private repositories, so `main` can't be protected while the repo is private.
- Going public is only safe once the safety rails are proven: the storage guards, the pre-commit hooks, the full-history gitleaks scan in CI, and no real data anywhere in history.

**Decision.**
1. Bootstrap `main` with one commit and push it, without branch protection.
2. Land Phase 1 through a pull request, with CI green, including the full-history gitleaks scan.
3. Once the operator has verified Phase 1, make the repo **public**.
4. **Immediately afterwards**, protect `main`: require pull requests (no approvals required, as this is a solo project) and the `Lint, types, tests` and `gitleaks (full history)` checks, and block force pushes and deletion. Administrators may bypass, so the operator can push to `main` directly when needed. Agents can't, because ADR-011 denies pushes that target `main`.

Until step 4, no direct pushes to `main` is a convention, not something GitHub enforces.

**Alternatives.**
- A paid GitHub plan, to protect `main` while private. Rejected: not worth the cost for a solo portfolio project.
- Staying private until Phase 5. Rejected: it delays the portfolio value, and `main` would go unprotected for longer.

**Trade-offs.**
- There is a short window where `main` is unprotected. It is covered by the per-push approval rule in ADR-011 and by making step 4 the very next action.
- Being public means every commit is visible forever. That is why the history must be clean before going public, and why the gitleaks scan covers all history, not only new commits.
- Until a license is added in Phase 5, the code is visible but **all rights reserved**: others can read it, but have no grant to reuse it.

## ADR-013: No exemptions in the data-path hook, not even for its own tests

> **Amended in Phase 2.** The hook no longer has a built-in default path. With `CAREERHUNTER_DATA_DIR` unset it refuses the commit and says how to set it, so no real path is written anywhere in the repo. Its tests use a synthetic data folder.

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
- The upstream gitleaks hook is staged-only by design (`--staged`), so it too ignores the files `--all-files` and `--files` hand it. Committed content is covered by CI's full-history gitleaks scan, and untracked files can be checked with `gitleaks dir`. Since Phase 2 (issue #1), `make verify-tree` does both in one step: it runs every hook with `--files`, and the pinned gitleaks with `gitleaks dir`, over every tracked and untracked file. It fails on an empty file list or a missing gitleaks build instead of passing.
- A path match now also ends at a backslash, with a regression test that fails against the old pattern.
- Each hook fix landed as its own `fix(hooks)` commit, with tests in separate `test` commits.

**Alternatives.**
- Exempting `tests/` or that one file. Rejected: exemptions erode the rail. Each one is a place real data could later slip through unnoticed, and exemption lists tend to grow.
- Inline "ignore" markers. Rejected for the same reason: a marker copied into the wrong file silently disables the check there.

**Trade-offs.**
- Tests and docs that need such a path must build it at runtime, or describe it in words (as this entry does). That's a small inconvenience, accepted as the cost of a rail with no holes.

## ADR-014: Two data layers: `search.db`, and one EI record file per claim

> **Amended: one read-only report attaches (#34).** `careerhunter report --claim <label>` is the one code path that joins the two layers. It opens `search.db` read-only (`mode=ro`), attaches that claim's file read-only (`mode=ro`) as `ei`, and sets `query_only`, so neither file can be written through it. It joins on the random `correlation_id` only; there are still no cross-database foreign keys. The report is a private file, `reports/report-<claim>-<utc>.txt` (0600, folder 0700), never overwritten, and the terminal prints only its relative path. It lists each application's EI employer, role, date, method and latest EI status beside its lane, fit word and source in `search.db`, with counts by lane; orphans on either side are counted and left to `doctor` to name. Voided EI rows are counted and left out. No other code path attaches: `ei_store` still opens nothing, export still reads only the claim's file, and `doctor` still queries each file on its own. A test fails if `ATTACH` appears in any other source file. Writes are unchanged: they still go through the ledger's fixed order, not an attached transaction. `doctor` now names an application found on one side only (or linked to a claim with no file) as an `orphan`, in either direction, and still repairs nothing.

**Context.**
- Phase 1 kept everything in one `careerhunter.db`. Postings with fit ratings and gaps sat beside the applications that Service Canada may ask for.
- From Phase 2 on, the bots will store fit ratings, gaps, LLM output, scout metadata and skip reasons. None of that belongs in a record EI can request.
- In one file, "the EI record contains only what EI needs" is a rule every query has to remember. In separate files, it is a property of the storage.
- Each EI claim has its own period, and its record is kept for 6 years from the end of that claim.

**Decision.**
- **Two kinds of file**, both in `CAREERHUNTER_DATA_DIR` under the existing storage guard:
  - `search.db`, the search and learning layer: every posting from any source, posting identity, fit ratings, gaps, LLM output, scout metadata, applied and skipped decisions (with skip reasons), and the full decision history. It is editable and has no retention triggers, so a later retention policy can compact it.
  - `ei_record_<label>.db`, one per claim, such as `ei_record_2026-27.db`. It holds employer, role, location, work model, posting URL, date and precision, method, the status history, activities, and the job-ad snapshot and resume references. It keeps every ADR-005 protection: no deletes, append-only history, voiding, and the audit log.
- **Each kind has its own migration track** (`migrations/search/` and `migrations/ei_record/`), with checksums as before. Each file records its track in a permanent one-row `db_identity` table, checked on every open, so one kind can never be migrated as the other. A file without an identity is refused.
- **The two layers are linked only by an opaque correlation ID**: 128 random bits from `secrets.token_hex(16)`, never derived from content, time or hardware. `search.db` also records the label of the claim each application went to (NULL when none). An application's link can't change once set.
- **Sanitizing works by construction.** The EI writer accepts only an `EiApplication`, whose fields are exactly the EI columns, and the ledger builds it field by field. Tests compare the EI schema against an explicit allowlist, check that no learning column exists, and push a sentinel value through every learning field to prove none of them reach an EI file on disk.
- **The EI side never reads the search side.** `ei_store` imports no search code and never opens or attaches a database. Export opens only the claim's file, read-only, and works with `search.db` missing.
- **The active claim is set in config, never computed:** `ei.active_claim` plus a list of claims with a label, a start and an end. The end is optional only for the active claim. A label names two consecutive years (`2026-27`) and must start in the first one. Labels become file names, so nothing else is accepted. Claims may not overlap. With no active claim, nothing is written to any EI file, and `search.db` keeps learning.
- **Write order:** `search.db`'s transaction is opened and its rows written first, but not committed. The EI record then commits, as the source of truth, and `search.db` commits last. A search-side problem (for example a URL already owned by another company) therefore stops everything before the EI record is touched, and the only gap left is a failure of the final commit. That is reported as a partial write.
- **Consistency is reconciled, not made atomic.** `doctor` compares the two layers in both directions:
  - correlation IDs in an EI file but not in `search.db`;
  - IDs in `search.db` but missing from their claim's file;
  - links to a claim that has no file;
  - status histories that differ.

  Re-running `seed` repairs seed rows in either direction, reusing the correlation ID already on the surviving side.
- **The claim check depends on date precision:**
  - `exact`: the day must fall in the claim;
  - `week`: the week must overlap it;
  - `unknown`: always accepted, with a warning, and listed under "Date unknown".

  The EI schema enforces the same rule with triggers. In `seed`, an out-of-claim application is refused by default, because it is usually a typo. `--route-out-of-claim-to-search` sends such applications to `search.db` only and lists each one. Activities outside the claim are always refused.
- **The status pipeline is shared by both layers:** `applied`, `screening`, `assessment_submitted`, `interview_scheduled`, `offer`, `hired` (offer accepted), `rejected`, `no_response`, `withdrawn`. EI status rows carry no notes; notes stay in `search.db`.
- **Sealing is explicit, never automatic.** `careerhunter claim seal <label>`:
  - prints what it will do;
  - requires the label to be typed again;
  - refuses the active claim, a claim whose end date hasn't passed, and a claim not in config.

  Sealing records the end date and sets `sealed_at`, after which triggers refuse every change. It also folds the WAL into the main file, switches to a rollback journal so the file can be read with no companion files, and makes it read-only (0400). Until every earlier claim is sealed, `init`, `migrate` and `seed` refuse to run and name the exact seal command, and `doctor` reports it as BAD. A status that arrives after its claim is sealed is recorded in `search.db` only.
- **`doctor` only reads.** It never migrates, creates or seals, apart from tightening database file modes as before.
- **The v0.1.0 `careerhunter.db` is retired without migration**, because no real data was ever seeded into it. The new code never opens it for writing. `doctor` warns if it is empty, and reports BAD if it holds rows.

**Alternatives.**
- One database, with views or careful queries for EI. Rejected: the separation would be a rule, not a property.
- `ATTACH` both files and commit them in one transaction. Rejected: SQLite makes multi-file commits atomic only in rollback-journal mode, not in WAL mode. It would also break "the EI record never reads `search.db`".
- Two-phase commit or an outbox table. Rejected: a lot of machinery for a single-user tool, when a fixed order plus reconciliation covers the failure case.
- One EI file for all claims, with a claim column. Rejected: an earlier claim couldn't be made read-only on its own, and each claim's 6-year clock starts at a different time.
- Working out the active claim from dates. Rejected: a wrong guess would silently file records under the wrong claim.
- Sealing automatically when config moves on. Rejected: an irreversible step would hang on a config typo. With explicit sealing, a mistyped `active_claim` fails validation (the real current claim then has no end date) before anything can be sealed.
- A correlation ID derived from the record, such as a hash. Rejected: it could be recomputed from either side.

**Trade-offs.**
- A crash between the two commits leaves a gap until it is noticed. `doctor` reports it, and `seed` repairs seed rows, but there is no general repair command yet.
- A sealed file stays at the schema version it had when it was sealed, because migrations can't run on it. Export must keep reading older EI versions, so `ei_record` migrations should only ever add.
- The EI natural key (employer, role, location, URL, date) has no requisition ID. Two postings with the same title, at the same employer and location, applied to on the same day without URLs can't be told apart, so `seed` refuses them and asks for the URLs.
- There are more files to back up: `search.db` plus one file per claim.

## ADR-015: Telegram is a cloud service: bot messages are kept minimal

> **Amended by [ADR-019](#adr-019-local-embedding-vectors-live-in-searchdb-beside-the-llm-tier) (Phase 3.5).** A watcher alert also includes the tier name and, when a cosine is stored, that cosine. It still does not send notes, fit ratings, gaps, the model text, vectors, skip reasons, or the claim.

> **Amended for fit tiers and lanes (#38, #33).** Every alert now carries a fit tier word: `Fit: High`, `Fit: Medium` or `Fit: Low`, from the `fit` key of the gap tier the posting was scored into. A job ad sent to the bot is never scored and says `Fit: not scored`. With `search.lanes` set, a watcher alert also carries `Lane: <name>`, the name of a lane from `config.yaml`, or `Off-lane`. The fit word is a coarse, three-level label for the gap tier already named in the alert, and both its levels and the lane names are the operator's own config values. The bot still never sends the 0-10 `fit_rating`, gaps, the model text, vectors, skip reasons, notes or the claim. Alerts are sent in lane order: off-lane last, then the best fit, then non-fallback lanes before fallback ones, then lane priority, then the oldest. An off-lane posting below `search.off_lane_min_fit` (default `high`) stays pending. While the bot can't read `config.yaml` during a poll, it sends no watcher alert, records the hold for `doctor`, and logs only when the hold starts and ends. `/log` still needs config for today's date and the claim check, so with config unreadable it fails closed, writes nothing, and replies with a fixed message: `Config can't be read, so nothing was recorded. Run careerhunter doctor.` That reply has no path and no content.

> **Amended: refusals are kept and given up (#42).** A posting Telegram refuses when it is sent alone (HTTP 400 or 403) still goes behind the other pending alerts, but its refusals are now counted per posting in `search.db` (search migration 0017), so a restart of the bot remembers them. After three refusals the bot gives the posting up: it is never sent again, it stays unalerted and unskipped (so it is never counted as sent), and `doctor` reports how many postings were given up, as a warning, without titles. Telegram accepting the posting clears its count. No answer, a bad reply or HTTP 429 is not a refusal and is not counted.

> **Amended for self-sourced logging (#34).** `/log` now records a job the operator found and applied to, rather than alerting it. `/log <link> | method | date` records it in one message; a bare `/log <link>` asks for the method, then the date, with the same buttons as Applied. `| Company | Title` after the date fills in what the page doesn't show, so the earlier `/log <link> | Company | Title` form is gone. The method is a method code or its button word; a typed date is `YYYY-MM-DD`, `today` or `yesterday`. The posting (`source` self_sourced, or the posting already stored under that link) and the application go to both layers under one correlation ID, in one ledger call, only at the last step: nothing is written while the bot waits for a button. The reply is `Recorded: <company> — <title> (ref aN).`, with the same request for a PDF or screenshot when no copy of the ad was kept. When an application to the same role already exists (the repost match: the same companies row and `filters.title_key`), the application is still recorded and the reply gains one line, `You logged an application to this role on <date>.` (or `in the week of <date>.`), naming the latest earlier date and nothing else. The job-ad alert with `Fit: not scored`, the tier name and the cosine is no longer sent. `/log` still fails closed without config, with the same fixed reply and nothing fetched. `careerhunter log` does the same on the Mac.

> **Amended for discovery alerts (#41).** A posting found by discovery (`source` discovery) is alerted exactly like a watcher posting: same digest per watch run, same rows, same order (fit before lane priority), the same holds (off-lane below `search.off_lane_min_fit`, no fit, not yet lane-checked) and the same config hold. No approval comes first. A discovery digest row carries the suggested name and the reduced URL only, as before, now with its own Approve and Reject tokens; discovery rows are held too while `config.yaml` can't be read. Career pages are no longer sent.

> **Amended for the alert digest (#39).** Watcher postings are sent as one digest per watch run, at most `alerts.max_per_poll` (default 10) per poll, and the rest carry over. Each row carries: company, title, location, lane (`Lane: <name>` or `Off-lane`), the fit word, the schedule flag in words (on call, weekend days over the limit), up to three key requirements, the number of gaps, and the link. Key requirements are short phrases (60 characters at most) the local model takes from the posting itself, separate from the gap list; a phrase that contains a gap name, or repeats 40 characters of the resume, is dropped before it is stored. **Gap names stay local:** only their count is sent. A digest row no longer carries the LLM tier name or a cosine; a job ad sent with `/log` still gets its own alert as before. Rows keep the order above: off-lane last, then fit, then non-fallback before fallback lanes, then lane priority, then the oldest. #39 asked for rows "ordered by lane priority"; fit still comes before lane priority, as this ADR set for #33. Each row has its own Applied and Skipped tokens, so tapping one row leaves the others' buttons valid. Every message stays within Telegram's 4096-character limit. The sentinel scan now covers the digest text and its button labels: gap names, `llm_output`, notes, the resume and the claim label must be absent.

**Context.**
- The bot (Phase 2) talks to the operator through Telegram. Bot chats are not end-to-end encrypted: every message, both ways, passes through and is stored on Telegram's servers.
- Telegram also holds updates the bot hasn't fetched yet for up to 24 hours, and keeps the chat history.
- The EI record (ADR-014) and the learning layer hold personal details that don't need to leave the Mac.

**Decision.**
- **What the bot sends** is limited to company, title and link for a posting; the status words of the pipeline; short references such as `a7`; and fixed prompts and confirmations.
- **It never sends** notes, fit ratings, gaps, LLM output, skip reasons recorded earlier, EI record details, dates of other applications, or the claim label and its dates. When a write is refused because of the claim, the reply says only that the date is outside the active EI claim.
- **Confirmations don't echo** what was typed: `/activity` replies `Recorded activity: <type>`, without the description.
- **The bot keeps no message log.** Its own log (`logs/bot.log`) records events only: what kind of update was handled, and the outcome.
- Tests scan every message and button label the bot sends for sentinels planted in the learning fields, and for the claim label.

**Alternatives.**
- Signal or Matrix, for end-to-end encryption. Rejected for now: no simple, maintained bot API that fits a single-user tool on a Mac. Worth revisiting if the threat model changes.
- Email. Rejected in ADR-007.
- Richer alerts, with the fit rating and gaps. Rejected: they are exactly the learning data ADR-014 keeps apart. The operator reads them on the Mac.

**Trade-offs.**
- Whatever the operator types goes to Telegram: activity descriptions, typed skip reasons, typed dates, and any PDF or screenshot sent as a job-ad snapshot. That is accepted, and SETUP.md advises against typing anything sensitive.
- Company names, titles and links of the postings acted on are visible to Telegram.

## ADR-016: The bot's trust boundary

> **Amended: fetch deadlines (#30).** A fetch also has one total deadline, 60 seconds, shared by the lookup, every redirect hop and the body. A socket timeout starts again on every read, so on its own it cannot stop a server that sends one byte at a time.
> - The host name is looked up in a separate thread, and the caller waits only for the time left. At most four lookups can still be running after their caller gave up. With all four taken, a new lookup fails at once instead of waiting.
> - Before each chunk of the body, the socket timeout is set to the time left.
> - A watchdog on each hop shuts the socket down when the deadline passes, which also wakes a read that is stuck in the headers. Once it has fired, any error from that hop counts as running out of time.
> - A body whose declared length is over the cap is refused before anything is read.
> - A body that ends before its declared length is a failure, or out of time after a fire or past the deadline. A body that is complete by its own framing (its declared length arrived, or its chunked encoding reached the final chunk) is kept even if the deadline passed while it was read. A body of unknown length that ends at close after a fire or past the deadline counts as out of time.
> - A failure is one of four kinds: refused (the link or its address), out of time, too large, or failed (no usable reply).

**Context.**
- Anyone can find and message a Telegram bot. Restricting it when creating it in BotFather doesn't stop strangers from sending it messages or button presses.
- Everything in an update (text, button data, reply ids) is chosen by whoever sent it.
- The bot writes to the EI record, so a forged or replayed update must not be able to write anything.

**Decision.**
- **Long polling only** (`getUpdates`). There is no webhook, and the bot listens on no port.
- **The allowlist is enforced in code.** An update is handled only if the chat is private, its id is `telegram.chat_id` from config.yaml, and the sender's id matches too. Anything else gets no reply at all, not even a callback answer. The log records only an 8-character hash of the chat id. Forwarded messages are ignored.
- **Nothing in a message is used as an identifier.**
  - Buttons carry random 16-byte tokens (`bot_tokens`, search migration 0002). Each is checked against its chat, the message it was sent on, an expiry, and whether it was used; using one button spends its whole keyboard.
  - Replies are matched to the bot's own prompt by its message id (`bot_prompts`), with an expiry and single use.
  - `/status` takes short references the bot issued (`bot_refs`): scoped to the chat, expiring after 7 days, never a database id.
  - Status values, activity types and methods are checked against the enums.
- **Fail closed at startup.** The storage guard, a valid config.yaml with a chat id, every earlier claim sealed, the token in the Keychain (plaintext keyrings refused, ADR-003), and a working `getMe` are all required before the first poll. Before every write, the bot re-reads config and re-checks the claims, as the CLI does.
- **The token never reaches a log.** It is part of every API URL, so client errors are re-raised with the token redacted and the original exception suppressed, and the log handler redacts anything token-shaped.
- **Dates are asked for, not guessed.** After Applied and a method, the bot offers today, yesterday, this week, last week, or a date typed as YYYY-MM-DD. Exact days record `exact` precision; the week choices record `week`. Future dates are refused, and every date passes the claim check (ADR-014).
- **Links are fetched safely** (`fetch.py`). Only http and https, with no user name or password. Every address a host resolves to must be public. The connection then goes to that checked address, with the original Host header and SNI, so DNS rebinding can't swap in a private address between the check and the connect. The same applies to every redirect hop (at most five), and bodies are capped at 5 MB.
- **It runs as a user LaunchAgent** (`careerhunter service install`) with restart on failure, throttled to once a minute. launchd's own output goes to /dev/null, so it never creates log files under an unmounted /Volumes. `doctor` checks that the service runs and that its heartbeat is recent.

**Alternatives.**
- A webhook. Rejected: it needs an inbound port and a public endpoint.
- HMAC-signed button data instead of stored tokens. Rejected: stateless data can be replayed. Stored tokens are single-use, and expire.
- A Telegram framework library. Rejected: a large dependency tree for about eight API methods. The client is the standard library plus `truststore`.

**Trade-offs.**
- Updates are acknowledged before they are handled, so an update that crashes the handler is not retried. Buttons are single-use, so a replay could do nothing anyway.
- Token, reference and prompt rows accumulate in `search.db`. They are small, and expired rows can be compacted later along with the rest of the search layer.
- A message from a stranger still reaches Telegram, and costs one poll. It is dropped before any parsing beyond the allowlist check.

## ADR-017: A snapshot of every job ad applied to

**Context.**
- ADR-005 planned job-ad copies for the EI record, and the schema has had `snapshot_path` since Phase 1.
- Many ATS pages (Workday, SuccessFactors) are a JavaScript shell: fetching the page returns no ad.

**Decision.**
- When a posting is marked Applied, the bot takes the snapshot before recording the application, so its path goes into both layers in the same write:
  1. **The page itself**, fetched safely (ADR-016). It is kept if it carries a schema.org `JobPosting` (JSON-LD), or enough readable text to be the ad.
  2. **The ATS's public JSON**, for JavaScript pages: Workday's `/wday/cxs/...` endpoint, and the documented Greenhouse, Lever and Ashby APIs. It is saved as a small HTML page that embeds the JSON.
  3. **Otherwise, the operator sends a PDF or screenshot**, as a reply to the bot's request. It is checked by its bytes to really be a PDF, PNG or JPEG, then attached to both layers: EI first, and as an audited update in the EI record.
- Files are `snapshots/<YYYY>/<random>.<ext>`, owner-only (0600, folders 0700), never overwritten, with paths stored relative to the data folder. `doctor` checks the folder's modes.

**Alternatives.**
- A headless browser (Playwright) for JavaScript pages. Deferred to Phase 3 as an option: it is a dependency of several hundred megabytes, for a case the manual fallback already covers.
- Printing every page to PDF. Rejected for the same reason.

**Trade-offs.**
- SuccessFactors has no public JSON endpoint that works across tenants, so its ads usually need the manual fallback until the browser option exists.
- A snapshot taken when the operator applies may differ from the ad as first posted.

## ADR-018: One setting for the time zone

**Context.**
- career-hunter turns "now" into a date in several places: the date of an application or activity recorded through the bot, the claim checks, and whether a claim has ended before it can be sealed.
- If each of those used the computer's own zone, the answer would depend on how the machine happens to be set up.

**Decision.**
- **One field, `schedule.timezone`, is the only place a zone is set.** Nothing in the code hardcodes one, and nothing reads the system zone; every "today" goes through `config.today()`.
- **It takes either form:**
  - an IANA zone name, such as `Europe/Berlin`;
  - a fixed offset from UTC: `UTC`, `UTC-03:00`, `UTC+05:30` (at most 14 hours).

  A fixed offset doesn't depend on the system's time zone database being current.
- **If it's missing, dates use UTC**, and `doctor` shows a warning suggesting it be set. A missing zone doesn't stop anything from running.
- Dates are recorded to the day (ADR-005), so the zone only decides which day "today" is.

**Alternatives.**
- The system zone. Rejected: it makes the record depend on the machine.
- Requiring the field, and failing closed without it. Rejected: a wrong day is a small, visible error that the warning covers, unlike the storage and claim checks.

**Trade-offs.**
- A fixed offset doesn't follow any daylight-saving changes. Choosing between a name and an offset is left to the operator.

## ADR-019: Local embedding vectors live in search.db beside the LLM tier

> **Amended: a bad reply has its own reason (#42).** An embedding miss is now `unreachable` only when the server could not be used (refused, out of time, or a reply over the cap); a reply that arrived but is not a usable vector (another status, a body of the wrong shape, an empty, zero or non-finite vector) is the new reason `bad-reply`. Only `unreachable` stops embedding for the rest of the run, as before. The log line is still `embedding miss posting <id>: <reason>` and nothing else. Scoring already told the two apart (ADR-023): a `ScoreError` is a down model, a reply that does not parse leaves the posting as it was.

> **Amended: loopback deadlines (#30).** The scoring, embedding and SearXNG clients share one exchange with a total deadline: 120 seconds for scoring, 60 for embedding and 30 for discovery. The lookup, the connect, the reply headers and the body all count against it, and a watchdog shuts the socket down when it passes.
> - The scoring request does not stream, so no byte arrives until the model has finished. One step has to cover that wait, so scoring's step limit is its whole total.
> - A host name such as `localhost` is looked up inside the deadline, and only an answer in 127.0.0.0/8 or exactly `::1` is used.
> - Embedding replies have their own 1 MB cap. A vector of 1536 numbers is about 21 KB, and the 20,000-byte scoring cap cut it off, so it always missed.
> - Only a transport failure (refused, out of time, or a reply over the cap) marks the embedding server down for the rest of the run, so nothing more is sent to it. A reply that arrives but is not a vector is a miss for that posting only. The log text is unchanged either way.

**Context.**
- The watcher scores each posting with a local chat model and stores a tier name from `gap_tiers`. That tier judges missing requirements. It does not measure how close the posting text is to a resume.
- A cosine between one resume vector and each posting vector can sit beside that tier. It must not replace the tier, cause a skip, or decide an alert by itself.
- The useful query is one cosine of one resume against the postings already in `search.db`, not nearest-neighbour search.
- sqlite-vec 0.1.9, the latest stable release at the time of this decision, is still before 1.0. A `vec0` virtual table would be permanent checksummed SQL, and every open of that table would need the extension. `doctor`, `export`, `seed`, and the bot open `search.db` for other work.

**Decision.**
- **Float32 vectors are BLOBs in ordinary tables in `search.db`.** Search migration 0004 adds `resume_embedding` (one row: SHA-256 of the resume bytes, dimension, vector, timestamp) and `posting_embeddings` (one row per posting: dimension, vector, timestamp). `postings.embedding_score` holds the cosine, from -1 to 1, or null. The resume text and the posting text are not stored. The EI files stay on schema 1. `fit_rating` stays null.
- **No native extension.** `search.db` stays mode 0600 and opens with the standard library.
- **The embedding client is loopback only.** It posts to `http://127.0.0.1:1234/v1/embeddings` on the local LM Studio server, with the model id `local-embedding`. No redirects, and it is not `fetch.py`.
- **The cosine is stored beside the LLM tier and never replaces it.** The existing skip rules stay as they are. A cosine does not cause a skip. The alert keeps company, title, and link, and adds `LLM: <tier>`. When a cosine is present, one more line is `similarity: 0.82`.
- **A miss fails closed for that posting only.** If the resume file is missing, the embedding server is down, or a vector has the wrong dimension, that posting's cosine stays null. The watch log gets `embedding miss posting <id>: <reason>` and nothing else: no resume text, no posting text, and no vector. The run continues and still exits 0. The alert then has the LLM line only.
- **The description is still not a column.** The first score pass embeds the in-memory text: company, title, location, work model, and the description when the board sent one. A later pass has only the stored fields. The vector is the only kept trace of that description. When the resume bytes change, stored posting vectors are compared again locally, without calling the model.
- **`search.resume` is an optional path relative to the data folder.** The example is null. An absolute path or a `..` segment is invalid. The real path stays in the data-folder config. *Amended by [ADR-023](#adr-023-scoring-is-grounded-in-an-imported-resume-and-re-scored-when-it-changes):* the embedder reads the resume only through `resume.load`, so `search.resume` must be `resume/current.txt`, the file `careerhunter resume import` writes, unchanged since its import.
- **`fit_rubric.embedding_weight` is validated and the watcher does not apply it.** It is an optional number from 0 to 1. The example is null. The watcher does not read it for skips, alerts, or what it stores.
- **Evaluation reads stored scores only.** `careerhunter embed-eval` does not call the model. The report is a mode 0600 file under `evals/` in the data folder. The terminal prints the relative path only. Spearman uses midranks and Pearson correlation on those ranks, from the standard library. LLM rank comes from the order of `gap_tiers`, best first. Precision at 5 and at 10 counts an application as a hit. Fewer than two comparable rows writes `undefined` and exits 0. If `embedding_weight` is set, the report also ranks `weight * cosine + (1 - weight) * tier`. That blend is not stored and not alerted.

**Alternatives.**
- sqlite-vec. Rejected: this corpus is one resume against existing rows, the library is still before 1.0, and a virtual table would force every opener of `search.db` to load a native extension.
- A cloud embedding API. Rejected: the resume and the posting text stay on the machine.
- Blending the two scores in the watcher. Rejected: the tier and the cosine answer different questions. The weight belongs in the evaluation report.
- Storing the posting description so a later pass can embed the same text. Rejected: the description is not stored.

**Trade-offs.**
- A retry after the description is gone embeds only the stored fields, so it may not match the first vector.
- Cosine is computed in Python over the stored BLOBs. That fits one resume against the rows on disk, and it needs no approximate index.
- The report can show a weighted blend that the watcher never stores. That line is not the alert decision.

## ADR-020: Discovery asks a local SearXNG and queues companies for approval

> **Amended: a bad reply has its own reason (#42).** A transport failure, or a reply over the 1 MB cap, still writes `discovery miss: unreachable`. A reply that arrives with another status, a body that is not UTF-8 JSON, no `results` list, or a non-empty list with no usable url now writes `discovery miss: bad-reply`, replacing the `unreachable` text the bullets below give for those cases. Both are a miss: nothing is stored, the cursor stays, and the pass counts as failed for the backoff. HTTP 429 and an engine throttled are still a stop.

> **Amended: rotation, the watch agent and direct alerts (#40, #41).** Supersedes the query, interval, career-page and agent bullets below where they differ.
> - **Rotation.** The queries are the full product of titles (lanes by priority, every variant, each once; else `search.target_titles`), places (`search.location_rules`: on-site and hybrid places, then remote regions; `search.locations` is no longer read) and hosts (one Greenhouse query as `greenhouse.io`, Lever, Ashby, SmartRecruiters, BambooHR, `myworkdayjobs.com`, then `discovery.career_hosts`). A cursor in `search.db` (search migration 0014, one row) marks the next one. A pass takes the next `discovery.queries_per_run` (default 12, 1 to 40), wrapping around, and moves the cursor in the same transaction as its queue rows, so a miss asks the same queries again.
> - **The watch agent runs it.** The separate discovery LaunchAgent is retired; `service install` boots it out and removes its plist. A watch run starts with a pass when none was claimed in the last `discovery.interval_minutes`: the claim is one conditional UPDATE under `BEGIN IMMEDIATE`, so two processes never both run one, and a manual `careerhunter discover` within 150 seconds of another pass is skipped. A pass gets at most 150 seconds of the run's budget and runs isolated like a board, so a discovery failure never stops the boards. `logs/discover.success` is written when a pass has run. `doctor` checks it against two discovery or watch intervals, whichever is longer, and warns about a leftover discovery plist.
> - **Etiquette.** Queries are `discovery.query_delay_seconds` apart (default 5, 2 to 30; together a pass's pauses are at most 120 seconds). HTTP 429 from SearXNG, or an engine in `unresponsive_engines` whose error is `CAPTCHA` or `too many requests` (searx/webutils.py), stops the pass. A stop or a miss counts as a failed pass: the next waits `interval_minutes` times 2 to the failures in a row, at most seven days, and a pass that succeeds clears it.
> - **Posting URLs alert directly.** A hit that is a posting URL on a supported ATS is fetched once through that ATS's API by the same watch run (the single-posting endpoint, or the board for Ashby, which has none), stored with `source` discovery and sent through the same gates, age gate, lanes, scoring and digest as a watched posting. Approval never gates the first alert. A posting already stored is not fetched again, known by its identity on the board (`postings.ats_key`, search migration 0015) as well as its URL, so a Greenhouse posting on the company's own domain is known too. At most 20 are fetched per run; stored ones are left out before that limit counts, and each run starts at a different place in the rest, so no pass is spent on the same items.
> - **The queue is a separate step, and confirmed.** A board hit is queued only after one board-API request confirms it (at most 10 per pass, made before the queue's transaction; boards already watched or queued, in any status, and boards excluded by slug are left out before that limit counts, and each pass starts from its cursor in the rest). HTTP 404, a Workday 422 or a BambooHR redirect off the company's subdomain stores the board as `gone` (search migration 0012 rebuilds the table to allow it), and it is never queued or asked about again. SmartRecruiters answers an unknown company with an empty list, so an empty list drops the board for now without marking it gone. A company in `exclusions.companies` is never queued: its slug is checked before the request, and every company name the reply gives (Greenhouse's board name, SmartRecruiters' company name) after it, case and punctuation ignored. Career pages are no longer queued or sent; `career_hosts` only adds `site:` hosts. Career rows already stored stay listed and can still be approved by hand.
> - **Telegram.** Each row of a discovery digest has its own Approve and Reject tokens, so one tap leaves the others valid. Nothing is sent while `config.yaml` can't be read.

> **Amended: run log and stricter replies (#30).** A search run of `careerhunter discover` writes its lines to `logs/discover.log` in the data folder: the miss or off notice, then `discover: <n> queued`. List, approve and reject write no log. A reply with no `results` list, or a non-empty list where no result has a url, is a miss, and so is a body that is not UTF-8. A result without a url is skipped, and an empty list is a normal reply. The miss text is unchanged. See ADR-022 for the log file.

> **Amended.** Two additions. Pending discovery rows can be approved or rejected from the allowlisted Telegram chat, with the same rules as the CLI. `discovery.interval_minutes` schedules an optional one-shot LaunchAgent for `careerhunter discover`. The decision bullets added below specify both. The earlier bullets stay as the record of the first slice.

**Context.**
- The watcher fetches Greenhouse, Lever, and Ashby boards named on the config watchlist. It does not look for companies that are not already listed.
- A cloud search API would put a vendor key in this tool and send every query to that account. The useful search is a few site-restricted queries, run on purpose.
- `fetch.py` refuses loopback. That is right for a job-ad URL and wrong for a process on this Mac. The scoring and embedding clients already use a separate loopback HTTP client.
- A search hit is not a posting. The result title and snippet are page text. This tool still does not store a posting description.
- Approving a board by rewriting `config.yaml` would drop comments and reorder the operator's file. `companies.watchlist` is filled by the watcher today. It is not an input, and a seeded `watchlist: true` row is not fetched.
- Discovery rows are learning data. They do not belong in an EI record.

**Decision.**
- **SearXNG is operator-run.** career-hunter does not install it, start it, or open a port. It listens on `127.0.0.1` only. `public_instance` and `image_proxy` stay off, the limiter stays off, and `search.formats` includes `json`. Engines that need an API key stay disabled. No search-engine API key is stored for this tool.
- **The client is loopback only.** `discovery.searxng_url` is optional. The example is null, and null leaves discovery off. A set URL is plain `http` to `127.0.0.1`, `localhost`, or `::1`, with no userinfo, query, or fragment. Redirects are not followed. The client is not `fetch.py`.
- **Queries are site-restricted.** A query is `site:<host>` plus one title and one location. Titles come from `search.lanes`, or from `search.target_titles` when there are no lanes. Locations come from `search.locations`. Hosts are `boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, and each exact hostname in `discovery.career_hosts`. One run sends at most 12 queries. Exclusions, the resume, salary, and the chat id are not part of the query.
- **A career host is a hostname only.** No scheme, path, wildcard, or `..`. The example list is empty. A hit is kept only when it reduces to a Greenhouse, Lever, or Ashby slug, or to `https` on one of those career hosts. The slug is one path segment, the same rule the watcher uses. Every other host is dropped. The command does not fetch the result or parse HTML.
- **The queue is `discovery_candidates` in `search.db`.** Search migration 0005 adds it. The EI track stays at version 1. A row is a board or a career page, with the reduced URL, the ATS type and slug when there is one, a suggested name, and a status of pending, approved, or rejected. The suggested name is the slug, or the host for a career page. There is no snippet column and no raw response. The same key is not inserted again once it is pending, approved, or rejected, or already on the config watchlist.
- **Approve and reject are CLI commands.** They do not rewrite `config.yaml` or `exclusions.yaml`, and they do not send Telegram messages. Approving a board inserts or fills a company with `watchlist = 1`. A new company uses `source = 'discovery'`. An existing company's source is left as it is, and a stored ATS identity that disagrees is refused. The next `careerhunter watch` also fetches companies where `source = 'discovery'`, `watchlist = 1`, and the ATS type is Greenhouse, Lever, or Ashby with a valid slug. Config watchlist entries are fetched as they are today. A seeded watchlist row with any other source is not fetched by this extra path. From that fetch on, the existing filter, score, cosine, and alert path owns the board. Approving a career page stores `careers_url` with `watchlist = 0` and does not fetch it. Rejecting a row stores that status and does not insert a company.
- **A miss fails closed for the run.** A down server, a non-200 status, or a body that is not JSON writes `discovery miss: unreachable` and nothing else. The run still exits 0 and writes no queue row. There is no discovery LaunchAgent in this slice. The amendment above adds one.
- **No EI write.** The commands open `search.db` only. They do not write applications, snapshots, or EI rows.
- **Telegram approve and reject use the same rules.** After a poll, the bot may send a pending row to the allowlisted chat. The message is the suggested name and the reduced URL, plus Approve and Reject. The buttons are opaque single-use tokens. A tap calls `discover.approve` or `discover.reject` through the ledger and opens `search.db` only. It does not rewrite config files and it does not open an EI record. Search migration 0006 records `notified_at` after Telegram accepts the message. At most five pending rows are one message each. More than five are one digest of at most eight, and the rest wait for a later poll.
- **`discovery.interval_minutes` is optional.** Missing or null means 1440 minutes, once a day. A set value is an integer of at least 60. When `discovery.searxng_url` is set, `service install` also installs `org.careerhunter.discover`. That agent runs `careerhunter discover` once per the interval and exits. It has no `KeepAlive`. A null URL does not install it. A run that exits 0 writes `logs/discover.success`. Approve and reject do not write that stamp. `doctor` reports whether the agent is loaded and whether the stamp is within two intervals.

**Alternatives.**
- A cloud search API. Rejected: this tool would hold a vendor key, and every query would go to that account.
- Calling SearXNG through `fetch.py`. Rejected: that client refuses loopback.
- Rewriting `config.yaml` on approve. Rejected: a dump of the file would drop the operator's comments and reorder keys.
- Inserting postings from result titles and snippets. Rejected: a hit is not a board payload, and the snippet would become stored page text.
- Fetching or crawling career pages in this slice. Rejected: that is open web scraping. A career page is recorded and left unfetched.
- Telegram approve and reject buttons. Rejected for the first slice. The amendment above accepts them, and the write stays the existing approve and reject functions.
- A discovery daemon with `KeepAlive`. Rejected: one shot per interval matches the watch agent. A crash waits for the next interval.
- Installing the discovery agent when the URL is null. Rejected: it would run an off discovery on a timer.

**Trade-offs.**
- The query string leaves the machine. SearXNG sends it to the engines the operator enabled. The resume, exclusions, and EI record stay local.
- An approved board is fetched because of the database flag, so it does not appear in `config.yaml` until the operator copies it there.
- A career-page approval stores a URL and does not make the watcher fetch that site.
- A daily run still sends at most 12 site-restricted queries. A shorter interval, down to 60 minutes, sends that set more often.
- A discovery message leaves the suggested name and the URL on Telegram's servers. The snippet and the query do not.

## ADR-022: A watch run isolates failures, logs as it goes, and has a time budget

> **Amended by [ADR-023](#adr-023-scoring-is-grounded-in-an-imported-resume-and-re-scored-when-it-changes) (#37).** Inline scoring of new postings stops once less than 240 seconds of the budget are left. After the boards, scoring runs in three passes, each ordered by its last score attempt. A model that does not answer stops scoring for the rest of the run. The budget, the board rotation and the counting below are unchanged.

> **Amended for the run record (#38).** Search migration 0009 adds `watch_runs`. A run inserts its row first, in its own transaction, and finishes it just before the last line with the board counts, `budget_reached`, and the postings still held: unscored, scored with no fit, and off-lane below the threshold, read from `search.db` at that moment. A run the budget cut short still finishes, with `budget_reached` 1, as this ADR counts it a success. A run that crashes, is killed or stops on a database error never finishes its row, so its counts stay null and are never read as zero. A CHECK keeps an unfinished row free of counts. `doctor` reports a run unfinished for more than the budget plus five minutes as BAD, and the held counts from the last finished run. The table is never pruned: one small row per run.

> **Amended for gates on every ATS (#32).** Greenhouse is fetched with `content=true` and Ashby with `includeCompensation=true`, so the description and pay gates see text on every supported board. The text is held in memory only, as before. A board fetch has its own 20 MB body cap, larger than the 5 MB cap for a job ad (ADR-016), because a Greenhouse board now carries every description. A board over the cap logs `board over the size cap (...); nothing recorded`, stores nothing from that board, keeps its attempt stamp, and counts as failed, never as fetched or as zero postings. Nothing is truncated.

> **Amended for multi-request boards, posting age and discovery (#35, #36, #40, #41).** A SmartRecruiters, BambooHR or Workday board takes several requests: list pages (at most 25), then one detail per posting not stored yet (at most 40 per board per run, the rest left for the next run with a line). The 20 MB cap now covers all of a board's replies together. A detail that answers another status or a bad body leaves out that posting only; a fetch error or HTTP 429 stops the board's details, keeps what was read before, and counts the board as failed (or left, when the budget ran out). A posting older than `search.max_posting_age_days` is skipped as `too_old` (search migration 0013 keeps its posted date for the recheck). A discovery pass runs first in the run when it is due, with at most 150 seconds of the budget, isolated like a board; the postings it finds are fetched next, at most 20.

> **Amended for the alert digest (#39).** Each run stores its `watch_runs` id on every posting it scores or gives a fit (`postings.score_run_id`, search migration 0011), in the same UPDATE as the score. The bot sends one digest per run and waits until the run has finished, or started more than twice this budget ago, so a run is not sent half-scored and a killed run does not hold its postings for ever.

**Context.**
- One unexpected exception in one company ended the whole watch run, and the boards after it were never fetched.
- Log lines were written only when the run finished. launchd discards stderr, so a crash left no trace at all.
- A run had no limit on its total time. A slow board or a slow local model could hold it past the next scheduled run.
- A plain time limit fetches the boards in the same order every run, so the same boards at the end of the list would be left out every time.

**Decision.**
- **One company's failure is isolated.** An exception while handling one board, one row of the recheck or scoring pass, or the embedding refresh is rolled back and logged as `watch: <who>: <ExceptionType>; continuing`, and the run goes on. `sqlite3.IntegrityError` is isolated the same way.
- **Only infrastructure stops a run.** A missing or invalid `config.yaml`, a missing `search.db`, a database that cannot be opened, migrated or written, or a run log that cannot be written. Each is rolled back, writes an abort line to the log file and exits 1 without the success stamp. A storage guard refusal still exits 2 before anything is opened.
- **Lines are written as they happen.** Each line goes to `logs/watch.log` (or `logs/discover.log`) the moment it is produced, then to stderr. The file is opened only after the storage guard passes, without following a symlink, and must be a regular file kept at mode 0600. A line is written whole. A failed write to stderr is ignored.
- **Tracebacks stay in the private log.** A crash the run carries on from, and an unexpected failure, leave their full traceback in the log file only, never on stderr and never in the returned lines.
- **A run has a 600-second budget**, shared by the fetcher, the scoring and embedding clients and the run itself. The boards come first. The embedding refresh, the recheck and the scoring of waiting rows use what is left, and scoring waits for a complete recheck, so a row the current config has not filtered again is never scored.
- **Boards rotate.** Search migration 0007 adds `watch_board_attempts`: one row per board, keyed by ATS type and case-folded slug, with the time of the last attempt. Boards never tried come first, then the one tried longest ago; ties keep config order. The row is written in its own transaction just before each fetch. A board the budget leaves out, before or during its fetch, gets its earlier value back, or loses its row if it had none. A crash, an abort or a real failure keeps the new value.
- **Nothing left over is counted.** Anything the budget does not reach is logged as left for the next run and counted as neither fetched nor failed. A posting is sent to the scorer at most once per run. The last line is `watch: <n> boards fetched, <m> failed, <k> left`, and a board refused for an ATS identity mismatch counts as failed.

**Alternatives.**
- Writing the log at the end of the run, as before. Rejected: a crash loses every line.
- A time limit per board only. Rejected: many slow boards still add up to a run with no end.
- A fixed board order under the budget. Rejected: the same boards would be left out every run.
- Catching every exception, database errors included. Rejected: a database that cannot be written fails the same way for every company, and carrying on would only hide it.

**Trade-offs.**
- A posting stored after the budget ran out is scored on a later run from its stored fields only, without the description.
- A database error that would have affected only one company still stops the run.
- The log files grow without limit. There is no rotation yet.

## ADR-023: Scoring is grounded in an imported resume, and re-scored when it changes

> **Amended for first-come scoring (#38).** The never-scored pass is ordered by `coalesce(score_attempted_at, created_at)`, then id: each posting waits from its last attempt, or from when it was stored if it was never tried. Both are UTC stamps to the second. A retry stamped at T goes ahead of every posting stored after T, so a steady flow of new postings can't push a held posting back forever. The re-score passes keep never-tried first, then the oldest attempt. A score also stores its tier's `fit`, in the same UPDATE as the score and the hash, so a failed re-score keeps the old fit too.

**Context.**
- The scoring prompt asked the model for missing requirements without giving it the resume, so the gaps were guesses about the posting alone.
- The resume is the most personal text in the system. It has to reach the local model, and nothing else: no log, no `search.db` column, no exception, no Telegram message.
- A resume is a PDF or a DOCX, made by other software. Parsing it is parsing untrusted input, and both formats can be made to expand far beyond their size on disk.
- A changed resume makes every stored score stale, but a watch run has a 600-second budget, and the boards must still be fetched first (ADR-022).

**Decision.**
- **Import, locally.** `careerhunter resume import <file>` decides the format from the bytes: `%PDF-` is a PDF, and a zip holding `[Content_Types].xml` and `word/document.xml` is a DOCX. Anything else is refused.
  - PDF is read with pypdf. An encrypted PDF is refused before any page is touched, and so is one with more than 200 pages.
  - DOCX is read with the standard library. More than 100 zip entries is refused. `word/document.xml` is read with a bounded read, never trusting the zip header. Its bytes must be UTF-8 with no NUL byte and no DOCTYPE anywhere, and the parser reads exactly those bytes.
  - Caps are refused, never truncated: a 10 MB file, a 5 MB `document.xml`, and 50,000 characters, checked as the text is collected.
  - Every failure is a `ResumeError` with one of a fixed set of messages, no content and no path, raised `from None`. Nothing is stored.
- **One file, one hash.** The text is written to `resume/current.txt` in the data folder (folder 0700, file 0600, created with `O_EXCL` and `fchmod`). Only its SHA-256 and the time go into `search.db` (`resume_import`, search migration 0008).
  - One import runs at a time, under an exclusive lock on `resume/.import.lock`.
  - The file is renamed into place inside the database transaction. The file it replaces is kept as `resume/.previous.txt` until the COMMIT succeeds, then removed.
  - If the COMMIT fails after the rename, the previous file is put back. If that fails too, or the process stops in between, the file no longer matches its recorded hash. Then the resume is refused wherever it is read, and `doctor` reports it, so a mismatch is never used silently.
- **One way to read it.** `resume.load` returns the text only when `search.resume` is exactly `resume/current.txt` and the file matches the latest import. Scoring and embeddings both read the resume through it. `doctor` checks the modes, any leftover temporary or `.previous.txt` file, the hash, and the config value.
- **The resume goes in the scoring prompt, and the reply does not come back out (D-A1).** While a resume is in use:
  - `llm_output` stores canonical JSON of the parsed gaps, never the model's reply.
  - A reply is treated as one that does not parse when a gap name or type is longer than 120 characters, or when 40 characters in a row of one also appear in the resume. Both sides are compared after NFKC, case folding, and turning every run of whitespace into one space.
  - Such a posting stays unscored and is tried again.
- **A down model is told apart from a bad reply.** A refused URL, no connection, a timeout, or any HTTP status other than 200 is a `ScoreError`. It stops all scoring for the rest of the run, and the posting it happened on keeps its place. A 200 that does not parse leaves that posting as it was.
- **Re-scoring, under the budget.** A score records the hash of the resume it was made with (`postings.scored_resume_sha256`).
  - During the boards, only new postings are scored, and that stops once less than 240 seconds of the budget are left.
  - A stored posting on a fetched board whose hash differs is queued with its description, which stays in memory.
  - After the boards come never-scored postings, then the queued re-scores, then re-scores from the stored fields. Within each re-score pass the order is the last attempt (`postings.score_attempted_at`, never-tried first), then the oldest posting, so one posting that keeps failing goes behind the others. The never-scored pass is first come, first served (see the amendment above). Anything not reached is left for the next run and not counted.
  - A posting with a skip or an application keeps its score. An alerted posting is re-scored and not sent again. A failed re-score keeps the old score and hash.
- **A configured resume that cannot be used holds scoring.** If `search.resume` is set but `load` refuses it, the run scores nothing and logs one line with the fixed reason. It does not score without the resume while the hashes say otherwise.

**Alternatives.**
- Storing the resume text in `search.db`. Rejected: `search.db` is read by the bot, `doctor`, export and every backup, and the text does not need to be there.
- Storing the model's reply as before. Rejected: a model can quote its prompt, and the reply would carry the resume into `search.db`.
- A subprocess with a hard time and memory limit around pypdf. Rejected for now: the file is the operator's own, pypdf 6.21.4 limits stream decompression, and macOS does not enforce `RLIMIT_AS`, so the memory limit would hold on Linux only.
- defusedxml. Rejected: a DOCTYPE is refused before parsing, so no entity can be declared, and ElementTree never fetches an external one. It would be one more dependency for no gain.
- Re-scoring everything inline during the boards. Rejected: model calls would use the budget before later boards were fetched.
- Scoring without the resume when it cannot be loaded. Rejected: the stored hashes would then say a score used a resume it did not.

**Trade-offs.**
- pypdf 6.21.4 was younger than 14 days when it was locked. It is allowed as a listed security fix, with its own `exclude-newer` cutoff (ADR-006), because every older version has known denial-of-service advisories. The mypy pre-commit hook runs in its own environment with `pypdf==6.21.4`. That pin has to move with `uv.lock` whenever pypdf is upgraded.
- A copy of the resume shorter than 40 characters in a gap name is not caught.
- Re-scores wait while never-scored postings outnumber what one run can score.
- A posting that keeps failing to parse is retried in first-come order, so a backlog of postings stored before its last attempt goes first (#38).
- A re-score from the stored fields has no description, so it can differ from one made with it.
