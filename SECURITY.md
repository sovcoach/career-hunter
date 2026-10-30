# Security and personal data

**In plain language:** career-hunter handles your personal job-search history, and later a bot token. It keeps that information off the internet and out of this code repository: data on an encrypted drive, secrets in the macOS Keychain. Several independent checks stop it from ending up somewhere it shouldn't.

## How personal data is handled

- **Location.** Everything personal lives in `$CAREERHUNTER_DATA_DIR`, on an encrypted external APFS volume. The app refuses to run if that folder is unset, inside any git repository, not on a separate mounted volume, or on an unencrypted or locked volume. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#storage-guard-sequence).
- **Permissions.** The data folder is `0700` and config files are `0600`.
- **Never in git.** There are four layers:
  1. data outside the repo, enforced at startup;
  2. `.gitignore` rules for databases, env files and real config names;
  3. a pre-commit check that blocks any staged file, symlink or content pointing into the data folder;
  4. gitleaks secret scanning on every commit and over full history in CI.
- **Permissions.** The database and its WAL/SHM files are `0600`, and `careerhunter doctor` checks this.
- **Retention.** The database refuses deletes, to preserve a 6-year EI record. Mistakes are voided with a reason rather than removed, and every edit is logged in an append-only audit table. Exports are written next to the data and never overwritten.
- **Tests and CI** use synthetic fixtures only (fictional companies and postings).

## How secrets are handled

- Stored only in the **macOS Keychain** through `keyring` (service `career-hunter`). They are never written to files, `.env`, environment variables, logs or the repo.
- Set with `careerhunter secret set <name>`, using hidden input entered twice to confirm. Checked with `careerhunter secret verify <name>`, which reports only presence and length.
- Plaintext or null keyring backends are refused. There is no fallback.
- If a secret is ever exposed, revoke it at its source (e.g. @BotFather for Telegram), then store the new one with `secret set`.

## Reporting a security issue

Please **don't** open a public issue for security problems. Instead, use GitHub's private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**. If that isn't available, contact the maintainer directly through their GitHub profile.

Please include what you found, how to reproduce it, and its potential impact. Never include real personal data or secrets in a report. You should get an acknowledgement within a few days.

## Scope notes

- The `.claude/settings.json` permission rules are a guard-rail for AI-assisted development, not a security boundary.
- The tool trusts the local macOS user account. Anyone with that account's unlocked session can read the data and the Keychain.
