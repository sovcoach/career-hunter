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
make setup        # installs Python 3.13, dependencies and the pre-commit hooks
make check        # lint, types, tests + coverage, all pre-commit hooks
```

The first `make check` also builds gitleaks, which takes a minute or two.

Tests never need a real data folder or Keychain: the storage guard and Keychain are replaced with fakes. To try the CLI by hand without your real drive, create a throwaway encrypted disk image (see "Testing the CLI safely" in [docs/SETUP.md](docs/SETUP.md#testing-the-cli-safely)).

## Workflow

1. Open or pick an **issue** and note its milestone (one per phase).
2. Branch from `main`, e.g. `feat/watchlist-sync` or `fix/export-week-overlap`.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/):
   `feat: …`, `fix: …`, `docs: …`, `test: …`, `refactor: …`, `chore: …`, `ci: …`.
4. Update `CHANGELOG.md` under `[Unreleased]`, and `docs/DECISIONS.md` if a design choice changed.
5. Open a **pull request** using the template. CI must pass: lint, types, tests with coverage of at least 85%, and gitleaks.
6. Merge through the PR. No direct or force pushes to `main`: once the repo is public, GitHub enforces this (ADR-012).
7. At the end of a phase: move `[Unreleased]` to a version and tag it (`vX.Y.0`).

## Common commands

| Command | Does |
|---|---|
| `make help` | List all commands |
| `make test` / `make cov` | Tests / tests with coverage |
| `make lint` / `make format` | Check / fix style |
| `make typecheck` | mypy, strict |
| `make check` | Everything CI runs, plus all pre-commit hooks |
