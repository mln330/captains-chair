"""Single-file plugin runtime entry point.

The plugin starts this executable without a subcommand for the long-running
sidecar.  Agent lifecycle commands use the same executable with a normal
``make-it-so`` CLI subcommand, so the packaged plugin does not depend on a
separate Python or CLI installation.
"""

from __future__ import annotations

import sys


def _uses_cli(argv: list[str]) -> bool:
    """Return whether argv contains a Make It So CLI subcommand."""
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in {"--config", "--once"}:
            index += 2
            continue
        if value in {"--background", "--force-replan", "-h", "--help"}:
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return True
    return False


def main() -> int:
    if _uses_cli(sys.argv[1:]):
        from make_it_so.cli import main as cli_main

        return cli_main()

    from make_it_so.sidecar import main as sidecar_main

    return sidecar_main()


if __name__ == "__main__":
    raise SystemExit(main())
