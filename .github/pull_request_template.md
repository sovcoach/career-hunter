## What and why

<!-- What does this change, and why is it needed? Link the issue: Closes #NN -->

## How it was tested

- [ ] `make check` passes locally (lint, types, tests with coverage, pre-commit)
- [ ] New behaviour has tests on synthetic fixtures

## Safety checklist

- [ ] No real job-search data, personal details, or data-folder file paths in the diff
- [ ] No secrets; anything sensitive goes in the Keychain via `careerhunter secret set`
- [ ] No deletes added anywhere in the record (EI 6-year retention)
- [ ] Schema changes are a new migration, not an edit to an applied one
- [ ] CHANGELOG.md `[Unreleased]` updated
- [ ] docs/DECISIONS.md updated if a design choice changed
