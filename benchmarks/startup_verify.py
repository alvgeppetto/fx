"""Recompute an fx startup report from retained neutral evidence."""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence

from benchmarks.startup_contract import StartupContractError, verify_bundle_report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    arguments = parser.parse_args(argv)
    try:
        report = verify_bundle_report(arguments.bundle.expanduser().resolve())
    except (StartupContractError, OSError, ValueError) as error:
        print(f"startup evidence verification failed: {error}", file=sys.stderr)
        return 3
    print(f"recomputed: {report['status']} ({report['plan_digest']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
