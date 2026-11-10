# Setup guide

**In plain language:** this guide takes you from nothing to a working career-hunter on a Mac that stays on, such as a Mac mini. You don't need to be a developer. Every command below can be copied and pasted into the **Terminal** app (press ⌘-Space, type "Terminal", press Return). After each step you'll see what the output should look like, and what to do if it doesn't.

It takes about 20 minutes, plus however long your drive takes to encrypt.

---

## What you need

- A Mac running a recent macOS. The steps were tested on macOS 26.
- An **external drive** that will hold your data, formatted as **APFS**. This guide calls it `ExampleDrive`; use your own drive's name wherever you see that.
- Your Mac's admin password (for installing developer tools).

## Step 1: Turn on FileVault for the Mac itself

career-hunter keeps its secrets in the macOS Keychain, which lives on the Mac's internal disk. FileVault encrypts that disk.

**System Settings → Privacy & Security → FileVault → Turn On.**

Check it from Terminal:

```bash
fdesetup status
```

Expected output:

```text
FileVault is On.
```

If it's off, career-hunter still runs, but prints a large warning every time.

## Step 2: Encrypt the data drive

career-hunter **refuses to write to an unencrypted drive**. In Finder, right-click the drive in the sidebar and choose **Encrypt "ExampleDrive"…**. Pick a strong password and store it in your password manager.

Encryption runs in the background, and the drive stays usable while it does.

Check it:

```bash
diskutil info /Volumes/ExampleDrive | grep -E "FileVault|Encrypted|Locked"
```

Expected output, once encryption is on:

```text
   FileVault:                 Yes
   Locked:                    No
```

**Unlocking automatically after a restart:** when macOS asks for the drive password after it is connected, tick **"Remember this password in my keychain"**. The drive will then unlock when you log in. On an always-on Mac, set the Mac to log in to your account after a power failure (System Settings → Users & Groups), otherwise the drive stays locked until someone logs in.

## Step 3: Install the developer tools and uv

These give you `git`, `make`, and [uv](https://docs.astral.sh/uv/), which installs Python for you.

```bash
xcode-select --install
```

A window pops up. Click **Install** and wait for it to finish. If it says the tools are already installed, that's fine.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close Terminal, open it again, then check:

```bash
uv --version
```

Expected output (the version number may be newer):

```text
uv 0.11.16 (…)
```

## Step 4: Download career-hunter

```bash
mkdir -p ~/Projects && cd ~/Projects
```

```bash
git clone https://github.com/sovcoach/career-hunter.git
```

```bash
cd ~/Projects/career-hunter && make setup
```

Expected output ends with:

```text
pre-commit installed at .git/hooks/pre-commit
```

## Step 5: Tell career-hunter where your data lives

This adds one line to your shell settings so every new Terminal window knows where the data folder is. Change `ExampleDrive` if your drive has another name.

```bash
echo 'export CAREERHUNTER_DATA_DIR=/Volumes/ExampleDrive/ExampleFolder' >> ~/.zshrc && source ~/.zshrc
```

Check it:

```bash
echo $CAREERHUNTER_DATA_DIR
```

Expected output:

```text
/Volumes/ExampleDrive/ExampleFolder
```

## Step 6: Create your data folder

```bash
cd ~/Projects/career-hunter && uv run careerhunter init
```

Expected output. Your real output shows the full folder path where this guide writes `$CAREERHUNTER_DATA_DIR`:

```text
created  $CAREERHUNTER_DATA_DIR
created  $CAREERHUNTER_DATA_DIR/exports
created  $CAREERHUNTER_DATA_DIR/snapshots
created  $CAREERHUNTER_DATA_DIR/logs
created  $CAREERHUNTER_DATA_DIR/backups
created  $CAREERHUNTER_DATA_DIR/config.yaml (from config.example.yaml; edit it before seeding)
created  $CAREERHUNTER_DATA_DIR/exclusions.yaml (from exclusions.example.yaml; edit it before seeding)
created  $CAREERHUNTER_DATA_DIR/seed.yaml (from seed.example.yaml; edit it before seeding)
database $CAREERHUNTER_DATA_DIR/search.db at schema version 1
no active EI claim (set ei.active_claim in config.yaml; see docs/SETUP.md)
```

`search.db` is where career-hunter keeps everything it learns from. Your EI record gets its own file once you set your claim in the next step.

**Upgrading from 0.1.0?** Your folder may still hold `careerhunter.db`. Version 0.2.0 doesn't use it. `doctor` reports it, and if it's empty you can delete it.

Running `init` again is safe. It keeps existing files and says `kept`.

## Step 7: Fill in your settings

Open the data folder in Finder:

```bash
open "$CAREERHUNTER_DATA_DIR"
```

Edit these with TextEdit, or any plain-text editor:

- **`config.yaml`**: your target job titles, locations, work models, salary floor, fit rubric, schedule, the companies you want to watch, and your EI claim (see below).
- **`exclusions.yaml`**: companies and keywords to skip.
- **`seed.yaml`**: the applications and activities you've *already* done. Delete the example entries and add your own. The comments at the top explain each field. If you only remember roughly when you applied, use `date_precision: week` with `week_of`. If you don't remember at all, use `date_precision: unknown`. Don't guess an exact date. Include the job ad's `url` (or `requisition_id`) whenever you have it: it's how career-hunter tells a re-posted role apart from the original.

### Which setting the watcher reads

The watcher and the scorer read `config.yaml` and `exclusions.yaml` only. A note you keep somewhere else is not applied. Map it onto a field:

| What you decided | Field |
|---|---|
| Target titles, in priority groups | `search.lanes` |
| Where on-site, hybrid, and remote work is acceptable | `search.location_rules` |
| How many missing requirements still count, and the fit each tier means | `fit_rubric.gap_tiers` (every tier needs `fit: low`, `medium` or `high`) |
| The lowest fit at which a title outside every lane still alerts | `search.off_lane_min_fit` (default `high`) |
| Your resume, for scoring and a similarity line (see "Import your resume") | `search.resume: resume/current.txt` |
| Languages, credentials, shifts, pay, seniority (in the title, or also the description), and a minimum salary | `hard_gates` |
| A cap on weekend days | `schedule_limits` |
| Which roles to keep at one company | that company's `role_filters` |
| A staffing agency | `agency: true` on that watchlist entry |
| Companies, title keywords, description keywords, and one role | `exclusions.yaml` |

The templates use placeholder names such as Example Robotics, Example City, and Example Staffing Mill.

`search.target_titles`, `search.locations`, and `search.work_models` still validate so an older file loads. The watcher does not match them. Use lanes and location rules.

Lanes label and order alerts; they never skip a posting. A title is matched against every title in every lane, as whole words, ignoring case, and the lane with the best (lowest) priority wins. Alerts go off-lane last, then best fit first, then non-fallback lanes before `fallback: true` ones, then by priority, then oldest. A title that matches no lane is still gated and scored, and alerts only at `search.off_lane_min_fit` or better, labelled `Off-lane`. A posting keeps the lane it was given when it was stored, even if you change the lanes later. Without `search.lanes`, lane checking is off and `doctor` says so: alerts go by fit, then oldest. Lanes are separate from `hard_gates.never_gated_families`.

Every alert carries a fit: `Fit: High`, `Medium` or `Low`, from the `fit` of the tier the posting was scored into. A posting the model hasn't scored yet, or scored into a tier name that is no longer in `gap_tiers`, is held and not alerted until it has a fit. `doctor` counts what is held.

**Upgrading to fit tiers and lanes?** Do these in order:

1. Add a `fit` to every tier in `fit_rubric.gap_tiers` in your `config.yaml`. Without one, the config is refused when it is loaded, and the services can't start.
2. Stop the services: `uv run careerhunter service uninstall`.
3. Run `uv run careerhunter migrate`. It brings `search.db` to schema 11 (fit and runs in 9, lanes in 10, key requirements and each score's run in 11).
4. Start the services again: `uv run careerhunter service install`.

Postings that were already scored, and not yet alerted, get their fit from your config on the next watch run, by their tier name. With `search.lanes` set, that run also gives every stored posting its lane.

`fit_rubric.weights` and `fit_rubric.thresholds` are not applied. Scoring uses `gap_tiers` and leaves the numeric rating empty.

`fit_rubric.embedding_weight` is validated and the watcher does not apply it. The watcher does not blend it into the score or the alert. `careerhunter embed-eval` reads it when ranking a report. `search.resume: null` skips embeddings.

`hard_gates.max_commute_km` and `hard_gates.exclude_work_models` are not applied.

`hard_gates.min_salary` is yearly pay in CAD. A figure counts only when its sentence mentions pay (salary, pay, compensation, base, wage, range) or a pay unit follows it (per hour, /yr, annually). Bonus, signing, relocation and equity amounts don't count. Hourly figures are converted at 40 hours a week, daily at 260 days a year, weekly at 52 and monthly at 12. A figure marked as another currency (USD, US$, €, £ or any three-letter code but CAD) is ignored, and a plain `$` is read as CAD. A posting is skipped only when the top of its highest range is below your minimum; a posting with no salary passes.

`search.location_rules`: Greenhouse, BambooHR and Workday give no work model, so a location containing the word "remote" is treated as remote and anything else as on-site. Lever and Ashby give one, and it is used as given; SmartRecruiters says remote or hybrid when it is, and otherwise nothing. With location rules set, a posting with no location text and no work model is skipped as `empty_location`; a posting with a work model but no location text is kept.

`search.max_posting_age_days` (default 7) skips a posting the board says was posted more than that many days ago, as `too_old`. A board that gives no date uses the day the posting was first seen, and the watch log says so for that posting.

A Workday board needs three values on its watchlist entry, all from its careers page address `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`: `ats_slug` is the tenant, `ats_host` the `wdN` part and `ats_site` the site. A BambooHR board's `ats_slug` is the company's `bamboohr.com` subdomain, and a SmartRecruiters board's is the company identifier after `jobs.smartrecruiters.com/`.

#### Check a new ATS board once by hand

CI only reads fixtures. Before relying on a SmartRecruiters, BambooHR or Workday board, check its endpoint once from this Mac, outside CI, with the board's own values in place of the examples:

```bash
curl -sS "https://api.smartrecruiters.com/v1/companies/ExampleRobotics/postings?limit=1"
curl -sS -H "Accept: application/json" "https://examplerobotics.bamboohr.com/careers/list"
curl -sS -X POST -H "Content-Type: application/json" -d '{"appliedFacets":{},"limit":1,"offset":0,"searchText":""}' "https://examplefreight.wd5.myworkdayjobs.com/wday/cxs/examplefreight/External/jobs"
```

Each should answer JSON with a list of postings (`content`, `result` or `jobPostings`). An unknown SmartRecruiters company answers an empty list; an unknown BambooHR company is redirected to BambooHR's own site; an unknown Workday site answers 404 and an unknown tenant 422. Then run `careerhunter watch` and read the board's lines in `logs/watch.log`.

`hard_gates.seniority_words`, `title_exceptions` and `never_gated_families` match whole words of the title, ignoring case: "Lead" matches "Team Lead" and "(Lead)", but not "Leader". List a plural on its own if it should count. `hard_gates.seniority_in_description: true` also checks the description for seniority words; it is false by default, so a junior title whose description mentions senior colleagues is kept. A key under `hard_gates` that career-hunter does not know, such as a misspelled option, is refused when the config is loaded.

One `role_filters` block requires every family, work model, and place it lists. Its `work_models` check uses remote when it is inferred from the location; with no work model and no "remote", the work model is unknown and passes. It cannot say "remote, or else Example City." Treating that as an AND would hide roles that should stay. There is no field for that or-rule yet.

### Import your resume

The scorer gives your resume to the local model with each posting, so the gaps it lists are about you, not just about the posting. Import it once, and again whenever it changes:

```bash
uv run careerhunter resume import path/to/your-resume.pdf
```

A PDF or a DOCX works. It is read on this Mac, with no network. The text goes to `resume/current.txt` in the data folder (owner-only), and `search.db` keeps only a fingerprint of it (its SHA-256). The command prints the number of characters, never the text. An encrypted PDF, any other kind of file, or a file over 10 MB or 50,000 characters is refused, and nothing is stored.

Then set this in `config.yaml`:

```yaml
search:
  resume: resume/current.txt
```

No other value is accepted. The next watch run re-scores the postings you haven't skipped or applied to. Postings still listed on their board are re-scored with the description the board sends, when it sends one, and the rest from what was stored. That can take a few runs: new postings go first, and each run stops at its time budget. A posting you were already alerted about is re-scored but not sent again.

Don't edit `resume/current.txt` by hand. If it no longer matches what was imported, scoring and embeddings stop using it and `doctor` says so. Run `resume import` again.

### Refresh the scout prompt

When a posting comes from a scout rather than `careerhunter watch`, the scout's instructions have to match the exclusions and the hard gates. After you change either file:

```bash
uv run careerhunter scout-prompt
```

Paste the text into your private scout setup. The command prints to the terminal and writes nothing. It does not include your chat id, and it does not include gates the watcher does not skip on.

With the template files, the prompt names Example Staffing Mill, the title keyword Senior Director, and the shift pattern overnight. Yours will name whatever you put in the two files.

### Find companies beyond the watchlist

SearXNG runs on this Mac and listens on `127.0.0.1` only. Leave `public_instance` and `image_proxy` off, leave the limiter off, and add `json` to `search.formats`. Leave engines that need an API key disabled. Set `discovery.searxng_url` to that loopback URL, for example `http://127.0.0.1:8888/search`. A null URL leaves discovery off.

Discovery runs inside the watch agent, at most once per `discovery.interval_minutes`. A pass sends the next `discovery.queries_per_run` site-restricted queries (default 12), `discovery.query_delay_seconds` apart (default 5), and the next pass carries on from there, so over several passes every title, place and host is asked. Titles come from your lanes, places from `search.location_rules`, and hosts are each supported ATS plus `discovery.career_hosts`. A posting URL in the results is fetched once from its ATS and goes through the same gates, scoring and digest as a watched posting, with no approval. A board is queued only after one request to its API confirms it exists; a board that does not exist is never queued again, and a company in `exclusions.companies` is never queued. Career pages are not queued. A down server prints `discovery miss: unreachable`, and one that answers with an error or a reply that is not a result list prints `discovery miss: bad-reply`; either stores nothing; HTTP 429 or a CAPTCHA reported by SearXNG stops the pass, and either way later passes wait longer each time, up to seven days. `careerhunter discover` runs one pass by hand and prints each new pending row. `careerhunter discover list` prints the pending rows again.

```bash
make discover
careerhunter discover approve 4
careerhunter discover reject 4
```

`approve` marks that pending row approved. A board becomes a company with `watchlist` 1, and a new company has `source` discovery. The next `careerhunter watch` fetches that board. A career page queued by an earlier version stores `careers_url` and is not fetched. `reject` stores the rejection and inserts no company. Neither command rewrites `config.yaml` or `exclusions.yaml`.

The bot does the same thing from the allowlisted chat. Each pending board is one message with the suggested name, the URL, and Approve and Reject. When more than five are waiting, one message lists up to eight, each row with its own buttons, and the rest follow on a later poll. A tap that does not apply answers `Not changed.` and leaves the row pending. Nothing is sent while `config.yaml` can't be read.

### Compare a cosine with the tier

`careerhunter embed-eval` reads stored cosines and tiers. It does not call the model. The report is a mode 0600 file under `evals/` in the data folder. The terminal prints the relative path only. When `fit_rubric.embedding_weight` is set, the report also ranks `weight * cosine + (1 - weight) * tier`. That blend is not stored and not sent.

```bash
make embed-eval
```

### Your EI claim

career-hunter keeps one EI job-search record per claim, in its own file. It never works out your claim for you: you set it in the `ei` section at the end of `config.yaml`, using the start date Service Canada gave you.

```yaml
ei:
  active_claim: "2026-27"
  claims:
    - label: "2026-27"
      start: 2026-08-02
```

- **`label`** names the two years the claim spans, like `2026-27`. The claim must start in the first of those years.
- **`end`** is optional while the claim is active. Add it if you know it.
- **Not claiming right now** (for example, while employed)? Set `active_claim: null`. Nothing is written to any EI record then, but career-hunter keeps learning from your search.

Then create the claim's record file:

```bash
uv run careerhunter migrate
```

Expected output (the label and date will be yours):

```text
search.db: applied nothing; schema version 1
ei_record_2026-27.db: created; schema version 1
EI claim 2026-27 (from 2026-08-02)
```

Then check your files:

```bash
uv run careerhunter doctor
```

Expected output. Again, your real output shows the full folder path in place of `$CAREERHUNTER_DATA_DIR`:

```text
data dir        OK  $CAREERHUNTER_DATA_DIR (volume /Volumes/ExampleDrive, encrypted)
config.yaml     OK
exclusions.yaml OK
ei claim        OK  2026-27 active (from 2026-08-02)
search.db       OK  schema version 1
search.db       OK  0600
claim 2026-27   OK  schema version 1
claim 2026-27   OK  active, from 2026-08-02
claim 2026-27   OK  0600
reconcile       OK  search.db and 1 claim record agree
secret          telegram_bot_token: missing
```

`telegram_bot_token: missing` is expected until Phase 2.

## Step 8: Import your history

```bash
uv run careerhunter seed
```

Expected output (your numbers will differ):

```text
EI claim 2026-27 (from 2026-08-02)
search.db
  companies            4 inserted     0 already present
  postings             4 inserted     0 already present
  applications         4 inserted     0 already present
  decision_history     4 inserted     0 already present
ei_record_2026-27.db
  applications         4 inserted     0 already present
  status_history       4 inserted     0 already present
  activities           3 inserted     0 already present
```

Each application goes into both files: your EI record gets what Service Canada may ask for, and `search.db` gets the rest, including your notes. Activities go into the EI record only.

Every entry must belong to your claim, by how sure you are of its date:

- **`exact`**: the date must fall within the claim. A date outside it is refused, because it's usually a typo.
- **`week`**: the week must overlap the claim.
- **`unknown`**: always accepted, with a `warning:` line. It appears in the **Date unknown** section of every report.

If you really did apply to some jobs before the claim started, and want career-hunter to learn from them without putting them in the EI record, run `uv run careerhunter seed --route-out-of-claim-to-search`. Each one it sends to `search.db` only is listed as `search.db only: …`. Activities outside the claim are always refused.

You can fix `seed.yaml` and run this again at any time. Entries already imported are recognized and skipped, so nothing is duplicated. Existing entries aren't changed by re-running: the record is permanent by design. A mistaken entry is *voided* with a reason rather than deleted. The record keeps it, but reports leave it out.

### Recording a job you applied to yourself

When you apply to a job you found yourself, record the posting and the application in one step:

```bash
uv run careerhunter log https://jobs.example.com/ads/7 --method company_site --date 2026-10-20
```

`--method` is one of `company_site`, `linkedin_easy_apply`, `email`, `referral`, `agency`, `recruiter_inbound` or `other` (plain words such as `Company site` work too). `--date` is `YYYY-MM-DD`, `today` or `yesterday`, and must fall within the active claim. The page is fetched once for the company, title and location; if it doesn't show them, add `--company "Example Robotics" --title "Synthetic Analyst"`. A copy of the ad is saved when the page gives one. If you already logged an application to the same role at the same company (a repost counts: the title is compared without its requisition code), it is still recorded, and one more line names the earlier date, for example `You logged an application to this role on 2026-10-27.` Without a readable `config.yaml`, nothing is fetched or recorded. The bot's `/log` does the same from Telegram.

### A report across both files

```bash
uv run careerhunter report --claim 2026-27
```

This joins the claim's EI record to `search.db`, opening both read-only, and writes a private report to `reports/` in the data folder (only the path is printed): each application's employer, role, date, method and latest EI status, beside the lane, fit and source `search.db` knows, with counts by lane. It is for you, not for Service Canada: use `export` for that. Applications on only one side are counted; `doctor` lists them.

## Step 9: Produce an EI job-search report

Pick the period you need, in `YYYY-MM-DD` format:

```bash
uv run careerhunter export --claim 2026-27 --from 2026-09-01 --to 2026-09-30
```

Expected output:

```text
EI claim 2026-27: 3 applications, 3 activities
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>.pdf
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>_applications.csv
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>_activities.csv
```

The report is built from that claim's record file alone. Nothing from `search.db` can appear in it. The PDF is the readable report. The CSV files open in Numbers or Excel. Applications and activities without a known date appear in a separate **Date unknown** section of every report, so nothing is left out.

## When a claim ends

When you start a new claim, or stop claiming, update `config.yaml`:

1. Give the old claim its `end` date.
2. Add the new claim, and point `active_claim` at it (or set `active_claim: null`).

career-hunter then refuses to write anything until you **seal** the old claim's record. Sealing makes it permanent and read-only:

```bash
uv run careerhunter claim seal 2025-26
```

It explains what it will do, then asks you to type the label again:

```text
This will permanently close the record for EI claim 2025-26 (2025-06-04 to 2026-06-02):
  - record 2026-06-02 as the claim's end date;
  - mark ei_record_2025-26.db sealed, so the database refuses every further change;
  - fold its write-ahead log into the main file;
  - make the file read-only (0400).
It stays readable for `careerhunter export --claim 2025-26`. Keep it until at least 2032-06-02. This can't be undone.
Type the claim label again to confirm: 2025-26
sealed   $CAREERHUNTER_DATA_DIR/ei_record_2025-26.db (read-only; keep until 2032-06-02)
```

Sealing never happens on its own, and the claim you are on can't be sealed. If `active_claim` has a typo, `config.yaml` fails its checks, so nothing can be sealed by mistake. You can still export a sealed claim at any time.

## Step 10: The Telegram bot

The bot lets you record applications, statuses and activities from your phone. It only ever talks to your own private chat with it, and it has no open ports: it calls Telegram, never the other way round.

**Before you start, know what Telegram sees.** Bot chats aren't end-to-end encrypted, so what the bot sends and what you send pass through Telegram's servers. The bot keeps its own messages minimal. Watcher postings come as one digest per watch run, each row with company, title, location, lane (or `Off-lane`), fit word, the number of gaps, the schedule flag in words, up to three key requirements taken from the posting, and the link. A job you record with `/log` is answered with its company, title and a short reference. Status words and short references like `a7` go too. It never sends your notes, the names of the gaps it found (only how many), the numeric fit rating or EI record details. `alerts.max_per_poll` (default 10) caps how many postings go per poll; the rest follow on the next poll. What you type (activity descriptions, skip reasons) and any job-ad files you send do go to Telegram, so don't type anything sensitive. See ADR-015 in docs/DECISIONS.md.

### 10a. Create the bot and store its token

In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. It gives you a token. Store it in the Keychain:

```bash
uv run careerhunter secret set telegram_bot_token
```

Paste the token when asked. You won't see it as you paste, which is deliberate. Paste it a second time to confirm. Then check it:

```bash
uv run careerhunter secret verify telegram_bot_token
```

Expected output:

```text
telegram_bot_token: present (46 chars)   ← the number is the token length
```

### 10b. Tell career-hunter which chat is yours

Send your new bot any message from your phone, for example `hi`. Then:

```bash
uv run careerhunter bot discover
```

Expected output (your number will differ):

```text
chat 123456789 (private)
Put your private chat's id in config.yaml as telegram.chat_id.
```

`discover` doesn't reply to anyone and doesn't show message content. Put the number in `config.yaml`:

```yaml
telegram:
  chat_id: 123456789
```

Only that chat can use the bot. Messages from anyone else get no reply at all.

### 10c. Run it as a service

```bash
uv run careerhunter service install
```

Expected output:

```text
installed ~/Library/LaunchAgents/org.careerhunter.bot.plist; the bot starts now and at every login
installed ~/Library/LaunchAgents/org.careerhunter.watch.plist; careerhunter watch runs now and every 60 minutes
```

The bot restarts if it stops, retrying once a minute (for example, while the data drive is still locked after a restart). The watch agent runs once and exits, then waits for `schedule.watcher_interval_minutes`. Check both with `uv run careerhunter doctor`:

```text
telegram        OK  chat set
bot service     OK  running (pid 4242)
bot heartbeat   OK  polled within the last 5 minutes
watch service   OK  loaded
watch           --  no successful run yet
```

The bot log is `logs/bot.log` in your data folder. It records events only, never message text or the token. A finished watch writes `logs/watch.success`. Each line of a run goes to `logs/watch.log` as it happens, and the last line counts the boards. A crash leaves its traceback in that file only. A run looks like this:

```text
watch: Example Staffing: no ats_type; skipped
watch: Example Robotics (greenhouse): 1 new, 0 already recorded
watch: Sample Logistics (lever): 1 new, 0 already recorded
watch: 2 boards fetched, 0 failed, 0 left
```

A run has 600 seconds. Boards it does not reach are logged `run budget reached; left for the next run`, and the next run fetches them first. To stop them: `uv run careerhunter service uninstall`. Changing an interval means uninstalling the services and installing them again.

Discovery runs inside the watch agent: there is no separate discovery agent. A null `discovery.searxng_url` leaves it off, and `doctor` shows `discovery       --  off`. With the URL set, a watch run starts with a discovery pass when the last one was claimed at least `discovery.interval_minutes` ago; the pass's lines go to `logs/watch.log` with the run's, and a pass that ran writes `logs/discover.success`:

```text
discovery miss: unreachable
discover: 0 queued, 0 postings found
```

`doctor` shows how old that stamp is; one older than two discovery intervals, or two watch intervals when those are longer, is a failure. If an earlier version installed `org.careerhunter.discover`, run `service install` again: it removes that agent and prints `removed …; discovery now runs inside the watch agent`, and `doctor` warns until you do. List, approve and reject write no log and do not refresh the stamp. `service uninstall` removes the bot and the watch agent, and an old discovery agent if one is left.

`doctor` also tells "ran" apart from "did useful work". A watch run is useful when it fetched at least one board and, if postings were waiting to be scored, the local model answered; a discovery pass is useful when SearXNG gave a usable reply to every query. After `health.unproductive_runs` runs (or passes) in a row that weren't useful (default 3), `doctor` warns:

```text
watch useful    WARN the last 3 watch runs did no useful work; last useful run 300 min ago. See logs/watch.log
discovery useful WARN the last 3 discovery passes got no usable reply; last useful pass 600 min ago
health          WARN LM Studio failed the last 3 runs (unreachable); 4 postings held for scoring
```

A `health` line names a component that is failing now (LM Studio, SearXNG, or every board) once it has failed `health.failure_runs` runs in a row (default 3); the bot also sends one message about it then, and one when it works again (see 10d). Both settings take 1 to 50:

```yaml
health:
  unproductive_runs: 3
  failure_runs: 3
```

### 10d. Using it

| Send | What happens |
|---|---|
| `/log <link> \| method \| date` | Records a job you found and applied to yourself, in one message: the posting and the application, with a copy of the ad when the page gives one. The method is a word such as `email` or `Company site`; the date is `YYYY-MM-DD`, `today` or `yesterday`. Send just `/log <link>` to be asked for the method and the date with buttons. If the page doesn't show the company and title, add them: `/log <link> \| email \| today \| Company \| Title`. |
| **Applied** | The bot asks how you applied, then when: **Today**, **Yesterday**, **This week**, **Last week** or **Other date** (reply with YYYY-MM-DD). It saves a copy of the job ad. If it can't, it asks you to reply with a PDF or screenshot of the ad. |
| **Skipped** | Asks why, then records the skip. Skips never go into the EI record. |
| `/status a7 interview_scheduled` | Updates an application. References like `a7` come from `/recent` and last 7 days. Send just `/status` to choose with buttons. |
| `/activity networking 2026-10-27 Coffee with a recruiter` | Records a job-search activity. Leave out the date to choose it with buttons. |
| `/recent` | Your latest applications, their references and current statuses. |

Every date must fall inside your active EI claim, as with `seed`, and future dates are refused. Buttons work once: tap a button twice, or use an old one, and the bot says it has expired.

The bot also tells you when something stops working. After `health.failure_runs` watch runs in a row (default 3) in which the local model, SearXNG, or every board failed, it sends one message, such as `Health: LM Studio has failed 3 runs in a row. 4 postings are waiting to be scored.`, and when that part works again, one more: `Health: LM Studio is working again.` It sends nothing more about the same outage in between.

---

## Troubleshooting

| You see | What it means | Fix |
|---|---|---|
| `REFUSING TO START: CAREERHUNTER_DATA_DIR is not set` | Step 5 didn't take effect in this window | Run `source ~/.zshrc`, or open a new Terminal window |
| `… resolves to the boot volume. Either the external drive is not mounted…` | The drive is unplugged or locked, or the path has a typo | Plug in and unlock the drive, then check `ls /Volumes` |
| `… is NOT encrypted` | The drive isn't encrypted yet | Step 2 |
| `… is encrypted but locked` | The drive needs its password | Open it in Finder and enter the password |
| `… is inside the code repository` / `… inside a git work tree` | The data folder points somewhere inside a git project | Point `CAREERHUNTER_DATA_DIR` at the external drive |
| `… is readable by other users … chmod 700` | Folder permissions are too open | Run the `chmod` command it prints |
| `seed file has N problem(s)` | Something in `seed.yaml` needs fixing | Each line names the entry and field. Fix them and re-run |
| `… is before claim 2026-27 started` | A seed date falls outside your claim | Fix the date, or see `--route-out-of-claim-to-search` in Step 8 |
| `refusing to write until these are fixed: … Run: careerhunter claim seal …` | An earlier claim's record isn't sealed yet | See "When a claim ends" |
| `claim … BAD the active claim has no record file` | You set a claim but haven't created its file | Run `uv run careerhunter migrate` |
| `search.db BAD schema version 8, expected 10` | The code was updated and `search.db` hasn't caught up | Stop the services with `uv run careerhunter service uninstall`, run `uv run careerhunter migrate`, then `uv run careerhunter service install` |
| `watch: aborted: …` in `logs/watch.log` | The run stopped before it finished: config, database, or the log itself | The rest of the line names the cause. Fix it; the next run starts over |
| `run budget reached; left for the next run` | The run used its 600 seconds | Nothing to fix. The next run fetches those boards first |
| `resume BAD the resume file does not match its import` | `resume/current.txt` was edited, or an import was interrupted | Run `uv run careerhunter resume import` again with your file |
| `resume BAD no resume has been imported` | `search.resume` is set but nothing was imported | Run `uv run careerhunter resume import <file>`, or set `search.resume: null` |
| `resume BAD search.resume must be resume/current.txt` | `search.resume` names another file | Set it to `resume/current.txt` |
| `resume WARN left by an interrupted or running import` | An import stopped part-way, or one is running now | Nothing to fix. The next import removes them |
| `watch: scoring: resume not usable (…)` | The resume can't be used this run, for the reason shown | Run `uv run careerhunter doctor` and fix what it reports. Nothing is scored until then |
| `watch: scoring: model unreachable; N postings left for the next run` | The local model stopped answering | Start the model server. The next run scores them |
| `config.yaml BAD … tiers need fit: low \| medium \| high; missing or not one of those on [...]` | A gap tier has no `fit`, or a value other than those three | Add `fit: low`, `medium` or `high` to each tier it names |
| `watch held WARN … with no fit` | Postings were scored into a tier name that isn't in `fit_rubric.gap_tiers` any more | Put the tier back, or accept that they stay held. The next run fills in their fit once the name is back |
| `watch run BAD the last run started N min ago and did not finish` | A watch run crashed, was stopped, or hit a database error | See `logs/watch.log`. Its counts are not used; the next finished run counts again |
| `bot alerts BAD held since …` | The bot couldn't read `config.yaml` at its last poll, so it sent no watcher alerts | Fix `config.yaml` (`doctor` shows the problem). The next poll sends them |
| The bot replies `Config can't be read, so nothing was recorded. Run careerhunter doctor.` | `/log` needs `config.yaml` for the date and the claim check | Fix `config.yaml`, then send the link again |
| `lanes -- not configured` | `search.lanes` isn't set, so lane checking is off | Nothing to fix if you meant it. Add lanes to label and order alerts |
| `config.yaml BAD … needs an end date` | A claim that isn't active has no `end` | Add its end date, or check `active_claim` for a typo |
| `WARNING: FileVault is OFF` | The Mac's own disk isn't encrypted | Step 1 |
| `uv: command not found` | Terminal hasn't picked up uv yet | Close and reopen Terminal, or re-run the Step 3 installer |

## Testing the CLI safely

Want to try commands without touching your real record? Create a small encrypted test volume inside a disk image. A plain encrypted `.dmg` won't work, because the guard needs real APFS volume encryption.

```bash
hdiutil create -size 1g -type SPARSE -fs APFS -volname CHBase ~/ch-test.sparseimage && hdiutil attach ~/ch-test.sparseimage
```

```bash
diskutil apfs addVolume "$(diskutil info -plist /Volumes/CHBase | plutil -extract APFSContainerReference raw -)" APFS CHTest -passprompt
```

```bash
CAREERHUNTER_DATA_DIR=/Volumes/CHTest/ExampleFolder uv run careerhunter init
```

When you're done, eject `CHBase` and `CHTest` in Finder, then move `~/ch-test.sparseimage` to the Trash.
