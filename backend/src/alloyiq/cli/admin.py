"""alloyiq-admin: the only administrative surface in the MVP (build prompt section 17).

Run on the host by the owner; nothing privileged is reachable over HTTP.
Phase 0 provides the entry point and the command list; each command is
implemented in the phase that owns its data.
"""

from __future__ import annotations

import argparse
import sys

from alloyiq import __version__

# command -> phase that implements it
PLANNED: dict[str, str] = {
    "summary": "P8",
    "runs": "P5",
    "audit": "P2",
    "suspend": "P2",
    "reconcile": "P4",
    "grant-credit": "P4",
    "rate-card": "P4",
    "catalog": "P3",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alloyiq-admin", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"alloyiq {__version__}")
    parser.add_argument("command", choices=sorted(PLANNED), help="admin command")
    args, _ = parser.parse_known_args(argv)
    print(
        f"alloyiq-admin {args.command}: not implemented yet (planned for {PLANNED[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
