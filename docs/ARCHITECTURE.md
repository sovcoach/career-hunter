# Architecture

**In plain language:** career-hunter has two halves that never mix.

- **The code** lives in this git repository and contains nothing personal.
- **Your data** lives in one folder on an encrypted external drive: the databases, your real settings, saved copies of job ads, and exported reports.

Inside that folder, the data itself has two layers that never mix either. `search.db` holds everything career-hunter learns from: postings, fit ratings, skip reasons and the like. Each EI claim gets its own small record file, holding only what Service Canada may ask for. The two are linked by a random ID that means nothing on its own.

A small "storage guard" runs before every command and checks that the data folder is somewhere safe. Secrets, such as the future Telegram bot token, sit in the macOS Keychain, separate from both halves.

---

## Components

| Module | Responsibility |
|---|---|
| `cli.py` | `careerhunter` entry point. Runs the storage guard, then dispatches the command. Exit codes: 0 ok, 1 error, 2 refused by the guard. |
| `storage_guard.py` | Fail-closed checks on `CAREERHUNTER_DATA_DIR` (see below). Also creates the data folder safely. |
| `platform_checks.py` | `SystemProbe`: read-only wrappers around `diskutil info -plist`, `fdesetup status`, `git rev-parse` and `os.stat`. Tests swap in a fake. |
| `secrets_store.py` | Keychain access through `keyring`. Allowlisted names only, plaintext backends refused, and values never printed. |
| `config.py` | Validates `config.yaml` and `exclusions.yaml` and reports every problem at once. |
| `db.py` | SQLite connections (WAL, `busy_timeout=5000`, foreign keys on; or read-only), the migration runner with one track per kind of file, and a transaction helper. A run of pending migrations is one `BEGIN IMMEDIATE` transaction, statement by statement, with no write lock when nothing is pending (ADR-004, #51). |
| `resume.py` | Reads a PDF (pypdf) or DOCX (standard library) resume locally, with fixed errors and caps; imports its text to `resume/current.txt` with only the SHA-256 in `search.db`; and `load`, the only way scoring and embeddings read it (ADR-023). |
| `migrations/search/`, `migrations/ei_record/` | Schema history, one track per kind of file. Each migration is applied once, and its checksum is recorded so edits are detected. The runner in `db.py` also creates `schema_migrations` and `db_identity`, and their four no-update and no-delete triggers. A fresh `search.db` therefore has 6 triggers (2 from its `0001_init.sql`), and a fresh EI record has 32 (28 from its `0001_init.sql`). |
| `models.py` | Plain record values: `Posting`, `Application`, `Activity`, `Dating`. |
| `claims.py` | The `ei` config section, the per-precision claim check, creating each claim's file, the refusal to write while an earlier claim is unsealed, and sealing. |
| `ledger.py` | Writes decisions to both layers in a fixed order, builds the sanitized EI copy, and reconciles the two layers. |
| `ei_store.py` | Reads and writes on one EI record file. It imports no search code and opens nothing itself. |
| `search_store.py` | Reads and writes on `search.db`, including the posting identity rules. |
| `seed.py` | Validates `seed.yaml`, runs the claim check, then imports each record into its layer. Idempotent by natural keys. |
| `export.py` | Builds one claim's EI record for a date range as CSV and PDF, from that claim's file alone. |
| `bot.py` | The Telegram bot: the chat allowlist, commands, buttons and replies, writing through the ledger; fail-closed startup and the polling loop. On each successful poll it sends one alert per scored watcher posting with a fit, in lane order, and none while it can't read `config.yaml`. |
| `telegram.py` | A minimal Bot API client (standard library plus `truststore`) whose errors never carry the token. |
| `fetch.py` | Fetches a link safely: public addresses only, pinned to the checked address on every redirect hop. One 60-second deadline covers the lookup, every hop and the body, and the body is capped. Keeps a `Retry-After` header when the server sends one. |
| `deadline.py` | A total time limit on a monotonic clock, a body read in chunks that honors it and a size cap, and the per-connection watchdog that shuts a socket down when time runs out. |
| `loopback.py` | One HTTP exchange with a server on this Mac, used by scoring, embedding and discovery: loopback addresses only, no redirects, a total deadline and a size cap. Not `fetch.py`. |
| `runlog.py` | A run's log file in `logs/`: each line written as it happens, mode 0600, never through a symlink. |
| `filters.py` | The decision to keep, skip or flag a posting: exclusions, hard gates, location rules, role filters and schedule limits. A skip reason is a string. No fetching, scoring or alerts. |
| `lanes.py` | Matches a title to the best-priority lane in `search.lanes` (whole words, via `filters.has_phrase`), and orders and holds pending alerts by lane, fit and age. Never skips a posting and never reads `never_gated_families`. |
| `score.py` | Scores one kept posting on a local loopback model. The prompt is lanes, location rules, hard gates and `gap_tiers`. Returns gaps, model text and the tier name; the watcher stores them with the tier's `fit`. Leaves `fit_rating` null. Not `fetch.py`; it talks through `loopback.py` with a 120-second total. |
| `htmltext.py` | A board's HTML job description as plain text, with the standard library only: unescaped once for Greenhouse, scripts and styles dropped, block tags as line breaks. Stores nothing. |
| `watch.py` | One run of `careerhunter watch`: fetch Greenhouse, Lever and Ashby, filter, then score, within a 600-second budget. One company's failure is isolated. Stores each posting's fit and lane, and records the run in `watch_runs`. Writes `search.db` only. Does not call Telegram. |
| `snapshots.py` | Job-ad snapshots: the page, an ATS's public JSON, or a PDF or screenshot sent to the bot. |
| `service.py` | User LaunchAgents: the bot, kept running; `org.careerhunter.watch`, which runs `careerhunter watch` once per `schedule.watcher_interval_minutes` and exits; and, when `discovery.searxng_url` is set, `org.careerhunter.discover`, which runs `careerhunter discover` once per `discovery.interval_minutes` and exits. |
| `scripts/check_no_datadir_paths.py` | Pre-commit hook that blocks staged content pointing into the data folder. |
| `scripts/pre_push_guard.py` | Pre-push hook that refuses any push updating or deleting `main` (ADR-011). |
| `scripts/verify_tree.py` | `make verify-tree`: every hook and gitleaks over every tracked and untracked file. |

## Where data lives, and why

```
/Volumes/ExampleDrive/ExampleFolder/        ← CAREERHUNTER_DATA_DIR (mode 0700)
├── search.db (+ -wal, -shm)              ← search and learning layer (0600)
├── ei_record_2026-27.db (+ -wal, -shm)   ← the active claim's EI record (0600)
├── ei_record_2025-26.db                  ← an earlier claim, sealed (0400, no -wal/-shm)
├── config.yaml                           ← your search settings and EI claims (from config.example.yaml)
├── exclusions.yaml                       ← companies/keywords to skip
├── seed.yaml                             ← one-off import of past history
├── exports/                              ← EI reports, timestamped, never overwritten
├── snapshots/<YYYY>/                     ← copies of job ads (0600; ADR-017)
├── logs/                                 ← bot.log, bot.heartbeat, watch.log, discover.log, *.success (0600)
└── backups/
```

- **Outside the repo:** a git mistake can't leak data that isn't there. See DECISIONS ADR-001.
- **On an encrypted external volume:** the record is personal. The guard requires encrypted APFS and refuses anything else. See ADR-001 and ADR-002.
- **Paths inside the databases are relative** to the data folder (`snapshot_path` can't start with `/`), so renaming the drive doesn't break the record.
- **One EI file per claim**, so an earlier claim can be sealed read-only on its own and kept for 6 years from its own end date. See ADR-014.

## Storage guard sequence

```
CAREERHUNTER_DATA_DIR set and absolute?            ── no ──▶ refuse
resolve symlinks
inside this repo / contains this repo?             ── yes ─▶ refuse
inside ANY git work tree?                          ── yes ─▶ refuse
macOS?                                             ── no ──▶ refuse (can't verify)
nearest mount point == "/"? (drive not mounted)    ── yes ─▶ refuse
same st_dev as / or /System/Volumes/Data?          ── yes ─▶ refuse
diskutil: MountPoint matches, different APFS
  container from boot, Internal == false?          ── no ──▶ refuse
FilesystemType apfs, Encryption true, not Locked?  ── no ──▶ refuse
folder exists with mode 0700? (except `init`)      ── no ──▶ refuse
FileVault on boot disk?                            ── no ──▶ loud warning
```

`init` creates the data folder **without** creating missing parents. If `/Volumes/ExampleDrive` isn't mounted, the path would sit on the boot disk; the guard already refuses that, and the non-recursive `mkdir` is a second stop.

## Data model

**`search.db`**, the search and learning layer. It is editable, with no retention triggers:

```
companies 1──* postings 1──* applications 1──* decision_history
                    │              │
                    └──* skips     └── correlation_id, ei_claim ──▶ an EI record file
```

- Postings carry the learning fields: `fit_rating`, `gaps`, `llm_output`, `scout_metadata`. Applications carry `reasoning` and `notes`, and skips carry their `reason`.
- `postings.fit` (search migration 0009) is `low`, `medium` or `high`: the `fit` of the gap tier the posting was scored into, or null until it is scored and that tier's name is in config. `fit_rating` (0-10) is a different column and stays null.
- `postings.lane` and `postings.lane_checked` (search migration 0010) hold the lane a title matched, or no lane (off-lane) once checked. A trigger refuses any change once `lane_checked` is 1, so a lane is set once.
- `watch_runs` (search migration 0009) has one row per watch run: its start, its finish, `budget_reached`, the board counts and the held counts. An unfinished row has no counts (a CHECK), so a crashed run is never read as zero.
- **Posting identity:** the URL, else company + requisition ID, else company + title + location + month first seen.
- An application's `correlation_id` never changes. Its `ei_claim` can be set once, and is NULL when no claim was active or the application fell outside the claim.
- `watch_board_attempts` (search migration 0007) holds one row per board the watcher has tried: the key `<ats_type>:<slug>`, with the slug case-folded, and the UTC time of the last attempt. It decides which boards a run fetches first. It never touches `companies` or `postings`, and it adds no triggers.

**`ei_record_<label>.db`**, one per EI claim. It is sanitized and append-only:

```
claim (one row: label, start, end, sealed_at)
applications 1──* status_history          activities
edits        (audit log of every permitted UPDATE, written by triggers)
```

- **Only EI fields:** employer, role, location, work model, posting URL, date and precision, method, the snapshot and resume references, and the void columns. There is no fit, gap, LLM, skip, scout or notes column anywhere, and tests hold the schema to an exact allowlist.
- **Dates:** ISO text. Applications and activities carry a `date_precision`:
  - `exact`: a real date;
  - `week`: `week_of`, always a Monday;
  - `unknown`: neither.
  A CHECK constraint enforces the right combination, and triggers refuse dates outside the claim period, by the same per-precision rule as the app.
- **Enumerations** (source, work model, method, status, activity type, ATS type) are CHECK-constrained in SQL. `enums.py` mirrors them for friendly validation messages. The status pipeline is shared by both layers: `applied`, `screening`, `assessment_submitted`, `interview_scheduled`, `offer`, `hired`, `rejected`, `no_response`, `withdrawn`.
- **Idempotency keys:**
  - in `search.db`: company name (case-insensitive), the posting identity above, and an application as posting + date;
  - in an EI record: an application as employer + role + location + posting URL + date, and an activity as type + description + date or week.
- **Retention and corrections** (EI records):
  - Every table has a `BEFORE DELETE` trigger that aborts, and nothing cascades.
  - `status_history` and `edits` are append-only. Status changes are new rows, and the current status is the latest row.
  - Mistaken applications and activities are **voided**: they need a reason, stay in the file, are left out of exports, and are frozen afterwards.
  - Every permitted UPDATE is written column-by-column to `edits` by triggers, so nothing can change without a trail.
  - `id`, `correlation_id` and `created_at` never change. The claim's label and start never change.
  - Once a claim is **sealed**, triggers refuse every INSERT and UPDATE, and the file is read-only.

## Writing to both layers

```
             1. open search.db's transaction and write its rows (not committed yet)
search.db ◀──────────────────────────────────────────────────── ledger / seed
                                                                    │
ei_record_<active>.db ◀── 2. write the sanitized copy and COMMIT ───┤
                                                                    │
search.db ◀────────────── 3. COMMIT ────────────────────────────────┘
```

- A problem on the search side stops everything before the EI record is touched.
- If step 3 fails, the EI record stands and the command reports a partial write. `doctor` then shows the gap.
- A skip writes `search.db` only. With no active claim, applications go to `search.db` only.
- Before any of this, `init`, `migrate` and `seed` refuse to run while an earlier claim's file is unsealed, or a claim file isn't in config.

`doctor` reconciles the two layers in both directions. It opens each file on its own connection, read-only, and never attaches one to another.

## Data flow

```
seed.yaml ──validate + claim check──▶ seed.py ──▶ search.db             (companies, postings,
                                         │                               applications, decisions)
                                         └─────▶ ei_record_<active>.db  (sanitized applications,
                                                                         statuses, activities)

ei_record_<label>.db ──read-only──▶ export --claim <label> --from --to
                                         │
                                         ├─▶ exports/ei_<label>_<from>_<to>_<ts>_applications.csv
                                         ├─▶ exports/ei_<label>_<from>_<to>_<ts>_activities.csv
                                         └─▶ exports/ei_<label>_<from>_<to>_<ts>.pdf
```

Export inclusion rules:
- exact dates within the range;
- weeks that overlap the range;
- undated records always listed, in a labelled "Date unknown" group so nothing silently drops out.

CSV cells starting with `= + - @` are prefixed with `'` to stop spreadsheet formula injection.

## The watch pipeline

`careerhunter watch` reads the watchlist once, then, within one 600-second budget:

0. **Start the run record and fill in.** A `watch_runs` row is inserted. Scored, unalerted postings stored without a fit get their tier's fit from the current config, by the stored tier name; a name no longer in config is left without one. With `search.lanes` set, every posting not yet checked against the lanes, from any source, is checked (`... WHERE lane_checked = 0`).

1. **Order the boards.** Entries that cannot be fetched (no ATS type, Workday, a bad slug) come first, because they cost nothing. Then boards never tried, then the one tried longest ago, from `watch_board_attempts`. Ties keep config order.
2. **Fetch.** Greenhouse, Lever and Ashby list URLs are built from `ats_slug` and read through `fetch.py` (public addresses only). The same pass also fetches a company whose `source` is `discovery` and whose `watchlist` is 1, when the ATS type is one of those three and the slug is one path segment. A seeded watchlist row with any other source is not fetched by that extra path. The board's attempt time is written in its own transaction just before the fetch. A failed board is a log line and stores nothing. Greenhouse is fetched with `content=true` and Ashby with `includeCompensation=true`. A board body over 20 MB logs `board over the size cap (...); nothing recorded`, stores nothing from that board and counts as failed; nothing is cut to fit. Workday and the other ATS types are not fetched.
3. **Filter.** Each new posting is kept, skipped or flagged. The description is used only for that decision. Every board sends one: Greenhouse `content` as HTML turned into text with the standard library (`htmltext.py`); Lever's description, lists, closing text and salary description; Ashby's description and salary summary. It is not stored. With `search.lanes` set, the posting's lane is stored in the same transaction as the insert, skipped postings included. An off-lane posting is still gated and scored.
4. **Score what was just kept.** Same local model. A count at or above `exclude_from_gaps` is a `gap_tier` skip. The same pass stores an embedding cosine when `search.resume` is set. The description is used in memory for that embedding when the board sent one, and is not stored. Only new postings are scored here, and only while at least 240 seconds of the budget are left; a later new posting waits for step 7 with its description in memory. A stored posting whose score was made with another resume is queued for step 7, with its description in memory too.
5. **Refresh the embeddings.** Stored cosines are compared again with the resume, locally.
6. **Recheck.** Watcher postings that have no skip and no score are filtered again, so a config change can record a skip. Postings inserted this run were filtered already.
7. **Score what is waiting.** Only after the recheck has reached every row, in three passes: never-scored postings, then the queued re-scores, then re-scores of other stored postings from their stored fields. The never-scored pass is first come, first served: by `coalesce(score_attempted_at, created_at)`, then id, so a retry stamped at T goes ahead of everything stored after T. Each re-score pass goes by the last score attempt, never-tried first, then the oldest posting. A reply that does not parse leaves the posting as it was. A model that does not answer stops scoring for the rest of the run. An embedding for a row from stored fields uses those fields only, because the description was not kept. A posting already sent to the scorer this run is not sent again.
8. **Finish the run record.** The board counts, `budget_reached`, and the postings still held (unscored, scored with no fit, and off-lane below `search.off_lane_min_fit`) are written to the run's `watch_runs` row, then the last line. A run that never gets here keeps its row unfinished.

A skip is one `skips` row: `excluded_company`, `excluded_title`, `excluded_description`, `excluded_role`, `exclude_applied`, `required_language`, `credential`, `shift_pattern`, `pay_structure`, `seniority`, `min_salary`, `empty_location`, `location_rule`, `role_filter` or `schedule_limit`. Seniority words match whole words of the title, ignoring case; the description is checked too only when `seniority_in_description` is true. `title_exceptions` and `never_gated_families` match whole words of the title the same way, and a never-gated family is gated only by its title. `config.yaml` refuses a key under `hard_gates` that is not a known gate. `min_salary` applies only when it is a number, and only to figures in a pay context: a pay word in the sentence or a pay unit after the figure. Bonus, signing, relocation and equity amounts are left out, and so is a figure marked as a currency other than CAD (a plain `$` counts as CAD). Hourly figures are yearly at 2080 hours, daily at 260 days, weekly at 52 and monthly at 12. A posting is skipped only when the top of its highest range is below the minimum; one with no countable figure is kept. `max_commute_km` and `exclude_work_models` are validated and not applied.

`over_limit: flag` keeps a posting whose stated weekend-day count is above the cap and sets `postings.schedule` to `over_weekend_limit`. `exclude` writes `schedule_limit` instead. An on-call mention sets `on_call` on that same column and keeps the posting, unless the weekend count already excluded it. The word "weekends" on its own does neither.

With no work model from the board (Greenhouse), a location with the whole word "remote" is inferred remote, and the location rules otherwise treat the posting as on-site. On-site and hybrid must match `onsite_hybrid`, remote must match `remote`, and an inferred remote posting may match either list. With `search.location_rules` set, blank location text and no board work model is an `empty_location` skip; a board work model stands in for the location.

One `role_filters` block is AND only. It cannot express "this work model, or else that place." `work_models` checks the board's work model, or remote when it is inferred; an unknown work model does not fail it.

Scoring posts to `http://127.0.0.1:1234/v1/chat/completions` and nowhere else. The request carries the posting and, when one is imported and in use, the resume text. The model returns gaps. The program applies `gap_tiers` and writes `gaps`, `llm_output`, `scout_metadata` (`{"tier": "<name>"}`), `fit` (that tier's `fit` from config) and `scored_resume_sha256`, in one UPDATE. While a resume is in use, `llm_output` is canonical JSON of the parsed gaps, never the reply, and a reply that copies 40 characters of the resume into a gap, or has a gap over 120 characters, is treated as one that does not parse (ADR-023). `fit_rating` stays null. A later run scores those still-unscored rows after it has fetched the boards. A configured resume that cannot be used holds all scoring for the run. The watch run does not call Telegram and does not open an EI record.

On that same pass, when `search.resume` is `resume/current.txt` and that file matches its import, the watcher also asks `http://127.0.0.1:1234/v1/embeddings` for one vector of the resume and one vector of the posting (company, title, location, work model, and the description when the board sent one). The client is loopback only, follows no redirects, and is not `fetch.py`. The model id is `local-embedding`. Search migration 0004 stores the float32 BLOBs in `resume_embedding` and `posting_embeddings`, and the cosine in `postings.embedding_score`. The resume text and the posting text are not stored. A resume that is not imported, missing, or changed since its import, a down server, or a different dimension leaves that cosine null and adds one log line, `embedding miss posting <id>: <reason>`. The run still exits 0. The client has a 60-second total and its own 1 MB cap. A transport failure (refused, out of time, or too large) means nothing more is sent to the embedding server this run; a reply that arrives but is not a vector is a miss for that posting only. A cosine never causes a skip. When the resume bytes change, stored posting vectors are compared again locally and the model is not asked to embed those postings again. `fit_rubric.embedding_weight` is not read. The EI schema stays at version 1.

`careerhunter embed-eval` reads those stored cosines and tiers. It does not call the model. The report is `evals/embed-eval-<utc>.txt`, mode 0600, and the terminal prints that relative path only. Each row is a posting id, the tier name, the cosine, and applied or skipped. Spearman uses midranks and Pearson correlation on those ranks, from the standard library. The LLM side follows `gap_tiers`, best first, as 1 for the best tier and 0 for the worst. Precision at 5 and at 10 counts an application as a hit among postings that were applied to or skipped. Fewer than two comparable rows, or a rank with no spread, writes `undefined` and the command still exits 0. When `fit_rubric.embedding_weight` is set, the report also ranks `weight * cosine + (1 - weight) * tier`. That blend is not stored and not alerted.

`careerhunter discover` asks a local SearXNG when `discovery.searxng_url` is set. The client is loopback only, follows no redirects, and is not `fetch.py`. Each query is `site:` plus one title and one location, and one run sends at most 12. A hit is kept only when it reduces to a Greenhouse, Lever, or Ashby board, or to https on a host in `discovery.career_hosts`. Search migration 0005 stores that pending row in `discovery_candidates`: the kind, the reduced URL, the slug or host, and the status. The result title, the snippet, and the response body are not stored. The command does not fetch the page and does not open an EI record. Each query has a 30-second total. A `localhost` URL is looked up inside that time, and only a loopback answer is used. A down server, a non-200 status, a body that is not JSON or not UTF-8, a reply with no `results` list, or a non-empty list with no usable url writes `discovery miss: unreachable` and exits 0 with nothing stored. A result without a url is skipped. A null URL makes no request. A search run writes its lines to `logs/discover.log`: the notice, then `discover: <n> queued`.

`careerhunter discover approve` and `careerhunter discover reject` take one pending row id. They write `search.db` only: they do not rewrite `config.yaml` or `exclusions.yaml`, and they do not send Telegram. Approving a board inserts or fills a company with `watchlist` 1. A new company uses `source` discovery. An existing company's source stays as it is, and a stored ATS identity that disagrees is refused. Approving a career page stores `careers_url` with `watchlist` 0, and the watcher does not fetch it. Rejecting a row stores that status and inserts no company. From the board fetch on, the existing filter, score, cosine, and alert path owns the posting. The alert query still selects watcher postings only. The EI schema stays at version 1.

On each successful poll the bot also sends pending discovery rows that have not been sent, or were sent more than seven days ago. Up to five are one message each. More than five are one message of at most eight, and the rest wait for a later poll. The text is the suggested name and the reduced URL. Approve and Reject are opaque single-use buttons. A tap calls the same approve and reject functions and opens `search.db` only. It does not open an EI record. Search migration 0006 stores `notified_at` after Telegram accepts the message. A failed send leaves that column null. A refusal replies `Not changed.` and leaves the row pending.

The bot, on each successful poll, reads `config.yaml` once and sends one alert for each watcher posting that has a score, a fit, no skip, and no `alerted_at` (search migration 0003). The text is the company, the title and the link, then `Fit: High` (or `Medium`, `Low`), then `Lane: <name>` or `Off-lane` when lanes are configured, then `LLM: <tier>`, and `similarity: 0.82` when a cosine is stored. A missing cosine sends the LLM line only. With lanes, alerts go off-lane last, then by fit, best first, then non-fallback lanes before fallback ones, then lane priority, then the oldest (`first_seen`, then id); a stored lane no longer in config sorts as a fallback after every configured lane, before off-lane. An off-lane posting below `search.off_lane_min_fit` (default `high`) stays pending, and a posting the watcher has not lane-checked yet waits for its next run. Without lanes, alerts go by fit, then oldest, and none is held for its lane. The sort is done in Python, not SQL. If `config.yaml` can't be read, no watcher alert is sent that poll: the hold is recorded in `bot_state` (`alerts_held_since`), logged only when it starts and ends, and reported by `doctor`. A job ad sent with `/log` is alerted at once with `Fit: not scored` and no lane line; `/log` needs config for the date and the claim check, so with config unreadable it writes nothing and replies `Config can't be read, so nothing was recorded. Run careerhunter doctor.` Applied and Skipped are unchanged. When the stored company is a watchlist entry flagged `agency: true`, it is shown as `Name (agency)`, because no separate employer was stored. A failed send leaves `alerted_at` null and is tried again next poll. Quiet hours are not implemented.

`careerhunter service install` also installs `org.careerhunter.watch`. That agent runs `careerhunter watch` once and exits. launchd starts it again after `schedule.watcher_interval_minutes` (default 60, minimum 15). It has no `KeepAlive`. A run that exits 0 writes `logs/watch.success`. Board failures and a down model still exit 0, so they still count. A missing config or database does not update the stamp. `doctor` reports whether the agent is loaded and whether the stamp is within two intervals. Changing the interval means uninstalling the services and installing them again.

When `discovery.searxng_url` is set, `service install` also installs `org.careerhunter.discover`, including when the bot and watch plists are already there. It does not bootstrap those two again. A null URL does not install it, and install still stops when both of those plists exist. The agent runs `careerhunter discover` once and exits. launchd starts it again after `discovery.interval_minutes` (default 1440, minimum 60). It has no `KeepAlive`. A run that exits 0 writes `logs/discover.success`, including a miss and discovery off. Approve, reject, and list do not update that stamp, and write no log file. `doctor` reports the agent. A null URL with no plist is off. A null URL with a leftover plist is a warning to reinstall. A set URL with no plist is not installed. A loaded agent is compared with the configured interval, and a `discover.success` older than two intervals is a failure. A warning does not fail `doctor`. `service uninstall` removes whichever of the three plists exist.

### Run budget

One watch run has 600 seconds. The fetcher, the scoring and embedding clients and the run itself share that budget, and each call is also cut short by its own limit. The boards come first, and the refresh, the recheck and the waiting scores use what is left. Anything the budget does not reach is left for the next run:

- a board not reached is logged `run budget reached; left for the next run`, and its company and postings are untouched;
- a fetch the budget cuts short is left too, not failed, and its attempt time goes back to what it was, or its row is removed if it had none;
- a new posting stored after the budget ran out is logged as left and scored by a later run;
- a pass not reached, or cut partway, is logged with the number of rows left.

Nothing left over counts as fetched or failed. The last line is `watch: <n> boards fetched, <m> failed, <k> left`. A board refused for an ATS identity mismatch counts as failed.

### Failures

| What fails | What happens |
|---|---|
| One board, one recheck or scoring row, or the embedding refresh raises | Rolled back, `watch: <who>: <ExceptionType>; continuing`, and the run goes on |
| `sqlite3.IntegrityError` in one of those | The same: isolated, and the run goes on |
| Any other database error, at any point | Rolled back; abort line in the log; exit 1, no success stamp |
| `config.yaml` missing or invalid, `search.db` missing | Abort line in the log; exit 1, no success stamp |
| The run log cannot be opened or written | `error: run log not writable: …` on stderr; exit 1, no success stamp |
| The storage guard refuses | Nothing is opened; exit 2 |

### Run logs

`careerhunter watch` and a search run of `careerhunter discover` write each line to `logs/watch.log` or `logs/discover.log` the moment it is produced, then to stderr. launchd discards stderr, so the file is what keeps a trace. It is opened only after the storage guard passes, without following a symlink, and must be a regular file at mode 0600. A crash leaves its traceback in that file only, never on stderr. See ADR-022.

## The Telegram bot (Phase 2)

```
your phone ──▶ Telegram's servers ◀── getUpdates (long poll) ── careerhunter bot run   (LaunchAgent)
                                                                  │  allowlist: one private chat
                                                                  │  buttons: single-use tokens (search.db)
                                                                  ├─▶ ledger ─▶ ei_record_<active>.db, then search.db
                                                                  ├─▶ fetch.py (public, pinned) ─▶ snapshots/
                                                                  └─▶ logs/bot.log, logs/bot.heartbeat
```

- No inbound port and no webhook: the bot only ever calls out to Telegram, and to the job-ad links it is given.
- Every write goes through the same ledger functions as the CLI, after the same claim checks (ADR-014). The bot re-reads config.yaml before each write.
- What it sends is minimal (ADR-015); what it trusts is limited (ADR-016); how it saves job ads is in ADR-017.
- `doctor` reports the chat setting, whether the bot service is running, how recent its heartbeat is, whether the watch agent is loaded, and the age of the last successful watch.
- It also reports whether lane checking is on (`lanes --  not configured` when `search.lanes` is unset, so it is never off silently); a watch run left unfinished for more than the budget plus five minutes (BAD); the held counts from the last finished run, with a WARN when a scored posting's tier name has no fit in config; and whether the bot is holding watcher alerts because it couldn't read `config.yaml` (BAD).

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| 1, Foundation (v0.1.0) | Storage guards, Keychain helper, schema and migrations, seed import, EI export, CI, docs | done |
| 1.5, Data layer split (v0.2.0) | `search.db` and one EI record file per claim, a correlation ID, write order and reconciliation, claim config and sealing | done |
| 2, Telegram bot (v0.3.0) | The bot (alerts with Applied/Skipped, `/log`, `/status`, `/activity`, `/recent`), job-ad snapshots, the launchd service, the fit-criteria schema (validation only), and repo hygiene | **this release** |
| 3, Watcher | Fetch watchlisted companies' ATS boards; job-ad snapshots for the EI record; fit rating against the config rubric; skips with reasons | planned |
| 4, Discovery | Find new companies and postings beyond the watchlist | planned |
| 5, Release | License, polish | planned |

Each phase lands as a pull request against `main` with its own milestone, and gets a semver tag when merged. The repo goes public once Phase 1 is verified, and `main` is protected straight after (ADR-012).
