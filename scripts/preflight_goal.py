"""Check a Claude Code goal's character limit without echoing or saving its contents."""

from __future__ import annotations

import sys

WARNING_LIMIT = 3_500
MAXIMUM_LIMIT = 4_000


def main() -> int:
    length = len(sys.stdin.read())
    print(f"Goal length: {length} characters.")
    if length > MAXIMUM_LIMIT:
        print(
            f"BLOCKED: goal exceeds the {MAXIMUM_LIMIT}-character command limit.",
            file=sys.stderr,
        )
        return 2
    if length > WARNING_LIMIT:
        print(f"WARNING: goal exceeds the recommended {WARNING_LIMIT}-character limit.")
    else:
        print("OK: goal is within the recommended limit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
