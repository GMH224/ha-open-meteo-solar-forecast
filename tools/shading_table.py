#!/usr/bin/env python3
"""Turn a shading geometry file (.yaml) into a shading table, or check a table.

Usage:
    python tools/shading_table.py examples/shading/south_geometry.yaml > south_table.txt
    python tools/shading_table.py my_table.txt --check

Runs without Home Assistant (needs numpy and PyYAML). The table is exactly
what the integration computes internally from the geometry file, so it can be
inspected, plotted or edited and then used instead of the geometry file.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "omsf_shading", ROOT / "custom_components" / "open_meteo_solar_forecast" / "shading.py"
)
shading = importlib.util.module_from_spec(spec)
sys.modules["omsf_shading"] = shading
spec.loader.exec_module(shading)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("--check", action="store_true", help="only validate and print a summary")
    args = parser.parse_args()
    text = Path(args.file).read_text(encoding="utf-8")
    try:
        table = shading.load_shading_text(text, args.file)
    except shading.ShadingError as err:
        print(f"invalid: {err}", file=sys.stderr)
        return 2
    if args.check:
        print(f"valid {table.source}; digest {table.digest}; {table.summary}")
        return 0
    sys.stdout.write(table.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
