#!/usr/bin/env python3
"""Git pre-push hook: refuse any push that would update or delete `main` on any remote.

Git passes one line per ref on stdin:

    <local ref> <local sha> <remote ref> <remote sha>

The hook checks every remote ref named there, so it sees the actual destination,
whatever the command looked like: a bare `git push` from a branch that tracks `main`,
`HEAD:main`, `refs/heads/main`, an alias, a script, or `git -C <dir> push`. A push that
lists several refs is refused if any one of them is `main`.

The one override is manual: set CAREERHUNTER_ALLOW_PUSH_MAIN=1 for that push. Claude
Code's deny rules block any command that mentions the variable (ADR-011), so an agent
can't use it.

Installed by `make hooks` (and `make setup`) as .git/hooks/pre-push. Standard library
only, so it runs before any project dependencies are installed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

PROTECTED = "refs/heads/main"
OVERRIDE = "CAREERHUNTER_ALLOW_PUSH_MAIN"


def protected_refs(lines: Iterable[str]) -> list[str]:
    """The protected remote refs this push would update or delete."""
    hits = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and parts[2] == PROTECTED:
            hits.append(parts[2])
    return hits


def main(argv: list[str], stdin: Iterable[str], env: dict[str, str]) -> int:
    remote = argv[1] if len(argv) > 1 else "the remote"
    if not protected_refs(stdin):
        return 0
    if env.get(OVERRIDE) == "1":
        print(f"pre-push: {OVERRIDE}=1 is set, so this push to main on {remote} is allowed.")
        return 0
    print(
        f"pre-push: refusing to push to main on {remote}.\n"
        "  main only changes through pull requests (ADR-010, ADR-012). Push your branch\n"
        "  by name instead, e.g. git push -u origin <branch>.\n"
        f"  To push to main by hand, deliberately: {OVERRIDE}=1 git push ...",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin, dict(os.environ)))
