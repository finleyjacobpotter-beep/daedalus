from __future__ import annotations

import json
import sys

from hostreport import collect


def main() -> int:
    json.dump(collect(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
