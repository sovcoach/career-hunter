# Security and personal data

**In plain language:** career-hunter handles your personal job-search history, your resume, and a Telegram bot token. It keeps that information out of this code repository, and sends as little as it can off the Mac: data on an encrypted drive, secrets in the macOS Keychain. Several independent checks stop it from ending up somewhere it shouldn't.

## How personal data is handled

- **Location.** Everything personal lives in `$CAREERHUNTER_DATA_DIR`, on an encrypted external APFS volume. The app refuses to run if that folder is unset, inside any git repository, not on a separate mounted volume, or on an unencrypted or locked volume. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#storage-guard-sequence).
- **Permissions.** The data folder is `0700` and config files are `0600`.
- **Never in git.** There are four layers:
  1. data outside the repo, enforced at startup;
  2. `.gitignore` rules for databases, env files and real config names;
  3. a pre-commit check that blocks any staged file, symlink or content pointing into the data folder;
  4. gitleaks secret scanning on every commit and over full history in CI.
- **Separation.** Learning data (fit ratings, gaps, LLM output, skip reasons, notes) lives in `search.db`. Each EI claim's record is a separate file that holds only what EI needs and never reads `search.db`. See ADR-014.
- **The Telegram bot** answers one private chat only, enforced in code, and ignores everything else without replying. Its buttons are random single-use tokens, it polls (no open ports), and it refuses to start unless every check passes. Telegram isn't end-to-end encrypted, so what the bot sends is kept to a minimum: for each alerted posting its company, title, location, lane, fit word, number of gaps, schedule flag, up to three key requirements taken from the posting, and link; short references and status words; health messages that name a failing part but no host or path; and weekly counts by lane. It never sends notes, gap names, the resume, the model's text or EI record details. What you type or send to it, job descriptions included, does reach Telegram; it never sends a job description back. The links you send it are fetched only from public addresses, pinned against DNS rebinding. See ADR-015, ADR-016 and ADR-017.
- **Permissions.** `search.db`, the active claim's record and their WAL/SHM files are `0600`. A sealed earlier claim is `0400`. `careerhunter doctor` checks all of them.
- **Retention.** EI record files refuse deletes, to preserve a 6-year record for each claim. Mistakes are voided with a reason rather than removed, every edit is logged in an append-only audit table, and a sealed claim refuses every change. Exports are written next to the data and never overwritten.
- **What leaves the Mac.** Besides the bot's messages: one request per watched board, and per posting detail where the board needs it, to that board's public API (pinned to public addresses); the requests for each link you send the bot or `careerhunter log` (the page, and the ATS's public JSON when the page has no ad or job description), never to LinkedIn, which is not fetched at all; and, only if you set `discovery.searxng_url`, the site-restricted search queries your local SearXNG sends to the engines you enabled. See ADR-016 and ADR-020.
- **Local models.** Scoring and embeddings go to LM Studio on this Mac only, at fixed loopback addresses that config can't change, with no redirects. The scoring request carries the posting, your rules and, when imported, your resume text. See ADR-019 and ADR-023.
- **The resume** is read on this Mac, with no network, and its text is stored as `resume/current.txt` (`0600`, in a `0700` folder). `search.db` keeps only its SHA-256. Its text never goes to a log, Telegram, `search.db` or an EI record.
- **Tests and CI** use synthetic fixtures only (fictional companies and postings). No test opens a socket.
- **The demo** (`careerhunter demo`) runs on synthetic data in memory. It reads no data folder, writes no file and opens no socket.

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
