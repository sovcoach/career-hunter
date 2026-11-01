# Working rules for agent sessions

These rules apply to every AI agent session in this repo, Claude Code or otherwise. [CONTRIBUTING.md](CONTRIBUTING.md) has the project's ground rules. This file covers how an agent works within them.

## Approval

- Commit, push, tag, publish a release or open a PR only after the operator explicitly approves that step. An approval covers only the step it was given for.
- Before every commit, show the staged file list and the commit message, then wait.
- Stop on any hook failure. Report it and fix the cause. Never skip a hook, bypass it, point git at other hooks, or use the push guard's override.
- Never push to `main`, and never merge a PR: the operator merges on GitHub. Branch from an up-to-date `main` with `--no-track`, and push with `-u` to a branch of the same name.

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
