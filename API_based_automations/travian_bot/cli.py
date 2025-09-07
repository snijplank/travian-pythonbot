#!/usr/bin/env python3
import argparse
import sys
import logging

from identity_handling.login import login
from identity_handling.identity_helper import load_villages_from_identity
from core.travian_api import TravianAPI
from core.full_map_scanner import full_map_scan
from analysis.full_scan_oasis_analysis import extract_unoccupied_oases


def cmd_scan(args):
    # Login
    session, server_url = login(interactive=False)
    api = TravianAPI(session, server_url)
    if args.fast:
        api.set_humanizer(False)

    villages = load_villages_from_identity() or []
    if not villages:
        print("❌ No villages found in identity.")
        sys.exit(2)

    if args.village is None:
        # List villages and exit
        print("\n🏡 Available villages to scan from:")
        for idx, v in enumerate(villages):
            print(f"{idx}: {v['village_name']} ({v['x']},{v['y']})")
        print("\nUse --village INDEX to select a village.")
        sys.exit(0)

    try:
        v = villages[int(args.village)]
    except Exception:
        print("❌ Invalid village index.")
        sys.exit(2)

    vx, vy = int(v["x"]), int(v["y"])
    radius = int(args.radius or 25)
    print("\n🔍 Starting map scan...")
    print(f"\n✅ Selected village: {v['village_name']} at ({vx},{vy})")
    print(f"[+] Starting full map scan around ({vx}, {vy}) with radius {radius}...")
    path = full_map_scan(api, vx, vy, radius)
    print(f"\n✅ Scan saved to: {path}")
    if args.extract:
        print("[+] Extracting unoccupied oases from scan data...")
        try:
            extract_unoccupied_oases(path)
        except Exception as e:
            print(f"❌ Extraction failed: {e}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="travian-bot", description="Travian bot CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="Run map scan around a village")
    p_scan.add_argument("--village", type=int, help="Village index (from identity)")
    p_scan.add_argument("--radius", type=int, default=25, help="Scan radius (default 25)")
    p_scan.add_argument("--fast", action="store_true", help="Disable humanizer delays during scan")
    p_scan.add_argument("--extract", action="store_true", help="Extract unoccupied oases after scan")
    p_scan.set_defaults(func=cmd_scan)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    logging.basicConfig(level=logging.INFO)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
