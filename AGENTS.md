# Working rules for agent sessions

These rules apply to every AI agent session in this repo, Claude Code or otherwise. [CONTRIBUTING.md](CONTRIBUTING.md) has the project's ground rules. This file covers how an agent works within them.

## Approval

- Plan first. Every phase and every non-trivial step starts with a written plan, as a numbered commit list. No code, config writes, commits, or pushes until that plan is explicitly approved. An approval covers only the plan it was given for.
- Sessions start from the repo directory so project permission rules load. Merges are done by the operator on GitHub with “Create a merge commit”, as described in "Merging" below. Public commits are authored as Daniel with the GitHub noreply address.
- Inside an approved plan, getting your own uncommitted code to pass is normal authoring. Fix failures yourself (tests, lint, types, formatting, hook autofixes, a missing environment variable) and keep going. Commit each step of the plan once its full checks pass. List anything notable in the push-review summary.
- Stop and ask only when:
  1. The plan's scope or design would change. That includes an extra commit, a different approach, or an acceptance criterion you can't meet as written.
  2. A fix would touch code outside this batch, or weaken a check: a noqa in `src`, a loosened test, a skipped hook, a coverage drop, a lint rule disabled.
  3. A dependency is added or changed.
  4. Anything touches the real data folder, or any push, PR or issue post.
  5. You're stuck on the same failure after two honest attempts.
- These hold in every plan:
  - Format with `uv run ruff format` only, never `make format`, which also runs `ruff check --fix`.
  - `CAREERHUNTER_DATA_DIR` comes from the bot's LaunchAgent plist (`org.careerhunter.bot.plist`, read with PlistBuddy) and is never printed. Set it on each check command and on each `git commit`.
  - Run each guarded git command on its own (see "Git commands").
  - The full checks pass fresh before every commit.
  - Never skip a hook, bypass it, point git at other hooks, or use the push guard's override.
- Push, tag, publish a release, open a PR or post to an issue only after the operator explicitly approves that step. An approval covers only the step it was given for. Before asking to push, show the commits since `origin/main`, the check results, and what was done differently from the plan and why.
- Never push to `main`, and never merge a PR: the operator merges (see "Merging"). Branch from an up-to-date `main` with `--no-track`, and push with `-u` to a branch of the same name.

## Merging

The operator merges each pull request on GitHub with "Create a merge commit" once its CI checks pass. Agents never merge, never push `main`, and never force-push `main`.

A local merge is optional. If the operator merges locally instead:

1. Update `main`: `git switch main`, then `git pull --ff-only`.
2. Merge with a merge commit: `git merge --no-ff <branch> -m "Merge pull request #<N> from sovcoach/<branch>"`.
3. Run `make check` on the result. If it fails, don't push: discard the merge with `git reset --hard origin/main` and fix it on the branch.
4. Push `main` with the pre-push guard's manual override: `CAREERHUNTER_ALLOW_PUSH_MAIN=1 git push origin main`. Branch protection requires a pull request and passing status checks, so GitHub reports "Bypassed rule violations" for those two rules. That is expected on this push only: administrators may bypass (ADR-012).

Either way, delete the branch on GitHub and locally once the pull request shows as merged.

## Git commands

- Run each guarded git command (`add`, `commit`, `push`, `rm`, `tag`, `merge`, `rebase`, `reset`) as a command of its own. Chain nothing before or after it with `;`, `&&`, `||` or `|`. The ask rules in `.claude/settings.json` match the whole command, so a chained command can run without the prompt it should get (ADR-011).
- No attribution lines anywhere: no `Co-Authored-By` trailers and no "generated with" footers in commits, tags, PRs, issues or release notes.
- Commit messages and command text must not contain the push guard's override variable or the hook-skipping options. The deny rules refuse any command that does, even inside a message.

## Data

- Use synthetic data only in tests, fixtures, examples and verification runs: made-up companies, people, postings and places. Never use real job-search data.
- The real data folder belongs to the operator. Run only the commands they approved there, one step at a time.

## Privacy in public files

Public means anything that leaves the machine: code, docs, ADRs, the CHANGELOG, templates, tests, commit and tag messages, PR and issue text, and release notes.

- Use placeholder paths only: `/Volumes/ExampleDrive/ExampleFolder` for the data folder and `/Users/example/ExampleFolder/career-hunter` for the repo. Never write a real drive, home or user path.
- Never reveal the operator's location or region, username, or personal criteria, or why they chose a setting. A time zone name that points to a region counts as location; time zone offsets in commit and tag metadata are fine. Examples use generic values such as `UTC`, a well-known IANA zone, or fictional places.
- Real paths and settings live only in the private config, the locally generated launchd plist and the data folder.
- Before committing, scan the diff for paths and location terms, and run `make verify-tree` when adding new files.
