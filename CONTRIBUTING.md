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

**Privacy tip:** git records your time zone offset in every commit and tag. If you'd rather not publish it, commit and tag with UTC timestamps:

```bash
TZ=UTC git commit
```

Existing history keeps whatever offsets it was made with.

Tests never need a real data folder or Keychain: the storage guard and Keychain are replaced with fakes. To try the CLI by hand without your real drive, create a throwaway encrypted disk image (see "Testing the CLI safely" in [docs/SETUP.md](docs/SETUP.md#testing-the-cli-safely)).

## Workflow

1. Open or pick an **issue** and note its milestone (one per phase).
2. Branch from `main`, e.g. `feat/watchlist-sync` or `fix/export-week-overlap`.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/):
   `feat: …`, `fix: …`, `docs: …`, `test: …`, `refactor: …`, `chore: …`, `ci: …`.
4. Update `CHANGELOG.md` under `[Unreleased]`, and `docs/DECISIONS.md` if a design choice changed.
5. Open a **pull request** using the template. CI must pass: lint, types, tests with coverage of at least 85%, and gitleaks.
6. Merge through the PR. No direct or force pushes to `main`: GitHub enforces this (ADR-012), and so does the pre-push guard.
   - **Manual override, for the operator only:** `CAREERHUNTER_ALLOW_PUSH_MAIN=1 git push origin main`. It's for a deliberate, by-hand fix; never put it in a script or alias. AI assistants are denied any command that mentions the variable (ADR-011).
7. At the end of a phase: move `[Unreleased]` to a version and tag it (`vX.Y.0`).

## Common commands

| Command | Does |
|---|---|
| `make help` | List all commands |
| `make test` / `make cov` | Tests / tests with coverage |
| `make lint` / `make format` | Check / fix style |
| `make typecheck` | mypy, strict |
| `make check` | Everything CI runs, plus all pre-commit hooks |
| `make verify-tree` | Every hook and gitleaks over every file, **untracked ones included**. `make check` uses `pre-commit run --all-files`, which only sees tracked files, and the gitleaks hook only scans staged changes (ADR-013), so run this before committing new files |
| `make hooks` | Reinstall the git hooks, the pre-push guard and `push.default=current` |
