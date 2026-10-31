# Setup guide

**In plain language:** this guide takes you from nothing to a working career-hunter on a Mac that stays on, such as a Mac mini. You don't need to be a developer. Every command below can be copied and pasted into the **Terminal** app (press ⌘-Space, type "Terminal", press Return). After each step you'll see what the output should look like, and what to do if it doesn't.

It takes about 20 minutes, plus however long your drive takes to encrypt.

---

## What you need

- A Mac running a recent macOS. The steps were tested on macOS 26.
- An **external drive** that will hold your data, formatted as **APFS**. This guide calls it `X10-Pro`; use your own drive's name wherever you see that.
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

career-hunter **refuses to write to an unencrypted drive**. In Finder, right-click the drive in the sidebar and choose **Encrypt "X10-Pro"…**. Pick a strong password and store it in your password manager.

Encryption runs in the background, and the drive stays usable while it does.

Check it:

```bash
diskutil info /Volumes/X10-Pro | grep -E "FileVault|Encrypted|Locked"
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

This adds one line to your shell settings so every new Terminal window knows where the data folder is. Change `X10-Pro` if your drive has another name.

```bash
echo 'export CAREERHUNTER_DATA_DIR=/Volumes/X10-Pro/CareerHunterData' >> ~/.zshrc && source ~/.zshrc
```

Check it:

```bash
echo $CAREERHUNTER_DATA_DIR
```

Expected output:

```text
/Volumes/X10-Pro/CareerHunterData
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
database $CAREERHUNTER_DATA_DIR/careerhunter.db at schema version 1
```

Running `init` again is safe. It keeps existing files and says `kept`.

## Step 7: Fill in your settings

Open the data folder in Finder:

```bash
open "$CAREERHUNTER_DATA_DIR"
```

Edit these with TextEdit, or any plain-text editor:

- **`config.yaml`**: your target job titles, locations, work models, salary floor, fit rubric, schedule and the companies you want to watch.
- **`exclusions.yaml`**: companies and keywords to skip.
- **`seed.yaml`**: the applications and activities you've *already* done. Delete the example entries and add your own. The comments at the top explain each field. If you only remember roughly when you applied, use `date_precision: week` with `week_of`. If you don't remember at all, use `date_precision: unknown`. Don't guess an exact date. Include the job ad's `url` (or `requisition_id`) whenever you have it: it's how career-hunter tells a re-posted role apart from the original.

Then check your files:

```bash
uv run careerhunter doctor
```

Expected output:

```text
data dir        OK  /Volumes/X10-Pro/CareerHunterData (volume /Volumes/X10-Pro, encrypted)
database        OK  schema version 1
db permissions  OK  0600
config.yaml     OK
exclusions.yaml OK
secret          telegram_bot_token: missing
```

`telegram_bot_token: missing` is expected until Phase 2.

## Step 8: Import your history

```bash
uv run careerhunter seed
```

Expected output (your numbers will differ):

```text
companies          4 inserted     0 already present
postings           4 inserted     0 already present
applications       4 inserted     0 already present
status_history     4 inserted     0 already present
activities         3 inserted     0 already present
```

You can fix `seed.yaml` and run this again at any time. Entries already imported are recognized and skipped, so nothing is duplicated. Existing entries aren't changed by re-running: the record is permanent by design. A mistaken entry is *voided* with a reason rather than deleted. The record keeps it, but reports leave it out.

## Step 9: Produce an EI job-search report

Pick the period you need, in `YYYY-MM-DD` format:

```bash
uv run careerhunter export --from 2026-09-01 --to 2026-09-30
```

Expected output. Again, your real output shows the full folder path in place of `$CAREERHUNTER_DATA_DIR`:

```text
3 applications, 3 activities
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-09-01_2026-09-30_<time>.pdf
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-09-01_2026-09-30_<time>_applications.csv
wrote $CAREERHUNTER_DATA_DIR/exports/ei_2026-09-01_2026-09-30_<time>_activities.csv
```

The PDF is the readable report. The CSV files open in Numbers or Excel. Applications and activities without a known date appear in a separate **Date unknown** section of every report, so nothing is left out.

## Step 10 (Phase 2 onward): Store the Telegram bot token

You don't need this yet. When the time comes:

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
CAREERHUNTER_DATA_DIR=/Volumes/CHTest/CareerHunterData uv run careerhunter init
```

When you're done, eject `CHBase` and `CHTest` in Finder, then move `~/ch-test.sparseimage` to the Trash.
