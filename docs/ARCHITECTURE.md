# Architecture

**In plain language:** career-hunter has two halves that never mix.

- **The code** lives in this git repository and contains nothing personal.
- **Your data** lives in one folder on an encrypted external drive: the databases, your real settings, saved copies of job ads, and exported reports.

Inside that folder, the data itself has two layers that never mix either. `search.db` holds everything career-hunter learns from: postings, fit ratings, skip reasons and the like. Each EI claim gets its own small record file, holding only what Service Canada may ask for. The two are linked by a random ID that means nothing on its own.

A small "storage guard" runs before every command and checks that the data folder is somewhere safe. Secrets, such as the Telegram bot token, sit in the macOS Keychain, separate from both halves.

---

## Components

| Module | Responsibility |
|---|---|
| `cli.py` | `careerhunter` entry point. Runs the storage guard, then dispatches the command; `demo` alone is dispatched first, because it uses no data folder. Exit codes: 0 ok, 1 error, 2 refused by the guard. |
| `demo.py` | `careerhunter demo`: an in-memory `search.db` and EI record, a synthetic config, fake boards and a fake local model, through the real `watch.run`, `bot.digest_texts` and `ledger.record_application`. Reads no data folder, writes no file, opens no socket. |
| `storage_guard.py` | Fail-closed checks on `CAREERHUNTER_DATA_DIR` (see below). Also creates the data folder safely. |
| `platform_checks.py` | `SystemProbe`: read-only wrappers around `diskutil info -plist`, `fdesetup status`, `git rev-parse` and `os.stat`. Tests swap in a fake. |
| `secrets_store.py` | Keychain access through `keyring`. Allowlisted names only, plaintext backends refused, and values never printed. |
| `config.py` | Validates `config.yaml` and `exclusions.yaml` and reports every problem at once. |
| `db.py` | SQLite connections (WAL, `busy_timeout=5000`, foreign keys on; or read-only), the migration runner with one track per kind of file, and a transaction helper. A run of pending migrations is one `BEGIN IMMEDIATE` transaction, statement by statement, with no write lock when nothing is pending (ADR-004, #51). |
| `pdftext.py` | The text of a PDF with pypdf, under caps the caller sets: an encrypted PDF is refused before any page is read, failures are fixed reasons, and nothing pypdf logs leaves it. Shared by `resume.py` and `jobdesc.py`. |
| `resume.py` | Reads a PDF (through `pdftext.py`) or DOCX (standard library) resume locally, with fixed errors and caps; imports its text to `resume/current.txt` with only the SHA-256 in `search.db`; and `load`, the only way scoring and embeddings read it (ADR-023). |
| `migrations/search/`, `migrations/ei_record/` | Schema history, one track per kind of file. Each migration is applied once, and its checksum is recorded so edits are detected. The runner in `db.py` also creates `schema_migrations` and `db_identity`, and their four no-update and no-delete triggers. A fresh `search.db` therefore has 8 triggers (2 from its `0001_init.sql`, 1 from 0010 and 1 from 0020), and a fresh EI record has 37 (28 from its `0001_init.sql` and 5 from 0002). |
| `models.py` | Plain record values: `Posting`, `Application`, `Activity`, `Dating`. |
| `claims.py` | The `ei` config section, the per-precision claim check, creating each claim's file, the refusal to write while an earlier claim is unsealed, and sealing. |
| `records.py` | Voids a mistaken application or activity with a reason, never deleting it; the schema's audit triggers log the change. |
| `ledger.py` | Writes decisions to both layers in a fixed order, builds the sanitized EI copy, records and later attaches job descriptions, and reconciles the two layers, `jd_expected`, the current JD and the canonical link included. |
| `selfsourced.py` | A self-sourced application in one step, for `careerhunter log` and the bot's `/log`: stores the link in its canonical form and matches a stored one canonically, reads the page for the company and title (never a LinkedIn page, which then needs both given), saves the job-ad copy and the JD's file, fetching the JD when none was given, and writes the posting, the application and the JD through one `ledger.record_application` call, under one correlation ID (ADR-024). |
| `urls.py` | `canonical(url)`: a posting link without tracking parameters or fragment, LinkedIn job links as `https://www.linkedin.com/jobs/view/<id>/`, idempotent. `no_fetch(url)`: LinkedIn (`linkedin.com`, its subdomains, `lnkd.in`), which `fetch.py` refuses for every caller. |
| `jobdesc.py` | A job description from a paste (verbatim), a PDF, HTML or text file (its type judged from its bytes), or an allowed fetch (JSON-LD or the ATS's JSON); its text hash; and its owner-only snapshot file (ADR-024). |
| `report.py` | `careerhunter report`: the one code path that attaches a claim's EI record to `search.db`, both read-only, to join them on `correlation_id` into a private report (ADR-014 as amended). Each row shows the canonical link and a JD reference. |
| `ei_store.py` | Reads and writes on one EI record file. It imports no search code and opens nothing itself. |
| `search_store.py` | Reads and writes on `search.db`, including the posting identity rules. |
| `seed.py` | Validates `seed.yaml`, runs the claim check, then imports each record into its layer. Idempotent by natural keys. |
| `backfill.py` | `careerhunter import`: reads a version 1 JSON file from `imports/` only (symlinks followed, its `jd_file`s too), validates it by hand, and gives each entry one class (create, attach, unchanged, ambiguous, conflict, refused) by `titles.py` matching against `search.db`. `plan` reads only, for `--dry-run`; `apply` writes exactly the plan, a new application through `selfsourced.record` with a fetcher that refuses everything, a JD or an empty link through `ledger.attach_job_description`. No SQL of its own. |
| `titles.py` | One normalization for titles and employers: `canonical` (`filters.title_key` with one comma inversion, `Analyst, Client Experience` as `client experience analyst`), `employer_key` (no leading "The", no Inc, Ltd, LLC, Corp, Co …) and `equivalent` (titles that differ only by level words at either end, or by specialist, analyst, associate). |
| `export.py` | Builds one claim's EI record for a date range as CSV and PDF, from that claim's file alone, with a JD reference per application, and writes the JDs and their original files into a private folder beside them. |
| `bot.py` | The Telegram bot: the chat allowlist, commands, buttons and replies, writing through the ledger; fail-closed startup and the polling loop. On each successful poll it sends one alert digest per watch run, capped by `alerts.max_per_poll`, in lane order; one failure and one recovery message per component outage (#42); and, at `alerts.weekly_digest` in `schedule.timezone`, the weekly counts by lane (#43). It sends none of these while it can't read `config.yaml`. |
| `telegram.py` | A minimal Bot API client (standard library plus `truststore`) whose errors never carry the token. Its errors say which kind of failure it was: no answer, HTTP 429, refused, or a reply that isn't the Bot API's JSON. |
| `fetch.py` | Fetches a link safely: public addresses only, pinned to the checked address on every redirect hop, and never a LinkedIn host (`urls.no_fetch`), on the first URL or any hop. One 60-second deadline covers the lookup, every hop and the body, and the body is capped. Keeps a `Retry-After` header when the server sends one. A board-API GET asks for `application/json` (`accept=JSON`); that is a hint only, and the caller's JSON parse is the gate. `post_json` sends one JSON POST the same way (for Workday's job list) and does not follow a redirect. |
| `deadline.py` | A total time limit on a monotonic clock, a body read in chunks that honors it and a size cap, and the per-connection watchdog that shuts a socket down when time runs out. |
| `loopback.py` | One HTTP exchange with a server on this Mac, used by scoring, embedding and discovery: loopback addresses only, no redirects, a total deadline and a size cap. Not `fetch.py`. |
| `runlog.py` | A run's log file in `logs/`: each line written as it happens, mode 0600, never through a symlink. |
| `filters.py` | The decision to keep, skip or flag a posting: exclusions, hard gates, location rules, role filters and schedule limits. A skip reason is a string. No fetching, scoring or alerts. |
| `lanes.py` | Matches a title to the best-priority lane in `search.lanes` (whole words, via `filters.has_phrase`), and orders and holds pending alerts by lane, fit and age. Never skips a posting and never reads `never_gated_families`. |
| `embed.py` | One embedding of the resume and of a posting on the local model (`scoring.embedding_model`), through `loopback.py`, and the cosine stored beside the tier. A cosine never causes a skip (ADR-019). |
| `embed_eval.py` | `careerhunter embed-eval`: ranks stored cosines against stored tiers into a private report under `evals/`. Calls no model. |
| `discover.py` | A discovery pass on a local SearXNG through `loopback.py`: the query cursor and pacing, the backoff, posting URLs and board candidates from the results, and the approve and reject rules (ADR-020). Stores no title, snippet or body. |
| `scout_prompt.py` | `careerhunter scout-prompt`: a prompt from the exclusions and the hard gates the watcher skips on. Prints only. |
| `score.py` | Scores one kept posting on a local loopback model. The prompt is lanes, location rules, hard gates and `gap_tiers`. Returns gaps, model text and the tier name; the watcher stores them with the tier's `fit`. Leaves `fit_rating` null. Not `fetch.py`; it talks through `loopback.py` with a 120-second total. |
| `htmltext.py` | A board's HTML job description as plain text, with the standard library only: unescaped once for Greenhouse, scripts and styles dropped, block tags as line breaks. Stores nothing. |
| `watch.py` | One run of `careerhunter watch`: a discovery pass when due and the postings it found, then the boards, filter, posting age, then score, within a 600-second budget. One company's failure is isolated. Stores each posting's fit, lane and posted date, and records the run in `watch_runs`. Writes `search.db` only. Does not call Telegram. |
| `ats.py` | The requests for one board and its jobs parsed from the replies: Greenhouse, Lever and Ashby lists; SmartRecruiters, BambooHR and Workday lists, then one detail per new posting; one posting by itself; and one request to confirm a discovered board. One byte budget per board. `Client.post_json` sends only to a subdomain of `myworkdayjobs.com` over https and refuses any other host before it is looked up, on top of the fetcher's address pinning. Field names checked against public docs or live public boards. |
| `snapshots.py` | Job-ad snapshots: the page or an ATS's public JSON. A JD's file is saved here too, and becomes the snapshot when there is none (ADR-024); a PDF or screenshot is still taken in reply to a request for the ad sent before 0.5.0. |
| `service.py` | User LaunchAgents: the bot, kept running; `org.careerhunter.watch`, which runs `careerhunter watch` once per `schedule.watcher_interval_minutes` and exits; discovery runs inside that watch agent. Install retires `org.careerhunter.discover` from earlier versions. |
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
├── imports/                              ← `careerhunter import` files and their JD files, read from here only (0700)
├── exports/                              ← EI reports, timestamped, never overwritten; each with a <stem>-job-descriptions/ folder (0700) when it has JDs
├── snapshots/<YYYY>/                     ← copies of job ads and job descriptions (0600; ADR-017, ADR-024)
├── logs/                                 ← bot.log, bot.heartbeat, watch.log, discover.log, *.success (0600)
├── resume/current.txt                    ← the imported resume's text (folder 0700, file 0600; ADR-023)
├── reports/                              ← `careerhunter report` output (0600), made on first use
├── evals/                                ← `careerhunter embed-eval` reports (0600), made on first use
└── backups/                              ← made by `init`; SETUP's dated upgrade backups go here
```

`doctor` checks `exports/`, `snapshots/`, `logs/` and `imports/`, and `resume/` and `reports/` once they exist, and tightens anything others could read. It does not check `evals/` or `backups/`.

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
- `postings.key_requirements` and `postings.score_run_id` (search migration 0011): up to three short requirements the model took from the posting (a JSON array, or null), and the `watch_runs` row of the run that last gave the posting its score or fit. The digest sends the first and groups by the second.
- `companies.ats_host` and `companies.ats_site` (search migration 0012): a Workday board's wdN host and site, null for every other ATS. The same migration rebuilds `discovery_candidates` so a queued board can be any of the six ATS types the watcher reads, carry Workday's host and site, and have the status `gone`; nothing references that table, so the rebuild needs no foreign-key pragma.
- `postings.posted_date` (search migration 0013): the posted date the board gave, in UTC, or null.
- `applications.jd_expected`, `jd_sha256` and `jd_path` (search migration 0020): whether a JD was expected (0 for applications from before it, fixed by a trigger), the current JD's text hash, and its snapshot file. The text itself is only in that file and in the EI record (ADR-024).
- `postings.ats_key` (search migration 0015, unique when set): `<ats_type>:<board>:<posting id>`, the posting's identity on its board, set when the watcher stores or meets it. Discovery checks it before fetching a posting, so a Greenhouse posting whose `absolute_url` is on the company's own domain is still known. The watcher matches a stored posting on it before the URL, and a posting whose key is stored gets no detail request. A Workday key is `workday:<tenant>:<host>:<site>:<id>`, where `<id>` is the trailing `_<id>` of the `externalPath`, as it is: on a live public board that is the tail of `jobPostingInfo.jobPostingId` and begins with `jobReqId` (`JREQ205219-7` for `JREQ205219`), so two postings of one requisition stay apart and a retitled or moved posting keeps its key (not verified across a real retitle). A path without that tail keys on the whole path. Search migration 0019 clears Workday keys stored in the older whole-path form; the watcher sets the new one when it next meets the posting by its URL.
- `skips.by_operator` (search migration 0016) is 1 for a skip the operator chose with the bot's Skipped button and 0 for one the watcher wrote. Only an operator skip suppresses a repost (`already_handled`). Skips stored before 0016 keep 0, because their reason text can't tell the two apart; `reason` stays free text.
- `alert_refusals` (search migration 0017): one row per posting Telegram refused when it was sent alone, with the count, the last refusal and, after three, when the bot gave it up. The row goes once Telegram accepts the posting. Transport failures, bad replies and HTTP 429 are not counted.
- `discovery_state` (search migration 0014), one row: the discovery cursor, when the last pass was claimed, and the failed passes in a row with the backoff they set. Search migration 0018 adds `useful_at`, the last pass that got a usable reply to every query, and `unproductive`, the passes in a row that ran without one.
- `watch_runs.useful` (search migration 0018): 1 when a finished run fetched and parsed at least one board and, with postings waiting, reached the scorer; 0 when it did not; null while unfinished, and for runs finished before 0018 (unknown, counted as neither).
- `component_health` (search migration 0018), one row each for `lm_studio`, `searxng` and `boards`: the runs in a row that failed it, the kind of the last failure, and the postings held for scoring then, written by the watcher; and what the bot has told the operator about the current outage (`alerted_failures`, `recovery_due`), written by the bot.
- `watch_runs` (search migration 0009) has one row per watch run: its start, its finish, `budget_reached`, the board counts and the held counts. An unfinished row has no counts (a CHECK), so a crashed run is never read as zero.
- **Posting identity:** the URL, else company + requisition ID, else company + title + location + month first seen.
- An application's `correlation_id` never changes. Its `ei_claim` can be set once, and is NULL when no claim was active or the application fell outside the claim.
- `watch_board_attempts` (search migration 0007) holds one row per board the watcher has tried: the key `<ats_type>:<slug>`, with the slug case-folded, and the UTC time of the last attempt. It decides which boards a run fetches first. A Workday key is `workday:<tenant>:<host>:<site>`. It never touches `companies` or `postings`, and it adds no triggers.

**`ei_record_<label>.db`**, one per EI claim. It is sanitized and append-only:

```
claim (one row: label, start, end, sealed_at)
applications 1──* status_history          activities
applications 1──* job_descriptions        (EI migration 0002; append-only)
edits        (audit log of every permitted UPDATE, written by triggers)
```

- **Only EI fields:** employer, role, location, work model, posting URL, date and precision, method, the snapshot and resume references, `jd_expected`, the void columns, and each job description (its text, hash, `jd_source` and file). There is no fit, gap, LLM, skip, scout or notes column anywhere, and tests hold the schema to an exact allowlist.
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
  - `status_history`, `job_descriptions` and `edits` are append-only. Status changes are new rows, and the current status is the latest row; so is the current JD. `jd_expected` never changes.
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

`doctor` reconciles the two layers in both directions. It opens each file on its own connection, read-only, and never attaches one to another (only `careerhunter report` does, read-only). An application on one side only, or linked to a claim with no file, is an `orphan` line; one on both sides that disagrees is a `reconcile` line. Both are BAD, and doctor fixes neither: it says so in one more line, and the files are left byte for byte as they were.

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

0.5. **Discover, when due.** With `discovery.searxng_url` set and no pass claimed in the last `discovery.interval_minutes`, one discovery pass runs first, with at most 150 seconds of the budget, isolated like a board (ADR-020). The posting URLs it found are then fetched once each through their ATS's API, at most 20, and recorded with `source` discovery through steps 3 and 4 like a board's postings.

1. **Order the boards.** Entries that cannot be fetched (no ATS type, a Workday entry without its host and site, a bad slug) come first, because they cost nothing. Then boards never tried, then the one tried longest ago, from `watch_board_attempts`. Ties keep config order.
2. **Fetch.** Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR and Workday boards are read through `fetch.py` (public addresses only) from list URLs built from `ats_slug`, and for Workday from the tenant, `ats_host` and `ats_site` too (`ats.py`). Every GET asks for `application/json`; a reply that is not the board's JSON, an HTML 200 included, fails the board. Workday's list is a JSON POST (`fetch.post_json`), 20 rows a page, to a `myworkdayjobs.com` subdomain only. Paging stops at a short page, at 25 pages, or at a page whose `externalPath`s are the page before's (a board ignoring the offset); the last two keep what was read and log a note, and the board counts as fetched. SmartRecruiters, BambooHR and Workday lists have no descriptions, so each posting not stored yet gets one detail request, at most 40 per board per run; a detail that fails with another status or a bad body leaves out that posting only, and a fetch error or HTTP 429 stops the board's details and counts it as failed, keeping what was read. The same pass also fetches a company whose `source` is `discovery` and whose `watchlist` is 1, when its ATS type is one of these and its identity is usable. A seeded watchlist row with any other source is not fetched by that extra path. The board's attempt time is written in its own transaction just before the fetch. A failed board is a log line and stores nothing. Greenhouse is fetched with `content=true` and Ashby with `includeCompensation=true`. All of a board's replies together over 20 MB log `board over the size cap (...); nothing recorded`, store nothing from that board and count as failed; nothing is cut to fit. Other ATS types are not fetched.
3. **Filter.** Each new posting is kept, skipped or flagged, then checked against the posting age gate. The description is used only for that decision. Every board sends one: Greenhouse `content` as HTML turned into text with the standard library (`htmltext.py`); Lever's description, lists, closing text and salary description; Ashby's description and salary summary; the SmartRecruiters job ad sections, the BambooHR description and the Workday job description from their detail requests. It is not stored. With `search.lanes` set, the posting's lane is stored in the same transaction as the insert, skipped postings included. An off-lane posting is still gated and scored.
4. **Score what was just kept.** Same local model. A count at or above `exclude_from_gaps` is a `gap_tier` skip. The same pass stores an embedding cosine when `search.resume` is set. The description is used in memory for that embedding when the board sent one, and is not stored. Only new postings are scored here, and only while at least 240 seconds of the budget are left; a later new posting waits for step 7 with its description in memory. A stored posting whose score was made with another resume is queued for step 7, with its description in memory too.
5. **Refresh the embeddings.** Stored cosines are compared again with the resume, locally.
6. **Recheck.** Watcher and discovery postings that have no skip and no score are filtered again, age gate included, so a config change or time passing can record a skip. Postings inserted this run were filtered already.
7. **Score what is waiting.** Only after the recheck has reached every row, in three passes: never-scored postings, then the queued re-scores, then re-scores of other stored postings from their stored fields. The never-scored pass is first come, first served: by `coalesce(score_attempted_at, created_at)`, then id, so a retry stamped at T goes ahead of everything stored after T. Each re-score pass goes by the last score attempt, never-tried first, then the oldest posting. A reply that does not parse leaves the posting as it was. A model that does not answer stops scoring for the rest of the run. An embedding for a row from stored fields uses those fields only, because the description was not kept. A posting already sent to the scorer this run is not sent again.
8. **Finish the run record.** The board counts, `budget_reached`, and the postings still held (unscored, scored with no fit, and off-lane below `search.off_lane_min_fit`) are written to the run's `watch_runs` row, then the last line. A run that never gets here keeps its row unfinished.

A skip is one `skips` row: `excluded_company`, `excluded_title`, `excluded_description`, `excluded_role`, `exclude_applied`, `required_language`, `credential`, `shift_pattern`, `pay_structure`, `seniority`, `min_salary`, `empty_location`, `location_rule`, `role_filter`, `schedule_limit`, `too_old`, or `already_handled` for a repost of a role the operator logged or skipped (same company row and `filters.title_key`; see ADR-005). Seniority words match whole words of the title, ignoring case; the description is checked too only when `seniority_in_description` is true. `title_exceptions` and `never_gated_families` match whole words of the title the same way, and a never-gated family is gated only by its title. `config.yaml` refuses a key under `hard_gates` that is not a known gate. `min_salary` applies only when it is a number, and only to figures in a pay context: a pay word in the sentence or a pay unit after the figure. Bonus, signing, relocation and equity amounts are left out, and so is a figure marked as a currency other than CAD (a plain `$` counts as CAD). Hourly figures are yearly at 2080 hours, daily at 260 days, weekly at 52 and monthly at 12. A posting is skipped only when the top of its highest range is below the minimum; one with no countable figure is kept. `max_commute_km` and `exclude_work_models` are validated and not applied.

`over_limit: flag` keeps a posting whose stated weekend-day count is above the cap and sets `postings.schedule` to `over_weekend_limit`. `exclude` writes `schedule_limit` instead. An on-call mention sets `on_call` on that same column and keeps the posting, unless the weekend count already excluded it. The word "weekends" on its own does neither.

With no work model from the board (Greenhouse), a location with the whole word "remote" is inferred remote, and the location rules otherwise treat the posting as on-site. On-site and hybrid must match `onsite_hybrid`, remote must match `remote`, and an inferred remote posting may match either list. With `search.location_rules` set, blank location text and no board work model is an `empty_location` skip; a board work model stands in for the location.

One `role_filters` block is AND only. It cannot express "this work model, or else that place." `work_models` checks the board's work model, or remote when it is inferred; an unknown work model does not fail it.

Scoring posts to `http://127.0.0.1:1234/v1/chat/completions` and nowhere else, naming the model `scoring.chat_model` (default `local-model`). The request carries the posting and, when one is imported and in use, the resume text. The model returns gaps. The program applies `gap_tiers` and writes `gaps`, `llm_output`, `scout_metadata` (`{"tier": "<name>"}`), `fit` (that tier's `fit` from config) and `scored_resume_sha256`, in one UPDATE. While a resume is in use, `llm_output` is canonical JSON of the parsed gaps, never the reply, and a reply that copies 40 characters of the resume into a gap, or has a gap over 120 characters, is treated as one that does not parse (ADR-023). `fit_rating` stays null. A later run scores those still-unscored rows after it has fetched the boards. A configured resume that cannot be used holds all scoring for the run. The watch run does not call Telegram and does not open an EI record.

On that same pass, when `search.resume` is `resume/current.txt` and that file matches its import, the watcher also asks `http://127.0.0.1:1234/v1/embeddings` for one vector of the resume and one vector of the posting (company, title, location, work model, and the description when the board sent one). The client is loopback only, follows no redirects, and is not `fetch.py`. The model id is `scoring.embedding_model` (default `local-embedding`); stored vectors do not record it, so after a change they are compared as they are (ADR-019). Search migration 0004 stores the float32 BLOBs in `resume_embedding` and `posting_embeddings`, and the cosine in `postings.embedding_score`. The resume text and the posting text are not stored. A resume that is not imported, missing, or changed since its import (`no-resume`), a down server (`unreachable`), a reply that is not a usable vector (`bad-reply`), or a different dimension (`dim-mismatch`) leaves that cosine null and adds one log line, `embedding miss posting <id>: <reason>`. The run still exits 0. The client has a 60-second total and its own 1 MB cap. A transport failure (refused, out of time, or too large) means nothing more is sent to the embedding server this run; a reply that arrives but is not a vector is a miss for that posting only. A cosine never causes a skip. When the resume bytes change, stored posting vectors are compared again locally and the model is not asked to embed those postings again. `fit_rubric.embedding_weight` is not read. The EI schema stays at version 1.

The scoring request also carries up to `learning.labelled_examples` (default 5) of the operator's most recently labelled postings, as title, lane, gap count and label (an application's latest status, or `skipped` for a Skipped tap), read once per run; the posting being scored is left out. They reach the loopback request only (ADR-023 as amended for #43). Rules and thresholds never change by themselves, and cosines stay out of the decision (ADR-019).

`careerhunter embed-eval` reads those stored cosines and tiers. It does not call the model. The report is `evals/embed-eval-<utc>.txt`, mode 0600, and the terminal prints that relative path only. Each row is a posting id, the tier name, the cosine, and applied or skipped. Spearman uses midranks and Pearson correlation on those ranks, from the standard library. The LLM side follows `gap_tiers`, best first, as 1 for the best tier and 0 for the worst. Precision at 5 and at 10 counts an application as a hit among postings that were applied to or skipped. Fewer than two comparable rows, or a rank with no spread, writes `undefined` and the command still exits 0. When `fit_rubric.embedding_weight` is set, the report also ranks `weight * cosine + (1 - weight) * tier`. That blend is not stored and not alerted.

Discovery asks a local SearXNG when `discovery.searxng_url` is set, from step 0.5 of a watch run or by hand with `careerhunter discover`. The client is loopback only, follows no redirects, and is not `fetch.py`. A query is `site:` plus one title and one place; a cursor in `discovery_state` (search migration 0014) walks every combination of titles (lanes by priority, every variant), places (`search.location_rules`) and hosts (each supported ATS once, then `discovery.career_hosts`), `discovery.queries_per_run` per pass, wrapping around. Queries are `discovery.query_delay_seconds` apart. A down server (or a reply over the cap) is a miss, `discovery miss: unreachable`; a non-200 status, a body that is not JSON or not UTF-8, a reply with no `results` list, or a non-empty list with no usable url is a miss too, `discovery miss: bad-reply`. Either way nothing is stored and the cursor is unchanged. HTTP 429, or an engine SearXNG reports for a CAPTCHA or too many requests, stops the pass. A miss or a stop starts an exponential backoff from `discovery.interval_minutes`, at most seven days, that a successful pass clears. Each query has a 30-second total. A `localhost` URL is looked up inside that time, and only a loopback answer is used. The result title, the snippet, and the response body are not stored.

A hit that is a posting URL on a supported ATS is fetched by the watch run once, through that ATS's API, and alerted like a watched posting (step 0.5). Postings already stored, by `ats_key` or URL, are left out before the 20-per-run limit counts, and boards already watched or in the queue (any status) or excluded by slug before the 10-per-pass limit counts, so each pass reaches new ones; each pass also starts at a different place in what is left (the run id, the cursor), so items that keep failing at the front of the same results cannot take the limit every time. A hit that reduces to a board is queued in `discovery_candidates` only after one board-API request confirms it, at most 10 per pass: a board that does not exist (404, a Workday 422, a BambooHR redirect off its subdomain) is stored as `gone` (search migration 0012) and never queued again; an empty SmartRecruiters list cannot tell and is dropped for now. A company in `exclusions.companies`, by its slug or by any company name the confirming reply gives, is never queued. Career pages are not queued. Each skipped board is a line in the run's log.

`careerhunter discover approve` and `careerhunter discover reject` take one pending row id. They write `search.db` only: they do not rewrite `config.yaml` or `exclusions.yaml`, and they do not send Telegram. Approving a board inserts or fills a company with `watchlist` 1, and Workday's host and site with it. A new company uses `source` discovery. An existing company's source stays as it is, and a stored ATS identity that disagrees is refused. Approving a career page queued by an earlier version stores `careers_url` with `watchlist` 0, and the watcher does not fetch it. Rejecting a row stores that status and inserts no company. From the board fetch on, the existing filter, score, cosine, and alert path owns the posting. The EI schema stays at version 1.

On each successful poll the bot also sends pending discovery boards that have not been sent, or were sent more than seven days ago, unless `config.yaml` can't be read. Up to five are one message each. More than five are one message of at most eight, each row with its own Approve and Reject tokens, and the rest wait for a later poll. The text is the suggested name and the reduced URL. A tap calls the same approve and reject functions and opens `search.db` only. It does not open an EI record. Search migration 0006 stores `notified_at` after Telegram accepts the message. A failed send leaves that column null. A refusal replies `Not changed.` and leaves the row pending.

The bot, on each successful poll, reads `config.yaml` once and sends the watcher and discovery postings that have a score, a fit, no skip, no application, and no `alerted_at` (search migration 0003), and that do not repeat a role the operator logged or skipped (ADR-005 as amended for reposts), as one digest per watch run (`postings.score_run_id`). A run's postings wait until it has finished, or started more than twice the run budget ago; postings with no run go first, then runs oldest first, and at most `alerts.max_per_poll` (default 10) go per poll, the rest carrying over in order. A row is the company, the title, the location, `Lane: <name>` or `Off-lane` when lanes are configured, `Fit: High` (or `Medium`, `Low`), the number of gaps, the schedule flag in words, up to three key requirements, and the link, each capped; never gap names, the tier name or a cosine. Rows are packed into messages of at most ten rows and 4096 UTF-16 units. Each row has its own Applied and Skipped tokens under a keyboard id of its own, so a tap spends only that row; the bot then re-sends the message's keyboard with the other rows' live buttons. No answer, a reply that isn't JSON, or HTTP 429 stops the poll with nothing marked; a refused message is sent again one row per message, and a row refused alone goes behind the others; its refusals are counted in `alert_refusals` (search migration 0017), so a restart remembers them, and after three the bot gives it up: it is never sent again, stays unalerted, and `doctor` counts it. With lanes, rows go off-lane last, then by fit, best first, then non-fallback lanes before fallback ones, then lane priority, then the oldest (`first_seen`, then id); a stored lane no longer in config sorts as a fallback after every configured lane, before off-lane. An off-lane posting below `search.off_lane_min_fit` (default `high`) stays pending, and a posting the watcher has not lane-checked yet waits for its next run. Without lanes, alerts go by fit, then oldest, and none is held for its lane. The sort is done in Python, not SQL. If `config.yaml` can't be read, no watcher alert is sent that poll: the hold is recorded in `bot_state` (`alerts_held_since`), logged only when it starts and ends, and reported by `doctor`. `/log` records a self-sourced application instead of alerting it (ADR-015 as amended for #34): `/log <link> | method | date [| Company | Title]` at once, or a bare `/log <link>` after a method and a date button, through one `ledger.record_application` call; it needs config for the date and the claim check, so with config unreadable it fetches and writes nothing and replies `Config can't be read, so nothing was recorded. Run careerhunter doctor.` Applied and Skipped are unchanged. When the stored company is a watchlist entry flagged `agency: true`, it is shown as `Name (agency)`, because no separate employer was stored. `alerted_at` is set only after Telegram accepts the message, so a failed send is tried again next poll. Quiet hours are not implemented.

`careerhunter service install` also installs `org.careerhunter.watch`. That agent runs `careerhunter watch` once and exits. launchd starts it again after `schedule.watcher_interval_minutes` (default 60, minimum 15). It has no `KeepAlive`. A run that exits 0 writes `logs/watch.success`. Board failures and a down model still exit 0, so they still count. A missing config or database does not update the stamp. `doctor` reports whether the agent is loaded and whether the stamp is within two intervals. Changing the interval means uninstalling the services and installing them again.

Discovery has no agent of its own: it runs inside the watch agent (step 0.5). `service install` boots out and removes `org.careerhunter.discover` if an earlier version installed it. A pass that ran writes `logs/discover.success`, a miss included; a pass that was not due writes nothing. `doctor` reports discovery off for a null URL, and otherwise how old that stamp is: older than two discovery intervals, or two watch intervals when those are longer, is a failure. A leftover discovery plist is a warning, which does not fail `doctor`. `service uninstall` removes whichever plists exist.

### Posting age

`search.max_posting_age_days` (default 7, from 1 to 365) skips a posting whose posted date is more days before the configured date than that, with the reason `too_old`. The age gate runs after every other gate, so a posting that fails one keeps that reason. The posted date is the one the board itself gives, read in UTC, and stored in `postings.posted_date` (search migration 0013). Each ATS exposes it differently:

| ATS | Field | Form |
|---|---|---|
| Greenhouse | `first_published` on each job of the list | ISO timestamp with an offset |
| Lever | `createdAt` on each posting | milliseconds since the epoch |
| Ashby | `publishedAt` on each job | ISO timestamp with an offset |
| SmartRecruiters | `releasedDate` on each list row | ISO timestamp, UTC |
| BambooHR | `datePosted` in the posting's detail | `YYYY-MM-DD` |
| Workday | `postedOn` on each list row | relative: `Posted Today`, `Posted N Days Ago`, `Posted 30+ Days Ago` |

Workday's relative dates are counted back from the configured date. `Posted 30+ Days Ago` only says the posting is at least 30 days old: it is too old under a limit below 30 and has no date otherwise. A value in any other form, a timestamp with no offset, or a missing field gives no date. Then `first_seen` is used, with the line `<title>: no posted date; first_seen used` when the posting is stored. A posting the list already shows as too old gets no detail request; it is stored without its description and skipped. The recheck applies the same gate to stored postings with no skip and no score, from `posted_date` or else `first_seen`, so a posting that waits unscored past the limit is skipped too.

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

`careerhunter watch` and a search run of `careerhunter discover` write each line to `logs/watch.log` or `logs/discover.log` the moment it is produced, then to stderr. launchd discards stderr, so the file is what keeps a trace. It is opened only after the storage guard passes, without following a symlink, and must be a regular file at mode 0600. A log that has reached 5 MB when a run opens it is rolled over first by the standard library's `RotatingFileHandler`, keeping five older files (`.1` to `.5`) in `logs/`, every one at 0600. A crash leaves its traceback in that file only, never on stderr. See ADR-022.

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

Only what is on `main` is listed. The latest release is v0.4.0.

| Phase | Scope | Status |
|---|---|---|
| 1, Foundation (v0.1.0) | Storage guards, Keychain helper, schema and migrations, seed import, EI export, CI, docs | released |
| 1.5, Data layer split (v0.2.0) | `search.db` and one EI record file per claim, a correlation ID, write order and reconciliation, claim config and sealing | released |
| 2, Telegram bot (v0.3.0) | The bot (alerts with Applied/Skipped, `/log`, `/status`, `/activity`, `/recent`), job-ad snapshots, the launchd service, the fit-criteria schema (validation only), and repo hygiene | released |
| 3, Watcher (v0.4.0) | `careerhunter watch` on Greenhouse, Lever, Ashby, SmartRecruiters, BambooHR and Workday boards; exclusions, hard gates, location rules and the posting age; lanes; gap-tier scoring with a fit on a local model, grounded in an imported resume; local embedding cosines and `embed-eval`; one alert digest per run; run records, health and the weekly digest; `careerhunter log` and `report` | released |
| 4, Discovery (v0.4.0) | Discovery passes against a local SearXNG inside the watch agent, posting URLs fetched once through their ATS, and a board queue approved from the CLI or the bot | released |
| 5, Release (v0.4.0) | The MIT license (ADR-021), `careerhunter demo`, the model-id settings, the inert template watchlist, and SETUP for a release checkout and upgrades | released |

Work lands as pull requests against `main`, and the operator tags a release with semver when it is cut. The repo went public once Phase 1 was verified, and `main` was protected straight after (ADR-012).
