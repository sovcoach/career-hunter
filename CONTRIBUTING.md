# Contributing

**In plain language:** this page explains how to get career-hunter running on your own Mac for development, and how changes are made. If you only want to *use* it, follow [docs/SETUP.md](docs/SETUP.md) instead.

## Ground rules

- **No real data, ever.** Tests, fixtures, issues, PRs and screenshots use made-up companies and postings only. The pre-commit hooks help, but you're the first line of defence.
- **No secrets in code or files.** Use `careerhunter secret set`.
- **No deletes** in the record (see [ADR-005](docs/DECISIONS.md#adr-005-the-schema-is-designed-around-ei-record-keeping)).
- Schema changes go in a **new** migration file, never an edit to one that has been applied.

## Local setup

You need macOS, git and make (`xcode-select --install`), and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sovcoach/career-hunter.git
cd career-hunter
make setup        # installs Python 3.13, dependencies, the git hooks and the push guard
make check        # lint, types, tests + coverage, all pre-commit hooks
```

The first `make check` also builds gitleaks, which takes a minute or two.

`make setup` (and `make hooks`) also protect `main` from a stray push:

- they set `push.default=current` for this checkout, so a bare `git push` always goes to a branch of the same name, never to the branch it tracks;
- they install `scripts/pre_push_guard.py` as the pre-push hook, which refuses any push that would update or delete `main`, however the command is written.

`careerhunter doctor` warns if either is missing.

The data-path hook needs `CAREERHUNTER_DATA_DIR` set in the shell you commit from (the same value career-hunter uses). No path is built into the hook, so without it every commit is refused with a message saying so.

Tests never need a real data folder or Keychain: the storage guard and Keychain are replaced with fakes. To try the CLI by hand without your real drive, create a throwaway encrypted disk image (see "Testing the CLI safely" in [docs/SETUP.md](docs/SETUP.md#testing-the-cli-safely)).

## Workflow

1. Open or pick an **issue** and note its milestone (one per phase).
2. Branch from `main`, e.g. `feat/watchlist-sync` or `fix/export-week-overlap`.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/):
   `feat: …`, `fix: …`, `docs: …`, `test: …`, `refactor: …`, `chore: …`, `ci: …`.
4. Update `CHANGELOG.md` under `[Unreleased]`, and `docs/DECISIONS.md` if a design choice changed.
5. Open a **pull request** using the template. CI must pass: lint, types, tests with coverage of at least 85%, and gitleaks.
6. **Merge on GitHub** with "Create a merge commit" once the pull request's CI checks pass (ADR-010). A local merge is optional; its steps are in [AGENTS.md](AGENTS.md#merging). No force pushes to `main`: any other push to `main` is refused by the pre-push guard.
   - **Manual override, for the operator only:** `CAREERHUNTER_ALLOW_PUSH_MAIN=1 git push origin main`. It's for a deliberate, by-hand fix or the optional local merge; never put it in a script or alias. AI assistants are denied any command that mentions the variable, and never merge (ADR-011).
7. At the end of a phase: move `[Unreleased]` to a version and tag it (`vX.Y.0`).

## Common commands

| Command | Does |
|---|---|
| `make help` | List all commands |
| `make test` / `make cov` | Tests / tests with coverage |
| `make lint` / `make format` | Check / fix style |
| `make typecheck` | mypy, strict |
| `make check` | Lint, types, tests with coverage, and all pre-commit hooks. CI also runs the supply-chain checks below |
| `make verify-tree` | Every hook and gitleaks over every file, **untracked ones included**. `make check` uses `pre-commit run --all-files`, which only sees tracked files, and the gitleaks hook only scans staged changes (ADR-013), so run this before committing new files |
| `make hooks` | Reinstall the git hooks, the pre-push guard and `push.default=current` |
| `make check-lock-age` | Every version in `uv.lock` was at least 14 days old when it was locked, unless it is a listed security fix (ADR-006) |
| `make check-history BASE=origin/main` | No local `/Volumes/` or `/Users/` path in the commits since `BASE`, other than the allowed placeholders |

## Supply-chain checks

CI's **Supply chain** job runs two checks with full history (#29). Run them yourself before pushing: `make check-lock-age` after any re-lock, and `make check-history BASE=origin/main`.

- **Lock age.** Every version in `uv.lock` must have been uploaded at least 14 days before the commit that first locked it (ADR-006). `[tool.uv] exclude-newer` in `pyproject.toml` keeps `uv lock` behind a cutoff: move it forward deliberately when you re-lock, and evict a single version that breaks the rule with `exclude-newer-package`. A younger version is allowed only as a fix for known advisories, listed in `scripts/lock_age_allowlist.toml` with its package, exact version and advisory IDs. Remove an entry when that version leaves the lock: the check fails on a stale one.
- **Local paths in history.** A pull request's commits, messages included, may contain no `/Volumes/` or `/Users/` path except those in `scripts/history_path_allowlist.txt`: `/Volumes/ExampleDrive/ExampleFolder` for the data folder, `/Users/example/ExampleFolder/career-hunter` for the repo, and a few synthetic test volumes. A finding names the commit and masks the path. A path a test must reject is assembled at runtime from pieces, so it never appears in a file (ADR-013).
