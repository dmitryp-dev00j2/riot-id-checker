#!/usr/bin/env python3
"""Check Riot ID availability across regions."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REGIONS = [
    "na1", "euw1", "eun1", "kr", "jp1",
    "br1", "la1", "la2", "oc1", "tr1", "ru",
]

API_BASE = "https://{region}.api.riotgames.com"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_key() -> str:
    key = os.environ.get("RIOT_API_KEY")
    if not key:
        cfg = Path.home() / ".config" / "riot_id_checker" / "api.key"
        if cfg.exists():
            key = cfg.read_text().strip()
    if not key:
        print("set RIOT_API_KEY or put key in ~/.config/riot_id_checker/api.key", file=sys.stderr)
        sys.exit(2)
    return key

def _req(url: str, api_key: str, client: httpx.Client, retries: int = 3) -> dict:
    """Make a Riot API request with 429 backoff."""
    headers = {
        "X-Riot-Token": api_key,
        "User-Agent": "riot_id_checker/0.2 (github.com/unknown)",
    }
    for attempt in range(retries):
        try:
            r = client.get(url, headers=headers)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", "2"))
                if attempt < retries - 1:
                    time.sleep(retry_after)
                    continue
                return {"_error": "rate_limited"}
            r.raise_for_status()
            return r.json() if r.text else {}
        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return {}

def _check_game_name(game_name: str, tag_line: str, api_key: str, client: httpx.Client, delay: float = 0.0, regions=None) -> dict:
    """Check a single Riot ID across regions."""
    results = {}
    check_regions = regions or REGIONS
    for region in check_regions:
        url = (
            f"{API_BASE.format(region=region)}"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        try:
            data = _req(url, api_key, client)
            if "_error" in data:
                results[region] = {"exists": None, "puuid": "", "error": data["_error"]}
            else:
                results[region] = {
                    "exists": bool(data.get("puuid")),
                    "puuid": data.get("puuid", ""),
                }
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                results[region] = {"exists": False, "puuid": ""}
            elif exc.response.status_code == 403:
                results[region] = {"exists": None, "puuid": "", "error": "forbidden (key expired?)"}
            else:
                results[region] = {"exists": None, "puuid": "", "error": f"http_{exc.response.status_code}"}
        except Exception:
            results[region] = {"exists": None, "puuid": "", "error": "network"}
        if delay:
            time.sleep(delay)
    return results

def _fmt_tsv(game_name: str, tag_line: str, results: dict) -> str:
    lines = [f"# checked {game_name}#{tag_line} at {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    for region in REGIONS:
        r = results.get(region, {})
        status = "taken" if r.get("exists") else "free" if r.get("exists") is False else "error"
        lines.append(f"{region}\t{status}\t{r.get('puuid', '')}\t{r.get('error', '')}")
    return "\n".join(lines)

def main() -> int:
    parser = argparse.ArgumentParser(
        usage="python -m riot_id_checker <name> [--tag TAG] [--regions REGIONS]",
        description="Check if a Riot ID (gameName#tagLine) is available.",
    )
    parser.add_argument("name", nargs="?", help="Game name (before the #)")
    parser.add_argument("--tag", default="", help="Tag line (after the #). Default: check all common tags")
    parser.add_argument("--regions", default="", help="Comma-separated region list. Default: all")
    parser.add_argument("--tsv", action="store_true", help="Output TSV (default: human readable)")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds between region requests")
    parser.add_argument("--only-free", action="store_true", help="Only show IDs that are free in at least one region")
    parser.add_argument("--batch", default="", help="Read names from file (one per line, optional #tag)")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds")
    args = parser.parse_args()

    api_key = _load_key()

    region_filter = [r.strip() for r in args.regions.split(",") if r.strip()] if args.regions else None

    # FIXME: httpx timeout needs to be passed per-client, but we create one client below
    client = httpx.Client(timeout=args.timeout)

    # Batch mode
    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.exists():
            print(f"batch file not found: {args.batch}", file=sys.stderr)
            return 2
        names = [line.strip() for line in batch_path.read_text().splitlines() if line.strip()]
        for entry in names:
            if "#" in entry:
                gname, gtag = entry.split("#", 1)
            else:
                gname, gtag = entry, ""
            results = _check_game_name(gname, gtag, api_key, client, delay=args.delay, regions=region_filter)
            if args.tsv:
                print(_fmt_tsv(gname, gtag, results))
            else:
                free_regions = [r for r, v in results.items() if v.get("exists") is False]
                status = "free" if free_regions else "taken"
                print(f"{gname}#{gtag}\t{status}\t{','.join(free_regions) or 'none'}")
        client.close()
        return 0

    if not args.name:
        parser.print_usage()
        return 2

    game_name = args.name
    tag_line = args.tag or ""

    # If no tag given, try a few common ones
    if not tag_line:
        tags_to_try = ["", "NA1", "EUW", "KR1", "JP1", "BR1"]
    else:
        tags_to_try = [tag_line]

    any_free = False
    for tag in tags_to_try:
        results = _check_game_name(game_name, tag, api_key, client, delay=args.delay, regions=region_filter)
        free_regions = [r for r, v in results.items() if v.get("exists") is False]
        if free_regions:
            any_free = True
        if args.only_free and not free_regions:
            continue
        if args.tsv:
            print(_fmt_tsv(game_name, tag, results))
        else:
            print(f"\n{game_name}#{tag}")
            for region in (region_filter or REGIONS):
                r = results.get(region, {})
                status = "taken" if r.get("exists") else "free" if r.get("exists") is False else "error"
                print(f"  {region:6s} {status}")
    client.close()
    if args.only_free and not any_free:
        print("no free tags found")
        return 1
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
