# career-hunter

**In plain language:** career-hunter is a private, self-hosted job-search assistant that runs on your own Mac. It keeps a careful record of every job you apply to and every other job-search activity, such as networking, job fairs, agency registrations and courses. When you need to show your job search, for example when Service Canada asks for it during an Employment Insurance (EI) claim, it produces a clean report as a PDF and a spreadsheet. It can also watch boards you name, score the postings it keeps, and message you on Telegram.

Your information never leaves your machine and is never stored in this code repository. The data lives on an encrypted external drive, and secrets live in the macOS Keychain.

> **Status:** Phase 2 (Telegram bot), version 0.3.0. Tracking, import and export work, with one EI record per claim kept apart from everything else, and a private Telegram bot records applications, statuses and activities from your phone. `careerhunter watch` records new postings from Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR and Workday boards, filters them, and scores the ones it keeps with a local model. The bot sends one alert digest per watch run.

---

## Who it's for

- **Job seekers** who want one trustworthy record of their search instead of a pile of emails and browser tabs, and who may need to prove that search for EI.
- **Developers and reviewers** looking at how a small personal-data tool can be built carefully: fail-closed storage guards, secrets in the OS keychain, append-only records, CI and decision records.
- **Friends** who want to run their own copy. Everything personal lives in config, so you can clone it and use it for your own search.

## What it does

| Command | What it does |
|---|---|
| `careerhunter init` | Creates your private data folder on the encrypted drive, copies in starter config files, and creates the databases |
| `careerhunter seed` | Imports your existing applications and activities from a simple YAML file, each into the right place. Re-running is safe: nothing is duplicated |
| `careerhunter log <link> --method … --date …` | Records a job you found and applied to yourself in one step: the posting and the application go into both files under one ID, with a copy of the ad when the page gives one. `--company` and `--title` fill in what the page doesn't show |
| `careerhunter export --claim … --from … --to …` | Writes one EI claim's job-search record for a date range as a PDF plus two CSV spreadsheets |
| `careerhunter claim seal …` | Permanently closes a finished claim's record and makes it read-only |
| `careerhunter secret set/verify` | Stores a secret (e.g. the future Telegram bot token) in the macOS Keychain without ever showing it |
| `careerhunter bot run` / `service install` | Runs the Telegram bot, and installs it plus a watch LaunchAgent that runs on the configured interval and exits. Discovery runs inside the watch agent; install removes the separate discovery agent of earlier versions |
| `careerhunter doctor` | Checks the setup and tells you what's missing |
| `careerhunter watch` | Fetches the Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR and Workday boards named on the watchlist, and approved discovery boards, once, after a discovery pass when one is due. It records new postings in `search.db`, skips or flags them from the config, and scores the ones it keeps with a local model. It does not call Telegram. The watch LaunchAgent runs this command on a schedule. The bot sends one alert digest per watch run |
| `careerhunter scout-prompt` | Prints a scout prompt from exclusions and the hard gates the watcher skips on. Writes nothing |
| `careerhunter embed-eval` | Ranks stored cosines against LLM tiers. Writes a private report under `evals/` and prints only that relative path |
| `careerhunter discover` | Runs one discovery pass against a local SearXNG by hand; the watch agent runs one each `discovery.interval_minutes`. A posting URL it finds is alerted through the watch run's digest; a board is queued in `search.db` once one board-API request confirms it. `approve` and `reject` update that queue only. The bot can approve or reject a pending row from the allowlisted chat. That message is the suggested name and the URL. An approved board is fetched on the next `careerhunter watch` |

## What it does *not* do (yet)

- Quiet hours are not built. `careerhunter watch` does not call Telegram. The bot sends one digest per watch run when it polls, at most `alerts.max_per_poll` postings per poll, in lane order. Each row has company, title, location, lane (or `Off-lane`), fit word, the number of gaps, the schedule flag in words, up to three key requirements taken from the posting, and the link, with its own Applied and Skipped buttons. Gap names never leave the Mac. Postings without a fit, and off-lane ones below `search.off_lane_min_fit`, are held. Scoring and embeddings stay on this Mac, at a loopback address.
- No automatic deletes, ever. Records are kept for at least the 6 years EI requires.
- No cloud sync and no accounts. It is a local tool.

## How it works at a glance

```
           code (this repo)                          your data (never in git)
  ┌──────────────────────────────┐          ┌───────────────────────────────────────┐
  │ careerhunter CLI             │  guard   │ /Volumes/<encrypted drive>/            │
  │  ├─ storage guard  ──────────┼──checks──▶   ExampleFolder/                      │
  │  ├─ seed import              │          │     search.db  ei_record_<claim>.db   │
  │  ├─ EI export (PDF + CSV)    │          │     config.yaml  exclusions.yaml      │
  │  └─ Keychain helper ─────┐   │          │     seed.yaml   exports/  snapshots/  │
  └──────────────────────────┼───┘          └───────────────────────────────────────┘
                             └──▶ macOS Keychain (secrets)
```

Inside the data folder, `search.db` keeps everything career-hunter learns from (postings, fit ratings, skip reasons). Each EI claim has its own record file holding only what Service Canada may ask for. The two are linked by a random ID that means nothing on its own, and the EI record never reads `search.db`.

Every command starts with the **storage guard**. It refuses to run unless:

- `CAREERHUNTER_DATA_DIR` is set;
- that folder is nowhere near a git repository;
- it is on a mounted external volume, separate from the boot disk;
- that volume is encrypted APFS.

If something isn't right, it stops and explains why rather than guessing.

### Data separation, in brief

1. The app fails closed on the data location, as described above.
2. Only templates (`*.example.yaml`) are committed. Real config, databases, exports and snapshots live in the data folder.
3. `.gitignore` blocks databases, env files, data folders and real config names, as a second layer.
4. Pre-commit hooks run **gitleaks** (secret scanning) and a custom check. The custom check blocks any commit containing a file, symlink or path that resolves into the data folder.
5. CI repeats the secret scan across the full git history on every pull request.

### Secrets, in brief

Secrets go in the **macOS Keychain** through the Python `keyring` library. They never go in files, `.env` files, environment variables or the repo. `careerhunter secret set` reads the value with hidden input, and `secret verify` only ever says "present (N chars)" or "missing". If the Keychain backend isn't available, the command fails instead of falling back to a file.

## Get started

- **Setting it up?** Follow [docs/SETUP.md](docs/SETUP.md). It assumes no developer background.
- **Curious how it's built?** See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Why it's built this way?** See [docs/DECISIONS.md](docs/DECISIONS.md).
- **Contributing or running it locally?** See [CONTRIBUTING.md](CONTRIBUTING.md).
- **Security and personal data:** see [SECURITY.md](SECURITY.md).
- **What changed:** see [CHANGELOG.md](CHANGELOG.md).

## Tech

Python 3.12+ managed with [uv](https://docs.astral.sh/uv/), SQLite, PyYAML, keyring and ReportLab. Quality checks: ruff, mypy (strict), pytest with coverage, gitleaks, pre-commit, and GitHub Actions.

A license will be added in Phase 5.
