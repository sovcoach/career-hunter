# Setup guide

**In plain language:** this guide takes you from nothing to a working career-hunter on a Mac that stays on, such as a Mac mini. You don't need to be a developer. Every command below can be copied and pasted into the **Terminal** app (press ⌘-Space, type "Terminal", press Return). After each step you'll see what the output should look like, and what to do if it doesn't.

It takes about 20 minutes, plus however long your drive takes to encrypt.

---

## What you need

- A Mac running a recent macOS. The steps were tested on macOS 26.
- An **external drive** that will hold your data, formatted as **APFS**. This guide calls it `ExampleDrive`; use your own drive's name wherever you see that.
- Your Mac's admin password (for installing developer tools).
- [LM Studio](https://lmstudio.ai), with one chat model and one embedding model downloaded (see "Run the local models" in Step 7). It is required: career-hunter scores postings only with a model running on this Mac, and a posting that hasn't been scored is never alerted. Memory decides which models fit; Step 7 has a guide.

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

### A separate checkout for the services

The background services (Step 10c) must run from a **release checkout**: a second folder that holds one release tag and nothing else. `uv sync` installs career-hunter so that it runs whatever source is in its folder, and `careerhunter` applies any new database migration it finds there the next time it opens `search.db`. If the services ran from the folder above and you checked out an unreleased branch there, a running service could apply that branch's migration to your real `search.db`; once applied, a migration can't be changed, and every restart (a crash, a reboot) would run whatever branch was checked out.

Make the release checkout next to the first one. This guide uses the tag `v0.11.1`; use the latest release tag:

```bash
cd ~/Projects/career-hunter && git worktree add ../career-hunter-release v0.11.1
```

```bash
cd ~/Projects/career-hunter-release && uv sync --locked
```

**Every `uv run careerhunter` command in the rest of this guide runs from `~/Projects/career-hunter-release`.** Keep `~/Projects/career-hunter` for reading the code or developing, and never install the services from it.

To see what career-hunter does before setting anything up, run the demo. It uses made-up companies and postings in memory, with fake job boards and a fake model: it needs no data folder, writes no file and connects to nothing.

```bash
cd ~/Projects/career-hunter-release && uv run careerhunter demo
```

It prints a watch run, the Telegram digest it would send, and the EI record of one application.

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
cd ~/Projects/career-hunter-release && uv run careerhunter init
```

Expected output. Your real output shows the full folder path where this guide writes `$CAREERHUNTER_DATA_DIR`:

```text
created  $CAREERHUNTER_DATA_DIR
created  $CAREERHUNTER_DATA_DIR/exports
created  $CAREERHUNTER_DATA_DIR/snapshots
created  $CAREERHUNTER_DATA_DIR/logs
created  $CAREERHUNTER_DATA_DIR/backups
created  $CAREERHUNTER_DATA_DIR/imports
created  $CAREERHUNTER_DATA_DIR/config.yaml (from config.example.yaml; edit it before seeding)
created  $CAREERHUNTER_DATA_DIR/exclusions.yaml (from exclusions.example.yaml; edit it before seeding)
created  $CAREERHUNTER_DATA_DIR/seed.yaml (from seed.example.yaml; edit it before seeding)
database $CAREERHUNTER_DATA_DIR/search.db at schema version 37
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

- **`config.yaml`**: your target job titles, locations, work models, salary floor, fit rubric, schedule, the companies you want to watch, the local models, and your EI claim (see below). The watchlist starts empty, so `careerhunter watch` fetches nothing until you add your own companies; the examples are a commented block under `watchlist:`.
- **`exclusions.yaml`**: companies and keywords to skip.
- **`seed.yaml`**: the applications and activities you've *already* done. Delete the example entries and add your own. The comments at the top explain each field. If you only remember roughly when you applied, use `date_precision: week` with `week_of`. If you don't remember at all, use `date_precision: unknown`. Don't guess an exact date. Include the job ad's `url` (or `requisition_id`) whenever you have it: it's how career-hunter tells a re-posted role apart from the original.

### Which setting the watcher reads

The watcher and the scorer read `config.yaml` and `exclusions.yaml` only. A note you keep somewhere else is not applied. Map it onto a field:

| What you decided | Field |
|---|---|
| Target titles, in priority groups | `search.lanes` |
| Where on-site, hybrid, and remote work is acceptable | `search.location_rules` |
| How many core and minor gaps a posting may have at each fit (worked out in code, never sent to the model) | `fit_rubric.requirement_tiers` (see "How a posting is scored") |
| The lowest fit at which a title outside every lane still alerts, or `never` | `search.off_lane_min_fit` (default `high`) |
| Your resume, for scoring and a similarity score that `embed-eval` reads (see "Import your resume") | `search.resume: resume/current.txt` |
| Postings you won't take, or whose fit you'd hold down, as numbered rules in your own words: a posting that meets a veto rule is skipped, and one that meets a cap rule is held to its fit (see "Your scoring criteria") | `search.criteria_file: criteria.md` |
| Languages, credentials, shifts, pay, seniority (in the title, or also the description), and a minimum salary | `hard_gates` |
| A cap on weekend days | `schedule_limits` |
| Which roles to keep at one company | that company's `role_filters` |
| A staffing agency | `agency: true` on that watchlist entry |
| How many of your recent Applied, Skipped and status decisions the scorer sees as examples (only the local model sees them; 0 for none) | `learning.labelled_examples` (default 5) |
| When `doctor` warns about runs that did nothing useful, and when the bot says a part stopped working | `health.unproductive_runs`, `health.failure_runs` (default 3 each) |
| Companies, title keywords, description keywords, and one role | `exclusions.yaml` |
| The boards to watch | `watchlist` (empty until you add companies) |
| Which models LM Studio runs for scoring and embeddings | `scoring.chat_model`, `scoring.embedding_model` (see "Run the local models") |

The templates use placeholder names such as Example Robotics, Example City, and Example Staffing Mill.

`search.target_titles`, `search.locations`, and `search.work_models` still validate so an older file loads. The watcher does not match them. Use lanes and location rules.

Lanes label and order alerts; they never skip a posting. A title is matched against every title in every lane, as whole words, ignoring case, and the lane with the best (lowest) priority wins. Each lane has a `group`: `primary` (the default), `career_change` or `stopgap` (`fallback: true` is read as `stopgap`; a lane can't set both). Alerts go by group first (every primary posting, then career change, then stopgap, then off-lane), then best fit first, then by priority, then oldest. So a High stopgap posting never outranks a Medium primary one, and when `alerts.max_per_poll` holds alerts back, stopgap postings wait first. The group decides the order and the label (`Lane: support (stopgap)`, `Lane: operations (career change)`), never whether a posting alerts. A title that matches no lane is still gated and scored, and alerts only at `search.off_lane_min_fit` or better, labelled `Off-lane`; with `off_lane_min_fit: never` it is never alerted. A title that matches no lane literally can still match by the normalized rule: a lane title that is two or more words and then `Specialist`, `Analyst` or `Associate` (one built-in group) matches a posting with the same words and any of those three, ignoring level words (Junior, Senior, Sr., I to IV, 1 to 4, Level N) and reading `Analyst, Client Experience` as `Client Experience Analyst`. So `IT Support Specialist` also matches `Senior IT Support Associate II` and `IT Support Associate - Night Shift`, but not `IT Support Associate Manager`, `IT Support Lead` or `Business Analyst, IT Support`. A one-word core such as `Data Analyst` only matches literally. A lane title that is a junior or entry-level word and `Developer` (`Junior Developer`, `Entry Level Developer`) also matches a title with such a word, up to three words, then `Developer`: `Junior Java Developer`, `Entry-Level Full Stack Developer`, `Jr. .NET Developer`. A posting keeps the lane it was given when it was stored, even if you change the lanes later, until you run `careerhunter lanes reassign`. Without `search.lanes`, lane checking is off and `doctor` says so: alerts go by fit, then oldest. Lanes are separate from `hard_gates.never_gated_families`.

Every alert carries a fit: `Fit: High` or `Medium`. A posting scored `low` is stored and never alerted. A posting the model hasn't scored yet, or scored under a tier name that isn't high, medium or low (an older release's), is held and not alerted until it has a fit. `doctor` counts what is held.

### How a posting is scored

The local model reads the posting and your resume and lists only the gaps: each qualification the posting requires or prefers that your resume doesn't meet, or meets only partly, in a few words. It never lists a qualification you meet, a duty of the job, or something the posting says it will teach, so those are never gaps. It is told to keep its reasoning brief. Experience counts only where your resume's roles and skills show it: an industry word in your resume's summary isn't experience in that field's job functions. The program then decides, in code:

- **Core or minor.** A gap central to the role that you don't meet at all is core; a partly met one is minor. A tool or skill named in your resume's roles or skills (after its first `Experience` or `Work history` heading) is at most a minor gap.
- **The seniority line:** a posting asking for 4 or more years you don't show is a core gap, and so is a senior or lead title. Up to 3 years is a minor gap, and none at all when the title is junior or entry level (junior, entry level, associate, new or recent graduate, intern, co-op, Tier 1, Level 1, or a level I or 1).
- **The fit** comes from the core and minor gaps, with `fit_rubric.requirement_tiers`:

  ```yaml
  fit_rubric:
    requirement_tiers:
      high_max_minor: 2          # High: no core gap and at most this many minor gaps
      entry_level_extra_minor: 2 # more minor gaps allowed when the posting says it's entry level
      exclude_at_core: 3         # this many core gaps or more: skipped (gap_tier)
      exempt_types: [education]  # gap types that never count; leave empty to count them all
  ```

  Two core gaps, a core gap in the role's main function, or a seniority gap make it Low. One other core gap makes it Medium. With no core gap it's High up to `high_max_minor` minor gaps, and Medium beyond that: minor gaps alone never make a posting Low. The gap types are experience, skill, tool, education, certification, licence, language, seniority and other. Every key is optional and has the default shown, except `exempt_types`, which defaults to none.

The model never sees these numbers. Its sampling settings and seed stay the same from call to call (the defaults, or what `scoring.sampling` sets), so the same posting and resume give the same reply.

**After you change `search.lanes`, or upgrade to a release that matches titles differently,** run `uv run careerhunter lanes reassign --dry-run`. It lists each stored posting whose lane would change, with its id, title, and old and new lane (`off-lane` for none), and each posting not checked against the lanes yet (`not checked -> <lane>`), then the counts and how many held postings would become alertable. It writes nothing. If the list looks right, run `uv run careerhunter lanes reassign` to apply exactly those moves; each is logged in `search.db`. Released postings alert at the bot's next poll, within `alerts.max_per_poll`, and one older than `search.max_posting_age_days` is skipped as `too_old` instead. Without `search.lanes` it refuses.

**Upgrading from 0.3.0?** Follow "Upgrading to a new release" near the end of this guide. Postings that were already scored, and not yet alerted, get their fit from your config on the next watch run, by their tier name. With `search.lanes` set, that run also gives every stored posting its lane.

`fit_rubric.weights` and `fit_rubric.thresholds` are not applied. Scoring uses `requirement_tiers` and leaves the numeric rating empty.

`fit_rubric.embedding_weight` is validated and the watcher does not apply it. The watcher does not blend it into the score or the alert. `careerhunter embed-eval` reads it when ranking a report. `search.resume: null` skips embeddings.

`hard_gates.max_commute_km` and `hard_gates.exclude_work_models` are not applied.

`hard_gates.min_salary` is yearly pay in CAD. A figure counts only when its sentence mentions pay (salary, pay, compensation, base, wage, range) or a pay unit follows it (per hour, /yr, annually). Bonus, signing, relocation and equity amounts don't count. Hourly figures are converted at 40 hours a week, daily at 260 days a year, weekly at 52 and monthly at 12. A figure marked as another currency (USD, US$, €, £ or any three-letter code but CAD) is ignored, and a plain `$` is read as CAD. A posting is skipped only when the top of its highest range is below your minimum; a posting with no salary passes.

`search.location_rules`: Greenhouse, BambooHR and Workday give no work model, so a location containing the word "remote" is treated as remote and anything else as on-site. With no "remote" in the location, a title with the whole word (such as `Analyst - Remote`) is read as remote too, and then your `remote` list may match the location or the title. Lever and Ashby give one, and it is used as given; SmartRecruiters says remote or hybrid when it is, and otherwise nothing. A posting with no location text is never skipped for it (when the board gives none, a `Location:` line in its description is used, and the watch log says so), and neither is one whose location names only a country with no remote or hybrid stated (`Exampleland`), or only a building with no city (`Example Tower`, as some Workday boards send): it is scored and alerted as usual, ranked as any other in its lane group, and its alert says `Location unknown` where the place would be (a country or building is shown as given). Its description is still checked for office days elsewhere. Travel never skips a posting: offsites, retreats, conferences, client or customer visits as needed, required travel. A posting whose description asks for regular office days ("2 days a week in the office", "on-site once a month", commuting to another city 1 to 3 days a week) outside your `onsite_hybrid` places is always skipped as `office_days_elsewhere`; office days in one of those places, or at an on-site or hybrid posting's own approved location, are fine. There is no setting for this: a `config.yaml` that still has `search.location_rules.allow_travel_outside` is refused until that line is deleted.

`search.max_posting_age_days` (default 7) skips a posting the board says was posted more than that many days ago, as `too_old`. A board that gives no date uses the day the posting was first seen, and the watch log says so for that posting.

A Workday board needs three values on its watchlist entry, all from its careers page address `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`: `ats_slug` is the tenant, `ats_host` the `wdN` part and `ats_site` the site. A BambooHR board's `ats_slug` is the company's `bamboohr.com` subdomain, and a SmartRecruiters board's is the company identifier after `jobs.smartrecruiters.com/`. A Lever board in Lever's EU region (its page is on `jobs.eu.lever.co`) also needs `ats_region: eu`, so it is read from `api.eu.lever.co` and its links stay on `jobs.eu.lever.co`; `ats_region` is for Lever entries only, and discovery sets it by itself for a board it finds on the EU host. A Greenhouse board on `job-boards.eu.greenhouse.io` needs nothing extra: it is read through the usual Greenhouse API.

A Workday board can also be filtered on the board itself with `ats_facets`, so its list holds only the postings you'd keep and fits in fewer pages. Give each facet and value by the name the board shows in its search filters, never by an id:

```yaml
watchlist:
  - name: Example Freight
    ats_type: workday
    ats_slug: examplefreight
    ats_host: wd5
    ats_site: External
    ats_facets:
      Work Region: Northland
      Sites: [Harbour Point, Hillside]
```

Each facet takes one value name or a list of them (at most 10 facets and 50 values, each name 1 to 100 characters). Names match the board's own ignoring case and extra spaces. A value name the board uses more than once stands for all of them. `ats_facets` is for `ats_type: workday` entries only.

Each run the watcher reads the board's unfiltered first page, looks up the ids of those names in its facets, and then reads the list with them applied, from the first page again. That's one extra request a run. When a name isn't found, because the board renamed it or no open posting has that value today (Workday leaves out values with no postings), the watch log says `watch: Example Freight (workday): ats_facets "Work Region: Eastland" not found; read unfiltered`, the board is read unfiltered as if it had no `ats_facets`, and `doctor` warns:

```text
workday facets  WARN Example Freight: "Work Region: Eastland" not found last run (renamed, or no open postings); read unfiltered
```

To see a board's facet names, look at the `facets` in the answer to the Workday check below: each has a `descriptor` (the facet's name) and `values`, each with its own `descriptor`. A facet can sit inside a group (a board's locations under a location group, for example), and the watcher looks inside groups too. Nested facets have only been tested with fixtures, not against a live board. If a board reaches the page limit, the watch log says how many pages its total fills, such as `page limit reached (25 of 61 pages)`.

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

### Run the local models

career-hunter talks to [LM Studio](https://lmstudio.ai) on this Mac only, at fixed addresses: `http://127.0.0.1:1234/v1/chat/completions` for scoring and `http://127.0.0.1:1234/v1/embeddings` for the similarity score (a cosine stored beside each score, which `embed-eval` reads). Neither address can be changed in `config.yaml`, and nothing is sent anywhere else. Port 1234 is LM Studio's default.

1. In LM Studio, download one **chat model** (an instruction-following model; it has to answer with JSON) and one **embedding model**.
2. Start LM Studio's local server on port 1234 and load both models. The server must be running whenever the watch agent runs.
3. Put each model's identifier in `config.yaml`. Use the identifier LM Studio shows for the loaded model, in its Developer tab or with `lms ps` in Terminal:

```yaml
scoring:
  chat_model: example-chat-model
  embedding_model: example-embedding-model
```

Each is text of 1 to 200 characters, with no control characters and no spaces at either end. A `chat_model` left out or `null` asks for `local-model`. With no `embedding_model`, embeddings are off: the watcher sends no embedding request and logs no `embedding miss` line, and no cosine is stored. `scoring` takes one other key, `timeout_seconds` (below): a `url` is refused.

4. **LM Studio's API token.** Keep authentication on in LM Studio's server settings, create an API token there, and store it in the Keychain the same way as the bot's token:

   ```bash
   uv run careerhunter secret set lm_studio_token
   ```

   It is sent as a Bearer header on every scoring and embedding call, and never printed or logged. `doctor` asks LM Studio for its model list with it: `LM Studio OK` when it is accepted, `BAD unauthorized` when LM Studio refuses it (store it again), and `BAD unreachable` when the server isn't running. A refused token is never reported as unreachable, in the watch log or in the bot's health message either.

**Reasoning models.** A chat model that thinks before it answers works: its thinking (`<think>…</think>` and the like) is removed before the reply is read, and never stored. Because a reasoning model takes longer, one scoring call may take up to `scoring.timeout_seconds` (60 to 540, default 300). Each call's duration is recorded, and `doctor`'s `scoring calls` line shows the median and the longest answered call of the last 14 days beside the timeout, such as `scoring calls   OK  12 answered in 14 days: median 118 s, max 166 s (timeout 200 s)`. It warns when the longest reaches 90% of the timeout: raise the timeout, or use a faster model. A call starts only while that much of the watch run is left, so the budget never cuts one short. Discovery and the boards have the first 10 minutes of a run; the scoring after them goes on until `scoring.run_minutes` from the start (10 to 50, under `schedule.watcher_interval_minutes`; unset, 20, or 5 less than a shorter interval). With a slow model, a longer run scores more postings: at about 160 seconds a call and boards that take about 3.5 minutes, a 20-minute run scores four or five. Each board's log line says how long its fetch took when that was a second or more, such as `(fetched in 41 s)`. A reply that doesn't parse leaves the posting unscored for the next run; after 3 such replies for one posting, it is given up and never sent to the model again, and `doctor`'s `bot refusals` line counts it.

**A call that times out.** When a scoring call reaches `scoring.timeout_seconds` with no reply, the model may still have been working on that one posting, so the run goes on to the next posting. The log says `watch: <company>: '<title>' left unscored (model took longer than 300 s: DeadlineExceeded); moved to the back of the queue`, the call is counted as timed out in `doctor`'s `scoring calls` line (such as `…; 2 timed out`), and the posting waits behind the others for the next run. After 2 timeouts in a row for the same posting it is set aside and not sent again: the line ends `set aside after 2 timeouts in a row, not tried again until careerhunter rescore --set-aside`, and `doctor` shows `score timeouts  WARN 1 posting timed out 2 times in a row; set aside and not scored (retry: careerhunter rescore --set-aside)`. Run `uv run careerhunter rescore --set-aside` (add `--dry-run` to only list them) to have the next run try them again, or `rescore --posting <id>` for one. A call that is answered, parsed or not, clears the count. A run whose scoring calls, two or more, all timed out counts as an LM Studio failure, so the bot's health message comes after `health.failure_runs` such runs, as for a server that is down. There is no cap on how long a reply may be: cutting a reasoning model short only leaves a reply that doesn't parse.

Before a stored posting is scored, its description is read again from its board. A re-read that fails for another reason than the posting being gone (HTTP 404 or 410) or throttled (HTTP 429), such as HTTP 403, a server error, a fetch error or a reply that doesn't parse, is counted for that posting. After 2 in a row it is set aside: the line ends `set aside after 2 failed re-reads (http-403), not counted as gone, until careerhunter rescore --set-aside`, it is not read again, and `doctor` shows `re-reads  WARN 1 posting set aside after failed re-reads (HTTP 403); retry: careerhunter rescore --set-aside`. A re-read that works clears the count, and `rescore --set-aside` releases these postings too, naming why each was set aside. A posting given up after 3 model replies that did not parse (`doctor`'s `bot refusals` line) is released with `uv run careerhunter rescore --given-up` (add `--dry-run` first to list them), or one at a time with `--posting <id>`; the next watch run sends it to the model again.

**Choosing models for your Mac's memory.** On a Mac with Apple silicon, the models share memory with everything else. A model's size on disk is roughly the memory it needs once loaded, plus some for the prompt. Leave at least a third of the memory for macOS and your other apps. As a guide, for chat models stored at 4-bit quantization:

| Memory | Chat model size to look for |
|---|---|
| 16 GB | about 7 to 8 billion parameters (around 5 GB) |
| 24 to 32 GB | up to about 14 billion parameters (around 9 GB) |
| 64 GB or more | up to about 32 billion parameters (around 20 GB) |

An embedding model is much smaller, usually under 1 GB. A larger chat model usually lists gaps more carefully and takes longer per posting. A watch run has 600 seconds, and postings it doesn't reach are scored on the next run. If LM Studio can't load a model, pick a smaller one.

If the chat model doesn't answer at all (a refused connection, a dropped one, or an HTTP error), the posting's line names the error's type, such as `left unscored (model unreachable: ConnectionRefusedError)`, the watch log says `watch: scoring: model unreachable; N postings left for the next run`, nothing is alerted until it answers, and after `health.failure_runs` runs in a row the bot tells you. Changing `scoring.chat_model` does not re-score anything by itself. Changing `scoring.embedding_model` does not re-embed stored postings: their stored vectors are compared with the new resume vector as they are, so stored cosines mix the two models until those postings are gone. A cosine never decides anything and is not sent.

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

### Your scoring criteria

Some postings you won't take can't be caught by a field or a gate word: a short contract with no path to a permanent role, say, written a dozen different ways. Write each as a numbered rule in a text file in the data folder. Each rule is a **veto** (a posting that meets it is skipped) or a **cap** (a posting that meets it keeps its score, but its fit is held to at most the level the rule names). The scorer sends the rules with each posting (ADR-028).

1. Create the file, owner-only:

   ```bash
   touch "$CAREERHUNTER_DATA_DIR/criteria.md" && chmod 600 "$CAREERHUNTER_DATA_DIR/criteria.md"
   ```

2. Write one rule per line, numbered 1, 2, 3 in order, each starting with its kind: `veto:`, or `cap low:`, `cap medium:` or `cap high:` (a fit level). A rule may also carry a short label in square brackets before the colon, as in `2. veto [short contract]:`. The label names the rule in skip reasons and the withdrawn message, and is never sent to the model. Say exactly what the rule covers, with any exception it allows: the model applies it as written, so a rule that names a preference or an asset covers those too. Blank lines and lines starting with `#` are ignored. For example:

   ```text
   # Postings I won't take, and postings I'd hold down
   1. veto: No night-shift roles at a warehouse.
   2. veto [short contract]: No contract under 6 months unless a path to permanent is stated.
   3. cap medium [needs a car]: A role that needs a car for daily site visits.
   ```

3. Set this in `config.yaml`:

   ```yaml
   search:
     criteria_file: criteria.md
   ```

The path is relative to the data folder and must stay inside it, even through a symlink. The file must be UTF-8 text that only you can read: at most 20 rules, each at most 300 characters, and 4,000 characters in all. A label is 1 to 40 characters with no brackets inside; an empty, longer or unclosed one makes the file unusable, as a rule without its kind does. Only this file is read: other notes in the data folder never reach the scorer. Leave the key out, or set it to `null`, to send no criteria.

**How a rule skips a posting.** When the model finds that a posting meets a rule, it names the rule and quotes the words in the posting that show it. The quote is checked against the title and description that were sent, ignoring case and spacing. If the words are there, the posting is skipped with the reason `criteria: 2. short contract` (the rule's number and label) and never alerted. A rule without a label is named by its first six words, with `…` when it goes on: `criteria: 2. No contract under 6 months unless…`. The log says `watch: <company>: '<title>' skipped (criteria 2)`. If the quote isn't in the posting, is too short, or names a rule the file doesn't have, the veto is rejected and the posting is scored as usual: `veto rejected (quote not found, rule 2); scored`. The quote itself is never stored or logged. Something the rule allows is not a match: an exception the rule names that the posting states, or the allowed side of a limit it sets.

**How a cap holds a posting down.** A cap rule is checked the same way: the model quotes the posting, and the quote must be in the text that was sent. If it is, the posting is scored as usual and its fit is stored as the lower of its tier's fit and the cap's level, so a `high` posting under `cap medium` is stored, and alerted, as `medium`. The log says `capped at medium (criteria 3)`. A cap never skips a posting and sends no message, even when a re-score lowers the fit of a posting already alerted. A cap whose quote doesn't check out is rejected and the posting keeps its fit: `cap rejected (quote not found, rule 3); scored`. When a posting meets a veto rule and a cap rule, the veto wins.

If a re-score skips a posting that was already alerted, the bot tells you once, in one message per poll for all of them: `withdrawn: Example Robotics — Synthetic Analyst, criteria: 2. short contract`.

If the file can't be used (missing, readable by others, empty, not UTF-8, over a size cap, not numbered rules such as a file still in plain prose, or a rule without its kind or with a bad label), the watch run holds scoring and logs `watch: scoring: criteria file not usable (<reason>); left for the next run`. The bot tells you why after the first run it holds, and again when scoring resumes. `doctor` shows it too, and counts the vetoes and caps of the last 14 days:

```text
criteria        OK  3 rules (2 vetoes, 1 cap), 214 of 4,000 characters; sent with each scoring call
criteria        BAD a rule has no kind or a bad label (start each rule with veto: or cap low|medium|high:, a label as veto [label]:); scoring is held
criteria vetoes OK  3 vetoed, 1 rejected; 2 capped, 0 rejected in 14 days
```

Editing the file doesn't re-score anything: postings already scored keep their score, and the new rules apply from the next posting scored. Use `careerhunter rescore` to apply them to postings already alerted.

### Refresh the scout prompt

When a posting comes from a scout rather than `careerhunter watch`, the scout's instructions have to match the exclusions and the hard gates. After you change either file:

```bash
uv run careerhunter scout-prompt
```

Paste the text into your private scout setup. The command prints to the terminal and writes nothing. It does not include your chat id, and it does not include gates the watcher does not skip on.

With the template files, the prompt names Example Staffing Mill, the title keyword Senior Director, and the shift pattern overnight. Yours will name whatever you put in the two files.

### Find companies beyond the watchlist

SearXNG runs on this Mac and listens on `127.0.0.1` only. Leave `public_instance` and `image_proxy` off, leave the limiter off, and add `json` to `search.formats`. Leave engines that need an API key disabled. Set `discovery.searxng_url` to that loopback URL, for example `http://127.0.0.1:8888/search`. A null URL leaves discovery off.

Discovery runs inside the watch agent, at most once per `discovery.interval_minutes`. A pass sends the next `discovery.queries_per_run` site-restricted queries (default 12), `discovery.query_delay_seconds` apart (default 5), and the next pass carries on from there, so over several passes every title, place and host is asked. Each query reads up to `discovery.result_pages` result pages (1 to 3, default 2), each one more request after the same pause. A query stops paging early when a page brings no new results or fails, and the watch log says so by the query's number in the pass, never its text; HTTP 429 on any page still stops the pass. The pauses of one pass, queries times pages times the delay, must be at most 120 seconds, so a pass with 2 pages sends at most 12 queries 5 seconds apart. Discovery recognizes the EU board hosts of Greenhouse and Lever, and the embed addresses of Greenhouse, Lever (`api.lever.co/v0/postings/<company>?mode=iframe`) and Ashby (`jobs.ashbyhq.com/<company>/embed`). Titles come from your lanes, places from `search.location_rules`, and hosts are each supported ATS plus `discovery.career_hosts`. A posting URL in the results is fetched once from its ATS and goes through the same gates, scoring and digest as a watched posting, with no approval. A board is queued only after one request to its API confirms it exists, with the company name that reply gives; a board that does not exist is never queued again, and a company in `exclusions.companies` is never queued. Each posting found on a queued board that passes your gates (no skip) is linked to it, and only a board with such a posting is ever sent to you. Career pages are not queued. A down server prints `discovery miss: unreachable`, and one that answers with an error or a reply that is not a result list prints `discovery miss: bad-reply`; either stores nothing; HTTP 429 from SearXNG stops the pass. One engine that SearXNG reports as rate limited, asking for a CAPTCHA or suspended doesn't: the pass goes on while other engines answer, names the throttled engines in its log line, and fails only when no query got an answer. After a failed pass the next one waits one `discovery.interval_minutes`, doubled for each further failure in a row, up to 24 hours. `doctor` shows the backoff with the command that ends it early, `careerhunter discover clear-backoff`. `careerhunter discover` runs one pass by hand and prints each new pending row. `careerhunter discover list` prints the pending rows again.

```bash
make discover
careerhunter discover approve 4
careerhunter discover reject 4
```

`approve` marks that pending row approved. `reject` takes an optional reason, kept with the rejection: `careerhunter discover reject 4 --reason "consumer retail"`. A board becomes a company with `watchlist` 1, and a new company has `source` discovery. The next `careerhunter watch` fetches that board. A career page queued by an earlier version stores `careers_url` and is not fetched. `reject` stores the rejection and inserts no company. Neither command rewrites `config.yaml` or `exclusions.yaml`.

The bot does the same thing from the allowlisted chat. Each candidate is one message: the company name, the ATS and the board link, then up to three of the postings found on it that passed your gates, each with its title, location and link. Its buttons are **Approve**, **Reject** and **Reject with reason…**, which records the rejection and asks for the reason as a reply. A candidate is sent once, and only while one of its postings is still unskipped; at most `discovery.max_candidates_per_day` are sent in a day (1 to 20, default 3), and the rest wait for the next day. One whose buttons expire stays pending and is still listed by `careerhunter discover list`. A tap that does not apply answers `Not changed.` and leaves the row pending. Nothing is sent while `config.yaml` can't be read.

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
search.db: applied nothing; schema version 37
ei_record_2026-27.db: created; schema version 5
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
timezone        OK  UTC
search.db       OK  schema version 37
search.db       OK  0600
resume          --  not imported (optional: careerhunter resume import)
lanes           OK  2 lanes; off-lane postings alert at high fit or better
watch run       --  none recorded yet
watch useful    --  no useful run recorded yet
bot alerts      OK  not held
bot refusals    OK  no posting given up
score timeouts  OK  none set aside
workday facets  --  none set (ats_facets in a watchlist entry)
claim 2026-27   OK  schema version 5
claim 2026-27   OK  active, from 2026-08-02
claim 2026-27   OK  0600
reconcile       OK  search.db and 1 claim record agree
exports/        OK  0 files, all owner-only
snapshots/      OK  0 files, all owner-only
logs/           OK  0 files, all owner-only
telegram        --  telegram.chat_id not set (see: careerhunter bot discover)
bot service     --  not installed (run: careerhunter service install)
watch service   --  not installed (run: careerhunter service install)
watch           --  no successful run yet
discovery       --  off
git push        OK  push.default is current
git push        OK  pre-push guard installed
secret          telegram_bot_token: missing
```

The `--` lines and `telegram_bot_token: missing` are expected until you finish Step 10.

## Step 8: Import your history

```bash
uv run careerhunter seed
```

Expected output with the template's example entries (your numbers will differ):

```text
EI claim 2026-27 (from 2026-08-02)
search.db
  companies            3 inserted     0 already present
  postings             3 inserted     0 already present
  applications         3 inserted     0 already present
  decision_history     3 inserted     0 already present
ei_record_2026-27.db
  applications         3 inserted     0 already present
  status_history       3 inserted     0 already present
  activities           2 inserted     0 already present
warning: applications[2] (Placeholder Foods / Project Coordinator): date unknown: recorded under claim 2026-27 and listed under "Date unknown" in exports
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

`--method` is one of `company_site`, `linkedin_easy_apply`, `email`, `referral`, `agency`, `recruiter_inbound` or `other` (plain words such as `Company site` work too). `--date` is `YYYY-MM-DD`, `today` or `yesterday`, and must fall within the active claim. The link is saved without tracking parameters. The page is fetched for the company, title and location; if it doesn't show them, add `--company "Example Robotics" --title "Synthetic Analyst"`. The location comes from the page's structured data (a remote job with the countries it is open to reads as, for example, `Remote, Exampleland`), then from the board's own data, then from a `Location:` line in the job description. A line read from the job description's text is kept only when it looks like a place: it says Remote, or names a country, a Canadian province or territory or a US state (by name, or by its postal abbreviation after a comma, as in `Example Town, OH`), in at most 120 characters. A city on its own is kept only when it is, ignoring case, one of the places your `search.location_rules` name (`onsite_hybrid` or `remote`), read from `config.yaml` each time; any other city on its own, or a line such as `Sorting Hub (SH) / Packing Hub (PH) Operations Environment`, is left unknown, and the watch log and `locations fill` say so. Locations from the page's structured data or the board's own fields are taken as given. `--location "Example City"` gives it by hand and wins over the page; it is optional, and the reply says `Location: unknown` when none was found. A posting stored with no location takes the next one logged for it. `--referral` marks the posting as a referral, and so does the method `referral` (the label never changes the method, since a referred application can still go in through the company site): a referral counts as local, inside your office area, so no location rule, watchlist `locations` or office-days check ever skips it, and its alert says `Referral`. `uv run careerhunter referral --posting 42` (or `--url <link>`) marks a posting already stored; a skip already stored for it stays. For postings already stored with no location, `uv run careerhunter locations fill --dry-run` lists the locations their kept job ads and job descriptions name (it fetches nothing); without `--dry-run` it writes them to `search.db`, only where the location is still blank, and only reads the EI records. A copy of the ad is saved when the page gives one.

Keep the job description with it: `--jd-file path/to/ad.pdf` takes a PDF, HTML or text file, and `--jd-stdin` reads pasted text from standard input (use one or the other). Without either, the job description is fetched from the page when it carries one. The output says `Job description saved.` or `No job description was saved.`; a missing one shows up in `doctor`. An encrypted PDF is refused, and a scanned PDF with no text is kept as the copy of the ad but doesn't count as the job description.

**LinkedIn links are never fetched**, so `--company` and `--title` are required with one. Better still, open the posting, follow it to the company's own career site, and log that page's link instead: its job description can be fetched. Pasting the description is the fallback, and so is a LinkedIn Easy Apply posting with no career-site page. If you already logged an application to the same role at the same company (a repost counts: the title is compared without its requisition code), it is still recorded, and one more line names the earlier date, for example `You logged an application to this role on 2026-10-27.` Without a readable `config.yaml`, nothing is fetched or recorded. The bot's `/log` does the same from Telegram.

### When the posting is gone before you saved it

If an application's job ad was taken down before its job description was kept, say so, so it stops showing as missing:

```bash
uv run careerhunter jd unavailable --url https://jobs.example.com/ads/7 --reason "posting removed before capture"
```

Pick the application by its link, or by `--company`, `--title` and `--date` (the day you applied, or the Monday of its week) when it has none; more than one match is refused. The reason (1 to 200 characters) is added to the EI record and never changed there: a new reason is a new row. `doctor` and the report then show `--` with the reason instead of a warning. If you find the job description later, attach it as usual: it takes the reason's place everywhere it is shown, and the reason stays in the record. An import entry can do the same with `jd_unavailable` (below).

### Backfilling applications and their job descriptions

`seed` brings in your history once. `careerhunter import` is for adding job descriptions, and applications you haven't recorded yet, later on: it matches each entry to what is already recorded, and never changes an application's date.

1. **Prepare the file.** Copy `import.example.json` from the repo into the `imports/` folder in your data folder (`init` creates it, owner-only), for example as `imports/backfill.json`, and replace the example entries with yours. The file must sit under `imports/`: a file anywhere else, even elsewhere in the data folder, is refused. Each entry needs `employer`, `role`, `date`, `date_precision` (`day`, or `week` when you only know the week; any day of that week works, and it's stored as its Monday) and `method`. `location`, `work_model`, `url`, `notes` (kept in `search.db` only, and only for a new application) and one of `jd_text` (the description, pasted), `jd_file` (a PDF, HTML or text file, named relative to the JSON file and also under `imports/`) or `jd_unavailable` (why there is no job description, such as `posting removed before capture`; see "When the posting is gone before you saved it") are optional. Links are saved in their canonical form, so a LinkedIn link with tracking parameters becomes `https://www.linkedin.com/jobs/view/<id>/`. Nothing is fetched.
2. **Dry run.** It opens both databases read-only and writes nothing, not even a file:

   ```bash
   uv run careerhunter import "$CAREERHUNTER_DATA_DIR/imports/backfill.json" --dry-run
   ```

   Each entry gets one line: its number, its class, and what it would write to `search.db` and to the EI record. A job description shows only as its SHA-256 and its length; its text is never printed. The classes:

   | Class | Means |
   |---|---|
   | `CREATE` | Nothing matches: a new application, recorded the way `/log` records one |
   | `ATTACH` | One recorded application has the same employer (ignoring case, a leading "The" and suffixes such as Inc or Ltd) and the same title (with `Analyst, Client Experience` read as `Client Experience Analyst`), or several do and one has the same date. It gets the job description, and the link if it has none |
   | `UNCHANGED` | It already has this job description and link. A second run shows every entry this way |
   | `AMBIGUOUS` | More than one application could be meant, a title only nearly matches (`Senior … Specialist` for `… Analyst`), or two entries point at one application. The candidates are listed under it as `candidate app N (…)` |
   | `CONFLICT` | The application already has a different link or a different job description. Nothing is changed |
   | `REFUSED` | A field is wrong, the date is in the future or outside the active claim, or the application's claim record can't change |

3. **Resolve ambiguous entries.** Add `"resolve": N` to an entry, with the `app N` number of the application it belongs to, or `"resolve": "new"` to record it as a new application. Run the dry run again until the result is what you want.
4. **Apply.** The same command without `--dry-run` prints the same lines and does exactly what they say:

   ```bash
   uv run careerhunter import "$CAREERHUNTER_DATA_DIR/imports/backfill.json"
   ```

   It exits 0 when every entry is done or unchanged, and 1 when anything is left ambiguous, in conflict or refused (the rest are still applied). An entry dated before your claim is refused; `--route-out-of-claim-to-search` records it in `search.db` only instead, as with `seed`. It refuses to run at all (exit 2) while an earlier claim's record is unsealed, or for a file outside `imports/`.

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
EI claim 2026-27: 3 applications, 2 activities
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>.pdf
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>_applications.csv
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>_activities.csv
```

With job descriptions in that period, one more line names their folder:

```text
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-27_2026-09-01_2026-09-30_<time>-job-descriptions/ (2 job descriptions)
```

The report is built from that claim's record file alone. Nothing from `search.db` can appear in it. The PDF is the readable report. The CSV files open in Numbers or Excel. Each application shows its job description by reference: `JD 1a2b3c4d (pasted)` (the first characters of its fingerprint, and where it came from), `JD missing`, or `JD missing (pre-feature)` for one recorded before career-hunter kept job descriptions. The folder holds one `.txt` per application with a job description (a short header, then the text exactly as recorded) and a copy of its original file, all owner-only. Applications and activities without a known date appear in a separate **Date unknown** section of every report, so nothing is left out.

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

**Before you start, know what Telegram sees.** Bot chats aren't end-to-end encrypted, so what the bot sends and what you send pass through Telegram's servers. The bot keeps its own messages minimal. Watcher postings come as one digest per watch run, each row with company, title, location, lane (or `Off-lane`), fit word, the number of gaps, the schedule flag in words, up to three key requirements taken from the posting, and the link, with its `Applied`, `Skipped` and `JD` buttons. The job description of every posting that may be alerted (a medium or high fit) is kept in `snapshots/` when it is scored; tapping `JD` sends it and changes nothing, and an `Applied` tap records it as the application's job description. When the same role is posted more than once (the same company, title and known location, and the same or a near-identical kept job description, whatever the requisition numbers), it is alerted once, its row saying `Also posted as R-1002`. `Applied` then records one application whose EI entry names every requisition (`R-1001, R-1002`), and the other postings are covered by it: they are not skipped, so they never show in skip counts or reasons, and they are never alerted or scored again. `Skipped` skips them all, with the same reason. A posting with an unknown location is never grouped, so one application never covers two places. A job you record with `/log` is answered with its company, title and a short reference. Status words and short references like `a7` go too. It never sends your notes, the names of the gaps it found (only how many), the numeric fit rating or EI record details. `alerts.max_per_poll` (default 10) caps how many postings go per poll; the rest follow on the next poll. What you type (activity descriptions, skip reasons, job descriptions you paste) and any job-ad or job-description files you send do go to Telegram, so don't type anything sensitive. The bot sends a job description only when you tap a row's `JD` button: the text career-hunter kept for that posting when it was scored, which came from the posting's own board, in at most 4 messages (`… the rest is in the posting.` after the fourth). It sends nothing else from it, and never a JD you pasted or sent. See ADR-015 in docs/DECISIONS.md.

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

Install the services from the release checkout (see "A separate checkout for the services" in Step 4), so the LaunchAgents run that checkout's `careerhunter`:

```bash
cd ~/Projects/career-hunter-release && uv run careerhunter service install
```

Expected output:

```text
installed ~/Library/LaunchAgents/org.careerhunter.bot.plist; the bot starts now and at every login
installed ~/Library/LaunchAgents/org.careerhunter.watch.plist; careerhunter watch runs now and every 60 minutes
```

The bot restarts if it stops, retrying once a minute (for example, while the data drive is still locked after a restart). The watch agent runs once and exits, then waits for `schedule.watcher_interval_minutes`. Check both with `uv run careerhunter doctor`:

```text
telegram        OK  chat set
bot service     OK  running (pid 4242); checkout ~/Projects/career-hunter-release, data folder /Volumes/ExampleDrive/ExampleFolder
bot heartbeat   OK  polled within the last 5 minutes
watch service   OK  loaded; checkout ~/Projects/career-hunter-release, data folder /Volumes/ExampleDrive/ExampleFolder
watch           --  no successful run yet
```

Each service line names the checkout and the data folder the agent serves, as launchd reports them. launchd keeps one bot and one watch agent per user, not per data folder, so a `doctor` run against another data folder (a test volume, say) still describes those agents: the line says `not this CAREERHUNTER_DATA_DIR`, and an OK becomes a WARN.

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
| `/log <link> \| method \| date` | Records a job you found and applied to yourself, in one message: the posting and the application, with a copy of the ad when the page gives one. The method is a word such as `email` or `Company site`; the date is `YYYY-MM-DD`, `today` or `yesterday`. Send just `/log <link>` to be asked for the method and the date with buttons. If the page doesn't show the company and title, add them: `/log <link> \| email \| today \| Company \| Title`. A LinkedIn link is never fetched, so it always needs them. Add `\| location: Example City` anywhere after the link to give the location; it is optional, and the reply says `Location: unknown` when none was found. Add `\| referral` anywhere after the link (or use the method `referral`) to mark it as a referral. |
| `/log …` with the job description | Paste the job description on the lines after the `/log` line, in the same message. Telegram splits a long paste into several messages; the bot joins every plain text message that arrives within 4 seconds of the one before, with a blank line between them (where Telegram split it), and records the application once they stop. Telegram changes whitespace on the way (tabs become spaces, trailing spaces go, and the blank lines where it splits are dropped and put back by the bot), so a pasted job description is kept as Telegram delivered it. To keep the exact text, send it as a file instead. Or send the job description as a PDF, HTML or text file with the `/log` line as its caption. The reply ends `Job description saved.` and never repeats the text. |
| A reply with the job description | When no job description was saved, the bot asks for it: reply to that message with the text, or with a PDF, HTML or text file. If a long paste wasn't joined in full, the bot says so: reply to the `Recorded` message with the whole description as a `.txt` file, which replaces the one saved. |
| **Applied** | The bot asks how you applied, then when: **Today**, **Yesterday**, **This week**, **Last week** or **Other date** (reply with YYYY-MM-DD). It saves a copy of the job ad when the page gives one, then asks for the job description: reply with the text, or with a PDF, HTML or text file (which also becomes the copy of the ad if none was saved). |
| **Skipped** | Asks why, then records the skip as soon as you tap a reason. **Other…** records it at once as `other` and asks for your words: reply to that message (within a day) and your words replace `other`. Skips never go into the EI record. |
| `/status a7 interview_scheduled` | Updates an application, dated today. Add a date to record when it happened: `/status a7 rejected 2026-11-12` (not in the future, not before the application). References like `a7` come from `/recent` and last 7 days. Send just `/status` to choose with buttons: the newest 10 first, with `More…` for older ones. In place of the reference you can name the application by company or title words, such as `/status example robotics analyst rejected 2026-11-12`: when every word matches one application the status is recorded, and otherwise the matching applications come as buttons (with the status and date you gave), together with the status words. The statuses are `screening`, `assessment_requested`, `assessment_submitted`, `interview_scheduled`, `offer`, `hired`, `rejected`, `no_response`, `withdrawn` and `position_closed`. From the command line: `careerhunter status rejected --on 2026-11-12 --company "Example Robotics" --title "Analyst"` (or `--id`, or `--url`; `--dry-run` writes nothing). |
| `/skip` | Skips a posting whose alert buttons are spent or expired, or whose skip was lost: choose one of the postings alerted in the last 14 days, or send `/skip <link>`. Then the same reasons as **Skipped**. A posting you skipped already isn't skipped twice. A link career-hunter doesn't have yet is stored first, as a job you found yourself, with the company, title and job description its page gives (add `\| Company \| Title` when it doesn't, and always for a LinkedIn link), so the watcher never alerts it later. A skip goes to `search.db` only, never to the EI record. |
| `/activity networking 2026-10-27 Coffee with a recruiter` | Records a job-search activity. Leave out the date to choose it with buttons. |
| `/recent` | Your latest applications, their references and current statuses. |

Every date must fall inside your active EI claim, as with `seed`, and future dates are refused. Buttons work once: tap a button twice, or use an old one, and the bot says it has expired. There's no hurry: the buttons on an alert work for 7 days, and the follow-up buttons and the questions the bot asks (a date, a reason) for a day.

With `alerts.weekly_digest: {day: monday, hour: 9}` in `config.yaml`, the bot also sends a weekly message at that local time (`schedule.timezone`) with counts for the week before, by lane: alerts, Applied, Skipped, responses (an employer's first reply: screening and later, or a rejection) and outcomes. It has numbers and lane names only, never titles:

```text
Weekly digest, 2026-10-19 09:00 to 2026-10-26 09:00
operations: 2 alerts, 1 applied, 1 skipped, 2 responses, 2 outcomes
Off-lane: 1 alerts, 0 applied, 1 skipped, 0 responses, 0 outcomes
All lanes: 3 alerts, 1 applied, 2 skipped, 2 responses, 2 outcomes
```

The bot also tells you when something stops working. After `health.failure_runs` watch runs in a row (default 3) in which the local model, SearXNG, or every board failed, it sends one message, such as `Health: LM Studio has failed 3 runs in a row. 4 postings are waiting to be scored.`, and when that part works again, one more: `Health: LM Studio is working again.` It sends nothing more about the same outage in between. A criteria file that holds scoring (see "Your scoring criteria") is told after the first run, with the reason, and again when scoring resumes. An alerted posting that a criteria rule later skips is told once, in a `withdrawn:` message.

---

## Upgrading to a new release

Upgrade the release checkout only, one release tag at a time, in this order. The commands below upgrade 0.3.0 to 0.4.0; for a later release, read its entry in `CHANGELOG.md` first and use its tag. If your services were installed from `~/Projects/career-hunter` (as 0.3.0's guide did), steps 1 and 3 move them to the release checkout.

1. **Stop the services**, so nothing opens the databases while you work:

   ```bash
   cd ~/Projects/career-hunter && uv run careerhunter service uninstall
   ```

   It prints `uninstalled`, or `not installed` if nothing was running.

2. **Back up the whole data folder** into one dated folder under `backups/`. Each database file (`search.db` and every `ei_record_<label>.db`) is copied with SQLite's own backup command, which makes a consistent copy. Everything else (config, `resume/`, `exports/`, `snapshots/`, `reports/` and the rest) is copied as it is, keeping its permissions. Run these one at a time, in the same Terminal window (the first one sets `B`, which the others use):

   ```bash
   cd "$CAREERHUNTER_DATA_DIR" && B="backups/$(date -u +%Y-%m-%dT%H%M%SZ)" && mkdir -m 700 "$B" && echo "$B"
   ```

   ```bash
   for f in *.db; do sqlite3 "$f" ".backup '$B/$f'" && chmod 600 "$B/$f"; done
   ```

   ```bash
   rsync -a --exclude '/backups/' --exclude '/*.db' --exclude '/*.db-wal' --exclude '/*.db-shm' ./ "$B/"
   ```

   Then check the copy. Each database prints `ok`, and the last command prints nothing when every file and folder in the copy is owner-only:

   ```bash
   for f in "$B"/*.db; do echo "$f: $(sqlite3 "$f" 'PRAGMA integrity_check')"; done
   ```

   ```bash
   find "$B" -perm +077 -print
   ```

   If `find` lists anything, run `chmod -R go-rwx "$B"` and check again. `doctor` does not check `backups/`, so this is the check. The copies stay on the encrypted drive; copy that dated folder somewhere else as well if you want a backup that survives losing the drive. A sealed claim's copy is 0600, not 0400: it is a copy, and the original stays sealed.

3. **Move the release checkout to the new tag**, or make it if you don't have one yet:

   ```bash
   cd ~/Projects/career-hunter && git fetch --tags
   ```

   ```bash
   cd ~/Projects/career-hunter && git worktree add ../career-hunter-release v0.4.0
   ```

   If `~/Projects/career-hunter-release` already exists, move it instead with `cd ~/Projects/career-hunter-release && git checkout v0.4.0`. Then, either way:

   ```bash
   cd ~/Projects/career-hunter-release && uv sync --locked
   ```

4. **Add a fit to every gap tier.** In `config.yaml`, every tier under `fit_rubric.gap_tiers.tiers` needs `fit: low`, `medium` or `high`. Without one the config is refused when it is loaded: `watch`, the bot and the other commands that need it stop, and `doctor` shows the problem:

   ```yaml
   fit_rubric:
     gap_tiers:
       tiers:
         - {name: strong, max_gaps: 1, fit: high}
         - {name: possible, max_gaps: 2, fit: medium}
   ```

   While you're there, set `scoring.chat_model` and `scoring.embedding_model` (see "Run the local models").

5. **Run `doctor` before migrating**:

   ```bash
   cd ~/Projects/career-hunter-release && uv run careerhunter doctor
   ```

   `config.yaml` should say `OK`. If it lists problems, fix them and run it again. A 0.3.0 `search.db` is behind, so this line is expected, and `doctor` exits 1:

   ```text
   search.db       BAD schema version 2, expected 19; run: careerhunter migrate
   ```

6. **Migrate**:

   ```bash
   cd ~/Projects/career-hunter-release && uv run careerhunter migrate
   ```

   Expected output (the claim will be yours):

   ```text
   search.db: applied [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]; schema version 19
   ei_record_2026-27.db: applied nothing; schema version 1
   EI claim 2026-27 (from 2026-08-02)
   ```

   All of them apply in one transaction: if one fails, nothing is applied, and the backup from step 2 is untouched either way.

7. **Import your resume, then point the config at it.** Do this after step 6: `resume import` applies any pending migration itself, and migrating in step 6 keeps that change between the two `doctor` runs, right after the backup.

   ```bash
   cd ~/Projects/career-hunter-release && uv run careerhunter resume import path/to/your-resume.pdf
   ```

   It prints the number of characters and the setting to add, for example:

   ```text
   imported resume: 2,481 characters
   set search.resume: resume/current.txt in config.yaml to use it
   ```

   Then set `search.resume: resume/current.txt` in `config.yaml` (see "Import your resume").

8. **Run `doctor` again**:

   ```bash
   cd ~/Projects/career-hunter-release && uv run careerhunter doctor
   ```

   Every line should be `OK` or `--`, and these show the upgrade took:

   ```text
   search.db       OK  schema version 19
   resume/         OK  2 files, all owner-only
   resume          OK  matches its import
   ```

9. **Install the services from the release checkout**:

   ```bash
   cd ~/Projects/career-hunter-release && uv run careerhunter service install
   ```

   The output is the two `installed …` lines in Step 10c. Run `doctor` once more to see the services running.

If something goes wrong before step 6 finishes, nothing in the data folder has changed yet. If it goes wrong after, stop the services, put the dated copy back (the `.db` files and anything else you changed), and move the release checkout back to the old tag.

## Upgrading from 0.10.0 to 0.11.0

Follow the nine steps above with the tag `v0.11.0` (`git checkout v0.11.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 3, then a snapshot:** `db snapshot` is new in 0.11.0, so move the release checkout to `v0.11.0` first (step 3 changes nothing in the data folder). Then, still before any migration, take a read-only snapshot with it and check it. Use a folder that exists, on an encrypted volume, outside the data folder and outside any git checkout:

  ```bash
  cd ~/Projects/career-hunter-release && uv run careerhunter db snapshot --to /Volumes/ExampleDrive/Snapshots
  ```

  Every database line must say `integrity ok`; any other result stops the command, and the upgrade waits until it is fixed.
- **Step 4: a config at the discovery limit needs `discovery.result_pages: 1` before you migrate.** Each query now reads 2 result pages by default, and `discovery.queries_per_run` times `discovery.result_pages` times `discovery.query_delay_seconds` must be at most 120 seconds. So if `queries_per_run` times `query_delay_seconds` is more than 60 seconds (4 queries 30 seconds apart is 120, for example), `doctor` and `migrate` refuse the config until you add this under `discovery`, which keeps one page per query as before:

  ```yaml
  discovery:
    result_pages: 1
  ```

  No other change is needed.
- **Step 6:** `migrate` applies search migrations 0036 and 0037 and EI migration 0005:

  ```text
  search.db: applied [36, 37]; schema version 37
  ei_record_2026-27.db: applied [5]; schema version 5
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** optional, each once the upgrade is done: `uv run careerhunter locations fill --dry-run` lists the blank stored locations the kept files name, and `uv run careerhunter lanes reassign --dry-run` shows which stored titles the new Developer rule moves. Run either without `--dry-run` to write.

Postings alerted before the upgrade have no kept job description: their `JD` button says so. Two postings of the same role stored before the upgrade are never merged.

## Upgrading from 0.9.0 to 0.10.0

Follow the nine steps above with the tag `v0.10.0` (`git checkout v0.10.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** `config.yaml` has to change, with the services stopped and the data folder backed up (steps 1 and 2). Until it does, the config is refused when it is loaded, and `doctor` says:

  ```text
  config.yaml     BAD /Volumes/ExampleDrive/ExampleFolder/config.yaml has 1 problem(s):
    - fit_rubric.gap_tiers is no longer a setting: replace it with fit_rubric.requirement_tiers (see "Upgrading from 0.9.0 to 0.10.0" in SETUP.md). The tier now follows from core and minor gaps, in code
  ```

  1. Replace the whole `fit_rubric.gap_tiers` block with `requirement_tiers` (see "How a posting is scored"). `exempt_requirements` becomes `exempt_types`, with type names (a degree is `education`):

     ```yaml
     fit_rubric:
       requirement_tiers:
         high_max_minor: 2
         entry_level_extra_minor: 2
         exclude_at_core: 3
         exempt_types: [education]
     ```

  2. Optional: give each lane a `group` (`primary`, `career_change` or `stopgap`) in place of `fallback: true`. A lane without one is primary. A lane can't set both:

     ```yaml
     search:
       lanes:
         - name: operations
           priority: 1
           group: primary
           titles: [Operations Coordinator]
         - name: customer_success
           priority: 2
           group: stopgap
           titles: [Customer Success Specialist]
     ```

  3. Optional: `search.off_lane_min_fit: never`, so no off-lane posting is alerted.

  After changing the lanes, run `uv run careerhunter lanes reassign --dry-run` once the upgrade is done.
- **Step 5:** `doctor` exits 0 once the config is changed. Both databases are already at their schema versions:

  ```text
  search.db       OK  schema version 35
  ```

- **Step 6:** `migrate` applies nothing:

  ```text
  search.db: applied nothing; schema version 35
  ei_record_2026-27.db: applied nothing; schema version 4
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took (with the example lanes above, and `never` set):

  ```text
  lanes           OK  2 lanes (1 primary, 1 stopgap); off-lane postings are never alerted
  ```

A posting with no location is no longer skipped; ones already skipped as `empty_location` stay skipped. Postings scored before the upgrade keep their stored fit and their old tier name. One that is pending and already has a fit can still be alerted, now in the new order; an off-lane one is held with `never`. To score a posting again under the new rules, run `uv run careerhunter rescore --posting <id> --dry-run`, then without `--dry-run`.

## Upgrading from 0.8.1 to 0.9.0

Follow the nine steps above with the tag `v0.9.0` (`git checkout v0.9.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** nothing has to change. `ats_facets` on a Workday watchlist entry is new and optional (see "Which setting the watcher reads"). If you add it, do so with the services stopped and the data folder backed up (steps 1 and 2).
- **Step 5:** `doctor` exits 1, because `search.db` is one migration behind:

  ```text
  search.db       BAD schema version 34, expected 35; run: careerhunter migrate
  ```

  The claim's record is already at schema version 4.
- **Step 6:** `migrate` applies one migration to `search.db` and none to the claim's record. Migration 0035 adds the `board_status` table, which holds each faceted Workday board's last facet result. No existing row changes:

  ```text
  search.db: applied [35]; schema version 35
  ei_record_2026-27.db: applied nothing; schema version 4
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took:

  ```text
  search.db       OK  schema version 35
  workday facets  --  none set (ats_facets in a watchlist entry)
  ```

  With `ats_facets` set, the `workday facets` line is OK, or a WARN per board with a name not found once the board has been read.

## Upgrading from 0.8.0 to 0.8.1

Follow the nine steps above with the tag `v0.8.1` (`git checkout v0.8.1` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** nothing has to change. Criteria labels are new and optional: with the services stopped and the data folder backed up (steps 1 and 2), you can give each rule a short label after its kind, such as `2. veto [short contract]: …`, so a veto's skip reason and the `withdrawn:` message name it (see "Your scoring criteria"). Keep a copy of the old file next to the backup and keep the file owner-only. A label must be 1 to 40 characters with no brackets inside; an empty, longer or unclosed one makes the file unusable, and `doctor` shows `criteria        BAD a rule has no kind or a bad label …`. A rule without a label still works, and its skip reason now ends in `…` when the rule goes on past six words.
- **Step 5:** `doctor` exits 1, because `search.db` is one migration behind:

  ```text
  search.db       BAD schema version 33, expected 34; run: careerhunter migrate
  ```

  The claim's record is already at schema version 4.
- **Step 6:** `migrate` applies one migration to `search.db` and none to the claim's record. Migration 0034 rebuilds `score_calls` and `component_health`, copying every row as it was, so a scoring call that times out is recorded as `timeout`, and adds the `score_timeouts` table:

  ```text
  search.db: applied [34]; schema version 34
  ei_record_2026-27.db: applied nothing; schema version 4
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took:

  ```text
  search.db       OK  schema version 34
  score timeouts  OK  none set aside
  ```

  With a criteria file, the `criteria` line reads as before, labels or not.

## Upgrading from 0.7.0 to 0.8.0

Follow the nine steps above with the tag `v0.8.0` (`git checkout v0.8.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** if you use `search.criteria_file`, rewrite the file as numbered rules, one per line, each with its kind (see "Your scoring criteria"). A file still in prose, or with a rule that has no kind, is refused, and scoring holds until it is rewritten. With the services stopped and the data folder backed up (steps 1 and 2), keep a copy of the old file next to the backup, then write each criterion as `1. veto: …` or `2. cap medium: …`, and keep it owner-only. Without a criteria file, nothing has to change. Two things are new and optional: `scoring.run_minutes` (default 20) and the `withdrawn:` message.
- **Step 5:** `doctor` exits 1, because `search.db` is three migrations behind:

  ```text
  search.db       BAD schema version 30, expected 33; run: careerhunter migrate
  ```

  The claim's record is already at schema version 4. A criteria file still in prose also shows `criteria        BAD a line is not a numbered rule (write one rule per line as 1. …); scoring is held`, and one with rules but no kinds shows `BAD a rule has no kind …`.
- **Step 6:** `migrate` applies three migrations to `search.db` and none to the claim's record. Migrations 0031 and 0033 rebuild `component_health`, and 0032 and 0033 rebuild `score_calls`, each copying every row as it was; 0032 adds the `withdrawals` table:

  ```text
  search.db: applied [31, 32, 33]; schema version 33
  ei_record_2026-27.db: applied nothing; schema version 4
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took:

  ```text
  search.db       OK  schema version 33
  criteria        OK  3 rules (2 vetoes, 1 cap), 214 of 4,000 characters; sent with each scoring call
  criteria vetoes OK  0 vetoed, 0 rejected; 0 capped, 0 rejected in 14 days
  ```

  Without a criteria file, the two criteria lines show `--`.

## Upgrading from 0.6.0 to 0.7.0

Follow the nine steps above with the tag `v0.7.0` (`git checkout v0.7.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** delete one line. `search.location_rules.allow_travel_outside` is no longer a setting: travel never skips a posting, and regular office days outside your `onsite_hybrid` places always do. With the services stopped and the data folder backed up (steps 1 and 2), open `config.yaml` and delete the `allow_travel_outside:` line under `search.location_rules`, and nothing else. A config that still has it is refused, and `doctor` shows `config.yaml` as BAD with the line `search.location_rules.allow_travel_outside is no longer a setting: delete that line from config.yaml (after a backup)`. Two things are new and optional: `discovery.max_candidates_per_day` (default 3) and the `/skip` command.
- **Step 5:** `doctor` exits 1, because `search.db` is three migrations behind and the claim's record one:

  ```text
  search.db       BAD schema version 27, expected 30; run: careerhunter migrate
  ```

- **Step 6:** `migrate` applies three migrations to `search.db` and one to the claim's record. EI migration 0004 rebuilds the status history for two new statuses and copies every row as it was:

  ```text
  search.db: applied [28, 29, 30]; schema version 30
  ei_record_2026-27.db: applied [4]; schema version 4
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took:

  ```text
  config.yaml     OK
  search.db       OK  schema version 30
  claim 2026-27   OK  schema version 4
  ```

  Discovery candidates queued before 0.7.0 have no linked postings, so they are not sent again; `careerhunter discover list` still lists them.

## Upgrading from 0.5.1 to 0.6.0

Follow the nine steps above with the tag `v0.6.0` (`git checkout v0.6.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** nothing has to change. A 0.5.1 `config.yaml` is valid as it is. Two things to know:
  - With no `scoring.embedding_model`, embeddings are now off: no request, no `embedding miss` line. Set it only if LM Studio runs an embedding model.
  - To give the scorer your own criteria, see "Your scoring criteria". It is optional.
- **Step 5:** `doctor` exits 1 with this line, because `search.db` is two migrations behind:

  ```text
  search.db       BAD schema version 25, expected 27; run: careerhunter migrate
  ```

  The claim's record is already at schema version 3.
- **Step 6:** `migrate` applies two migrations to `search.db` and none to the claim's record:

  ```text
  search.db: applied [26, 27]; schema version 27
  ei_record_2026-27.db: applied nothing; schema version 3
  EI claim 2026-27 (from 2026-08-02)
  ```

- **Step 7:** skip it.
- **Step 8:** these lines show the upgrade took:

  ```text
  search.db       OK  schema version 27
  criteria        --  not set (optional: search.criteria_file)
  scoring calls   --  no scoring call in 14 days
  ```

  `scoring calls` fills in as watch runs score postings. If you set `search.criteria_file`, the `criteria` line shows its size instead.

## Upgrading from 0.5.0 to 0.5.1

Follow the nine steps above with the tag `v0.5.1` (`git checkout v0.5.1` in the release checkout, then `uv sync --locked`). These steps differ:

- **Before step 5:** store LM Studio's API token (see "Run the local models"), once, from either checkout:

  ```bash
  uv run careerhunter secret set lm_studio_token
  ```

- **Step 4:** nothing to add. A 0.5.0 `config.yaml` is valid as it is. For a reasoning chat model, the default `scoring.timeout_seconds` of 300 is a good start.
- **Step 5:** `doctor` exits 1 with this line, because `search.db` is four migrations behind:

  ```text
  search.db       BAD schema version 21, expected 25; run: careerhunter migrate
  ```

  The active claim's record is one behind, at schema version 2.
- **Step 6:** `migrate` applies four migrations to `search.db` and one to the claim's record. The claim will be yours:

  ```text
  search.db: applied [22, 23, 24, 25]; schema version 25
  ei_record_2026-27.db: applied [3]; schema version 3
  EI claim 2026-27 (from 2026-08-02)
  ```

  A sealed claim's record is not migrated, and is still read.
- **Step 7:** skip it. The resume was imported for 0.4.0.
- **Step 8:** these lines show the upgrade took:

  ```text
  search.db       OK  schema version 25
  claim 2026-27   OK  schema version 3
  LM Studio       OK  answers with the API token
  git push        OK  pre-push guard installed, shared with another worktree
  ```

  The last line is the release checkout's: it shares the first checkout's hooks, so it no longer warns. If discovery was backing off, `discovery WARN` shows until when, with `careerhunter discover clear-backoff` to end it.
- **After step 9:** applications recorded by `import` or `/log` before this release may have no lane. Check them, and see any other moves, without changing anything:

  ```bash
  cd ~/Projects/career-hunter-release && uv run careerhunter lanes reassign --dry-run
  ```

  Postings not checked yet are listed as `not checked -> <lane>`. If the list looks right, run it without `--dry-run`.

## Upgrading from 0.4.0 to 0.5.0

Follow the nine steps above with the tag `v0.5.0` (`git checkout v0.5.0` in the release checkout, then `uv sync --locked`). These steps differ:

- **Step 4:** nothing to add. A 0.4.0 `config.yaml` is valid as it is.
- **Step 5:** `doctor` exits 1 with this line, because `search.db` is two migrations behind:

  ```text
  search.db       BAD schema version 19, expected 21; run: careerhunter migrate
  ```

  The active claim's record is one behind, at schema version 1.
- **Step 6:** `migrate` applies two migrations to `search.db` and one to the claim's record. The claim will be yours:

  ```text
  search.db: applied [20, 21]; schema version 21
  ei_record_2026-27.db: applied [2]; schema version 2
  EI claim 2026-27 (from 2026-08-02)
  ```

  Applications recorded before this release have no job description until one is attached (see "Backfilling applications and their job descriptions"). A sealed claim's record is not migrated, and is still read.
- **Step 7:** skip it. The resume was imported for 0.4.0.
- **`imports/`:** `careerhunter import` reads only files under `imports/` in the data folder, and `migrate` doesn't create that folder. After step 6, run `init`. It creates `imports/` (0700), keeps every file that already exists, and changes nothing else:

  ```bash
  cd ~/Projects/career-hunter-release && uv run careerhunter init
  ```

- **Step 8:** these lines show the upgrade took. The count is yours, and `imports/` counts the files you put there:

  ```text
  search.db       OK  schema version 21
  claim 2026-27   OK  schema version 2
  imports/        OK  0 files, all owner-only
  job descriptions --  14 applications from before JDs were kept, with no JD
  ```

  `job descriptions` turns to `WARN` while an application recorded on 0.5.0 is waiting for its job description.
- **Step 9:** if `service install` stops with `<label> is disabled in launchd`, or with `launchctl bootstrap failed (5)`, launchd has a disabled override for that label, and `uninstall` leaves it in place. See the two launchd rows in "Troubleshooting". It is never fixed with root or sudo.
- **After step 9:** 0.5.0 matches titles to lanes differently, and postings stored before it keep the lane they were given. See which would move, without changing anything:

  ```bash
  cd ~/Projects/career-hunter-release && uv run careerhunter lanes reassign --dry-run
  ```

  It lists each move and the counts, and writes nothing. If the list looks right, run it without `--dry-run` to apply exactly those moves (see "After you change `search.lanes`" in Step 7). Without `search.lanes` it refuses, and there is nothing to move.

## Troubleshooting

| You see | What it means | Fix |
|---|---|---|
| `REFUSING TO START: CAREERHUNTER_DATA_DIR is not set` | Step 5 didn't take effect in this window | Run `source ~/.zshrc`, or open a new Terminal window |
| `… resolves to the boot volume. Either the external drive is not mounted…` | The drive is unplugged or locked, or the path has a typo | Plug in and unlock the drive, then check `ls /Volumes` |
| `… is NOT encrypted` | The drive isn't encrypted yet | Step 2 |
| `… is encrypted but locked` | The drive needs its password | Open it in Finder and enter the password |
| `… is inside the code repository` / `… inside a git work tree` | The data folder points somewhere inside a git project | Point `CAREERHUNTER_DATA_DIR` at the external drive |
| `LM Studio BAD unauthorized: LM Studio refused the API token` | The token in the Keychain is missing, old, or not LM Studio's current one | Copy the token from LM Studio's server settings and run `uv run careerhunter secret set lm_studio_token` |
| `LM Studio BAD unreachable at 127.0.0.1:1234` | LM Studio's server isn't running, or not on port 1234 | Start the server in LM Studio and load the models |
| `discovery WARN backing off until …` | Recent discovery passes failed: SearXNG was down, or every engine was throttled | Wait, or run `uv run careerhunter discover clear-backoff` to try at the next due pass |
| `… is readable by other users … chmod 700` | Folder permissions are too open | Run the `chmod` command it prints |
| `seed file has N problem(s)` | Something in `seed.yaml` needs fixing | Each line names the entry and field. Fix them and re-run |
| `… is before claim 2026-27 started` | A seed date falls outside your claim | Fix the date, or see `--route-out-of-claim-to-search` in Step 8 |
| `REFUSING TO IMPORT: the import file must be under imports/ in the data folder` | `careerhunter import` reads only from `imports/` | Move the file (and its `jd_file`s) into `imports/` and run it again |
| `import file has N problem(s)` | The import file isn't a version 1 file, or isn't valid JSON | Each line names the problem. Compare with `import.example.json` |
| `refusing to write until these are fixed: … Run: careerhunter claim seal …` | An earlier claim's record isn't sealed yet | See "When a claim ends" |
| `claim … BAD the active claim has no record file` | You set a claim but haven't created its file | Run `uv run careerhunter migrate` |
| `search.db BAD schema version 2, expected 37` | The code was updated and `search.db` hasn't caught up | Follow "Upgrading to a new release": stop the services, back up, then `uv run careerhunter migrate` from the release checkout, and install the services again |
| `watch: aborted: …` in `logs/watch.log` | The run stopped before it finished: config, database, or the log itself | The rest of the line names the cause. Fix it; the next run starts over |
| `run budget reached; left for the next run` | The run used its 600 seconds | Nothing to fix. The next run fetches those boards first |
| `resume BAD the resume file does not match its import` | `resume/current.txt` was edited, or an import was interrupted | Run `uv run careerhunter resume import` again with your file |
| `resume BAD no resume has been imported` | `search.resume` is set but nothing was imported | Run `uv run careerhunter resume import <file>`, or set `search.resume: null` |
| `resume BAD search.resume must be resume/current.txt` | `search.resume` names another file | Set it to `resume/current.txt` |
| `resume WARN left by an interrupted or running import` | An import stopped part-way, or one is running now | Nothing to fix. The next import removes them |
| `watch: scoring: resume not usable (…)` | The resume can't be used this run, for the reason shown | Run `uv run careerhunter doctor` and fix what it reports. Nothing is scored until then |
| `watch: scoring: model unreachable; N postings left for the next run` | The local model stopped answering | Start the model server. The next run scores them |
| `… left unscored (model took longer than N s …); moved to the back of the queue` | The model was still working on that posting when `scoring.timeout_seconds` ran out | Nothing to fix for one. The run went on, and the posting is tried again after the others. If `doctor`'s `scoring calls` line shows many timeouts, raise the timeout or use a faster model |
| `score timeouts WARN N postings timed out 2 times in a row; set aside and not scored` | Those postings timed out on two runs in a row and are no longer sent to the model | Run `uv run careerhunter rescore --set-aside` to have the next run try them again, after raising `scoring.timeout_seconds` if they keep timing out |
| `workday facets WARN <board>: "<facet>: <value>" not found last run …` | A name in that board's `ats_facets` wasn't in its facets on the last read, so the board was read unfiltered | Check the name against the board's search filters. If it was renamed, update `ats_facets`; if no posting has that value now, nothing to fix |
| `config.yaml BAD fit_rubric.gap_tiers is no longer a setting …` | The config still has the gap-count rubric of 0.9.0 and earlier | Replace `gap_tiers` with `requirement_tiers` (see "Upgrading from 0.9.0 to 0.10.0") |
| `watch held WARN … with no fit` | Postings were scored under a tier name that isn't high, medium or low, or `fit_rubric.requirement_tiers` isn't set | Set `requirement_tiers`, or re-score them with `careerhunter rescore --posting <id>` |
| `watch run BAD the last run started N min ago and did not finish` | A watch run crashed, was stopped, or hit a database error | See `logs/watch.log`. Its counts are not used; the next finished run counts again |
| `bot alerts BAD held since …` | The bot couldn't read `config.yaml` at its last poll, so it sent no watcher alerts | Fix `config.yaml` (`doctor` shows the problem). The next poll sends them |
| The bot replies `Config can't be read, so nothing was recorded. Run careerhunter doctor.` | `/log` needs `config.yaml` for the date and the claim check | Fix `config.yaml`, then send the link again |
| `lanes -- not configured` | `search.lanes` isn't set, so lane checking is off | Nothing to fix if you meant it. Add lanes to label and order alerts |
| `config.yaml BAD … needs an end date` | A claim that isn't active has no `end` | Add its end date, or check `active_claim` for a typo |
| `org.careerhunter.bot is disabled in launchd. Run: launchctl enable gui/…/org.careerhunter.bot, then careerhunter service install` (or the watch label), from `service install` or on a `doctor` service line | launchd has a disabled override for that label. `service install` checks before it writes anything, and stops. The override is launchd's own, kept per user and per label: it outlasts `careerhunter service uninstall`, which only removes the plist, so installing again fails the same way until it is cleared. career-hunter never clears it for you | Run the command it prints, `launchctl enable gui/$(id -u)/<label>`, as yourself, then `uv run careerhunter service install` |
| `launchctl bootstrap failed (5). Check launchctl print-disabled gui/… for <label>` | launchd refused to load the agent. Error 5 most often means the label is disabled | Run `launchctl print-disabled gui/$(id -u)`. If the label shows `disabled` or `true`, run `launchctl enable gui/$(id -u)/<label>`, then `uv run careerhunter service install`. It is a user agent: it never needs root |
| `bot service WARN … not this CAREERHUNTER_DATA_DIR` (or `watch service`) | The agent loaded for your user serves another data folder than the one this `doctor` run checked | Nothing to fix while you are testing against another data folder. Otherwise run `uv run careerhunter service uninstall`, then `service install` from the release checkout with the right `CAREERHUNTER_DATA_DIR` |
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

`doctor` against the test volume still describes the services loaded for your user: their lines name the real data folder and say `not this CAREERHUNTER_DATA_DIR`. Don't install the services against the test volume.

When you're done, eject `CHBase` and `CHTest` in Finder, then move `~/ch-test.sparseimage` to the Trash.
