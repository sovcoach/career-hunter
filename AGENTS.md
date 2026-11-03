# Working rules for agent sessions

These rules apply to every AI agent session in this repo, Claude Code or otherwise. [CONTRIBUTING.md](CONTRIBUTING.md) has the project's ground rules. This file covers how an agent works within them.

## Approval

- Plan first. Every phase and every non-trivial step starts with a written plan. No code, config writes, commits, or pushes until that plan is explicitly approved. An approval covers only the step that was approved.
- Sessions start from the repo directory so project permission rules load. Merges are done by the operator, locally, as described in "Merging" below. Public commits are authored as Daniel with the GitHub noreply address.
- Commit, push, tag, publish a release or open a PR only after the operator explicitly approves that step. An approval covers only the step it was given for.
- Before every commit, show the staged file list and the commit message, then wait.
- Stop on any hook failure. Report it and fix the cause. Never skip a hook, bypass it, point git at other hooks, or use the push guard's override.
- Never push to `main`, and never merge a PR: the operator merges locally (see "Merging"). Branch from an up-to-date `main` with `--no-track`, and push with `-u` to a branch of the same name.

## Merging

Pull requests are merged locally by the operator, never through GitHub's web UI. A web-UI merge commit carries the local time zone offset; a local merge made with `TZ=UTC` does not. Agents never merge and never push `main`.

1. The pull request's CI checks have passed.
2. Update `main`: `git switch main`, then `git pull --ff-only`.
3. Merge with a merge commit, in UTC: `TZ=UTC git merge --no-ff <branch> -m "Merge pull request #<N> from sovcoach/<branch>"`.
4. Run `make check` on the result. If it fails, don't push: discard the merge with `git reset --hard origin/main` and fix it on the branch.
5. Push `main` with the pre-push guard's manual override: `CAREERHUNTER_ALLOW_PUSH_MAIN=1 git push origin main`. Branch protection requires a pull request and passing status checks, so GitHub reports "Bypassed rule violations" for those two rules. That is expected on this push only: administrators may bypass (ADR-012). Never force-push `main`.
6. GitHub marks the pull request merged once its head commit is on `main`. Then delete the branch, on GitHub and locally.

## Git commands

- Run each guarded git command (`add`, `commit`, `push`, `rm`, `tag`, `merge`, `rebase`, `reset`) as a command of its own. Chain nothing before or after it with `;`, `&&`, `||` or `|`. The ask rules in `.claude/settings.json` match the whole command, so a chained command can run without the prompt it should get (ADR-011).
- Commit and tag with UTC timestamps: `TZ=UTC git commit …` and `TZ=UTC git tag -a …`.
- No attribution lines anywhere: no `Co-Authored-By` trailers and no "generated with" footers in commits, tags, PRs, issues or release notes.
- Commit messages and command text must not contain the push guard's override variable or the hook-skipping options. The deny rules refuse any command that does, even inside a message.

## Data

- Use synthetic data only in tests, fixtures, examples and verification runs: made-up companies, people, postings and places. Never use real job-search data.
- The real data folder belongs to the operator. Run only the commands they approved there, one step at a time.

## Privacy in public files

Public means anything that leaves the machine: code, docs, ADRs, the CHANGELOG, templates, tests, commit and tag messages, PR and issue text, and release notes.

- Use placeholder paths only: `/Volumes/ExampleDrive/ExampleFolder` for the data folder and `/Users/example/ExampleFolder/career-hunter` for the repo. Never write a real drive, home or user path.
- Never reveal the operator's location, time zone or region, or why they chose a setting. Examples use generic values such as `UTC`, a well-known IANA zone, or fictional places.
- Real paths and settings live only in the private config, the locally generated launchd plist and the data folder.
- Before committing, scan the diff for paths and location terms, and run `make verify-tree` when adding new files.
