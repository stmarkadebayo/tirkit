"""Fail if a built wheel does not contain the web UI (so `pip install tirkit[server]` shows it).

    python scripts/check_wheel.py dist/tirkit-*.whl
"""

from __future__ import annotations

import sys
import zipfile


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: check_wheel.py WHEEL...", file=sys.stderr)
        return 2
    bad = 0
    for path in paths:
        names = zipfile.ZipFile(path).namelist()
        has_index = "tirkit/server/web/index.html" in names
        assets = [n for n in names if n.startswith("tirkit/server/web/assets/")]
        if has_index and assets:
            print(f"{path}: ok (web UI, {len(assets)} asset files)")
        else:
            print(f"{path}: the web UI is missing (run `make dist`, not a plain build)")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
