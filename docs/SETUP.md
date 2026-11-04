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
| How many missing requirements still count | `fit_rubric.gap_tiers` |
| A resume file inside the data folder, for a similarity line | `search.resume` |
| Languages, credentials, shifts, pay, seniority (in the title, or also the description), and a minimum salary | `hard_gates` |
| A cap on weekend days | `schedule_limits` |
| Which roles to keep at one company | that company's `role_filters` |
| A staffing agency | `agency: true` on that watchlist entry |
| Companies, title keywords, description keywords, and one role | `exclusions.yaml` |

The templates use placeholder names such as Example Robotics, Example City, and Example Staffing Mill.

`search.target_titles`, `search.locations`, and `search.work_models` still validate so an older file loads. The watcher does not match them. Use lanes and location rules.

`fit_rubric.weights` and `fit_rubric.thresholds` are not applied. Scoring uses `gap_tiers` and leaves the numeric rating empty.

`fit_rubric.embedding_weight` is validated and the watcher does not apply it. The watcher does not blend it into the score or the alert. `careerhunter embed-eval` reads it when ranking a report. `search.resume: null` skips embeddings.

`hard_gates.max_commute_km` and `hard_gates.exclude_work_models` are not applied.

`hard_gates.seniority_words`, `title_exceptions` and `never_gated_families` match whole words of the title, ignoring case: "Lead" matches "Team Lead" and "(Lead)", but not "Leader". List a plural on its own if it should count. `hard_gates.seniority_in_description: true` also checks the description for seniority words; it is false by default, so a junior title whose description mentions senior colleagues is kept. A key under `hard_gates` that career-hunter does not know, such as a misspelled option, is refused when the config is loaded.

One `role_filters` block requires every family, work model, and place it lists. It cannot say "remote, or else Example City." Treating that as an AND would hide roles that should stay. There is no field for that or-rule yet.

### Refresh the scout prompt

When a posting comes from a scout rather than `careerhunter watch`, the scout's instructions have to match the exclusions and the hard gates. After you change either file:

```bash
uv run careerhunter scout-prompt
```

Paste the text into your private scout setup. The command prints to the terminal and writes nothing. It does not include your chat id, and it does not include gates the watcher does not skip on.

With the template files, the prompt names Example Staffing Mill, the title keyword Senior Director, and the shift pattern overnight. Yours will name whatever you put in the two files.

### Find companies beyond the watchlist

SearXNG runs on this Mac and listens on `127.0.0.1` only. Leave `public_instance` and `image_proxy` off, leave the limiter off, and add `json` to `search.formats`. Leave engines that need an API key disabled. Set `discovery.searxng_url` to that loopback URL, for example `http://127.0.0.1:8888/search`. A null URL leaves discovery off.

`careerhunter discover` sends at most 12 site-restricted queries. It prints each new pending row and stores it in `search.db`. It does not fetch the page. A down server prints `discovery miss: unreachable` and stores nothing. `careerhunter discover list` prints the pending rows again.

```bash
make discover
careerhunter discover approve 4
careerhunter discover reject 4
```

`approve` marks that pending row approved. A board becomes a company with `watchlist` 1, and a new company has `source` discovery. The next `careerhunter watch` fetches that board. A career page stores `careers_url` and is not fetched. `reject` stores the rejection and inserts no company. Neither command rewrites `config.yaml` or `exclusions.yaml`.

The bot does the same thing from the allowlisted chat. Each pending row is one message with the suggested name, the URL, and Approve and Reject. When more than five are waiting, one message lists up to eight, and the rest follow on a later poll. A tap that does not apply answers `Not changed.` and leaves the row pending.

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

**Before you start, know what Telegram sees.** Bot chats aren't end-to-end encrypted, so what the bot sends and what you send pass through Telegram's servers. The bot keeps its own messages minimal: company, title, link, status words and short references like `a7`. It never sends your notes, fit details or EI record details. What you type (activity descriptions, skip reasons) and any job-ad files you send do go to Telegram, so don't type anything sensitive. See ADR-015 in docs/DECISIONS.md.

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

A null `discovery.searxng_url` leaves the discovery agent out. `doctor` then shows `discovery agent --  off`. Set the URL and run `service install` again. It adds `org.careerhunter.discover` and leaves the bot and watch agents as they are. The new line looks like:

```text
installed ~/Library/LaunchAgents/org.careerhunter.discover.plist; careerhunter discover runs now and every 1440 minutes
```

That agent runs once and exits, then waits for `discovery.interval_minutes`. A finished search writes `logs/discover.success`, and its lines go to `logs/discover.log`:

```text
discovery miss: unreachable
discover: 0 queued
```

List, approve and reject write no log. Approve and reject do not refresh that stamp. `doctor` shows the agent and how old the stamp is. A stamp older than two intervals is a failure. A warning, such as a plist left behind after the URL is set back to null, does not fail `doctor`. `service uninstall` removes the bot, the watch agent, and the discovery agent.

### 10d. Using it

| Send | What happens |
|---|---|
| `/log <link>` | Records a posting you found and shows it with **Applied** and **Skipped** buttons. If the page doesn't show the company and title, send `/log <link> \| Company \| Title`. |
| **Applied** | The bot asks how you applied, then when: **Today**, **Yesterday**, **This week**, **Last week** or **Other date** (reply with YYYY-MM-DD). It saves a copy of the job ad. If it can't, it asks you to reply with a PDF or screenshot of the ad. |
| **Skipped** | Asks why, then records the skip. Skips never go into the EI record. |
| `/status a7 interview_scheduled` | Updates an application. References like `a7` come from `/recent` and last 7 days. Send just `/status` to choose with buttons. |
| `/activity networking 2026-10-27 Coffee with a recruiter` | Records a job-search activity. Leave out the date to choose it with buttons. |
| `/recent` | Your latest applications, their references and current statuses. |

Every date must fall inside your active EI claim, as with `seed`, and future dates are refused. Buttons work once: tap a button twice, or use an old one, and the bot says it has expired.

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
| `search.db BAD schema version 6, expected 7` | The code was updated and `search.db` hasn't caught up | Stop the services with `uv run careerhunter service uninstall`, run `uv run careerhunter migrate`, then `uv run careerhunter service install` |
| `watch: aborted: …` in `logs/watch.log` | The run stopped before it finished: config, database, or the log itself | The rest of the line names the cause. Fix it; the next run starts over |
| `run budget reached; left for the next run` | The run used its 600 seconds | Nothing to fix. The next run fetches those boards first |
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
