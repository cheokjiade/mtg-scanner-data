#!/usr/bin/env python3
"""
Generate a delta update file from Scryfall's bulk card data.

Compares the current Scryfall default_cards bulk data against the previous
version (stored as a GitHub Release). Outputs a compressed delta JSON with
only new, updated, and removed cards.

The delta format:
{
    "version": "1.0",
    "scryfall_updated_at": "2026-04-03T...",
    "previous_updated_at": "2026-04-02T...",
    "new": [ {card_object}, ... ],
    "updated": [ {card_object}, ... ],
    "removed": [ "uuid1", "uuid2", ... ]
}

The app downloads this small delta (~1-5MB) instead of the full 500MB file.
"""

import json
import gzip
import os
import sys
import hashlib
from datetime import datetime
from pathlib import Path

import requests

SCRYFALL_BULK_API = "https://api.scryfall.com/bulk-data/default-cards"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "cheokjiade/mtg-scanner-data")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
OUTPUT_DIR = Path("delta-output")
PREVIOUS_HASHES_FILE = "previous-card-hashes.json.gz"


def set_github_env(key: str, value: str):
    """Set a GitHub Actions environment variable."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"  [env] {key}={value}")


def fetch_scryfall_info() -> dict:
    """Get bulk data download info from Scryfall."""
    print("Fetching Scryfall bulk data info...")
    resp = requests.get(SCRYFALL_BULK_API, headers={"User-Agent": "MTGScanner/1.0"})
    resp.raise_for_status()
    info = resp.json()
    print(f"  Updated at: {info['updated_at']}")
    print(f"  Download URI: {info['download_uri'][:80]}...")
    print(f"  Size: {info.get('size', 0) / 1024 / 1024:.0f} MB")
    return info


def get_previous_release() -> dict | None:
    """Find the most recent delta release on GitHub."""
    print("Checking for previous delta release...")
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"User-Agent": "MTGScanner/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(f"{GITHUB_API}/releases?per_page=5", headers=headers)
    if resp.status_code != 200:
        print(f"  No releases found (status {resp.status_code})")
        return None

    for release in resp.json():
        if release["tag_name"].startswith("scryfall-delta-"):
            print(f"  Found: {release['tag_name']}")
            return release

    print("  No previous delta release found")
    return None


def download_previous_hashes(release: dict) -> dict:
    """Download the card hash manifest from a previous release."""
    for asset in release.get("assets", []):
        if asset["name"] == "manifest.json":
            print(f"  Downloading manifest ({asset['size']} bytes)...")
            resp = requests.get(asset["browser_download_url"],
                                headers={"User-Agent": "MTGScanner/1.0"})
            resp.raise_for_status()
            return resp.json()
    return {}


def card_hash(card: dict) -> str:
    """Generate a hash of the card's key fields to detect changes.

    IMPORTANT: Prices are EXCLUDED from the hash because they change daily
    for most cards, which would make every delta contain ~100k cards (~50MB+).
    Prices are published separately as a lightweight prices.json.gz file.
    """
    key_fields = {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "type_line": card.get("type_line"),
        "oracle_text": card.get("oracle_text"),
        "set": card.get("set"),
        "collector_number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "layout": card.get("layout"),
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty"),
        "image_uris": card.get("image_uris"),
        "legalities": card.get("legalities"),
        # "prices" — EXCLUDED: changes daily, would bloat every delta
        "reserved": card.get("reserved"),
        "released_at": card.get("released_at"),
        "cmc": card.get("cmc"),
        "color_identity": card.get("color_identity"),
        "finishes": card.get("finishes"),
    }
    raw = json.dumps(key_fields, sort_keys=True).encode()
    return hashlib.md5(raw).hexdigest()


def stream_download_cards(url: str):
    """Stream-download and parse the Scryfall bulk JSON line by line."""
    print(f"Downloading bulk data...")
    resp = requests.get(url, stream=True, headers={"User-Agent": "MTGScanner/1.0"})
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    buffer = b""
    card_count = 0

    for chunk in resp.iter_content(chunk_size=256 * 1024):
        buffer += chunk
        downloaded += len(chunk)

        # Process complete lines
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()

            # Skip array brackets
            if line in (b"[", b"]", b""):
                continue

            # Remove trailing comma
            if line.endswith(b","):
                line = line[:-1]

            if line.startswith(b"{"):
                try:
                    card = json.loads(line)
                    card_count += 1
                    if card_count % 10000 == 0:
                        pct = (downloaded * 100 // total) if total else 0
                        print(f"  Parsed {card_count} cards ({pct}%)...")
                    yield card
                except json.JSONDecodeError:
                    continue

    print(f"  Total: {card_count} cards")


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 1. Get Scryfall info
    info = fetch_scryfall_info()
    scryfall_updated_at = info["updated_at"]

    # 2. Get previous release
    prev_release = get_previous_release()
    prev_hashes = {}
    prev_updated_at = None

    if prev_release:
        manifest = download_previous_hashes(prev_release)
        prev_hashes = manifest.get("hashes", {})
        prev_updated_at = manifest.get("scryfall_updated_at")
        print(f"  Previous: {len(prev_hashes)} cards from {prev_updated_at}")

        # Check if Scryfall data has changed
        if prev_updated_at == scryfall_updated_at:
            print("Scryfall data hasn't changed — no delta needed.")
            set_github_env("HAS_CHANGES", "false")
            return

    # 3. Download and process current data — single pass collects:
    #    - Card hashes (for delta comparison)
    #    - New/updated cards
    #    - Compact price map (published separately)
    current_hashes = {}
    new_cards = []
    updated_cards = []
    price_map = {}  # card_id → {usd: "0.34", usd_foil: "1.20", ...}

    for card in stream_download_cards(info["download_uri"]):
        card_id = card.get("id")
        if not card_id:
            continue

        h = card_hash(card)
        current_hashes[card_id] = h

        if card_id not in prev_hashes:
            new_cards.append(card)
        elif prev_hashes[card_id] != h:
            updated_cards.append(card)

        # Extract prices (compact: only non-null values)
        prices = card.get("prices", {})
        compact_prices = {}
        for key in ("usd", "usd_foil", "eur", "eur_foil"):
            val = prices.get(key)
            if val is not None:
                compact_prices[key] = val
        if compact_prices:
            price_map[card_id] = compact_prices

    # 4. Find removed cards
    current_ids = set(current_hashes.keys())
    prev_ids = set(prev_hashes.keys())
    removed_ids = list(prev_ids - current_ids)

    print(f"\nDelta summary:")
    print(f"  New: {len(new_cards)}")
    print(f"  Updated: {len(updated_cards)}")
    print(f"  Removed: {len(removed_ids)}")
    print(f"  Cards with prices: {len(price_map)}")

    # 4c. Fetch latest set data from Scryfall (always include — it's small)
    print("\nFetching set data...")
    sets_data = []
    try:
        sets_resp = requests.get("https://api.scryfall.com/sets",
                                  headers={"User-Agent": "MTGScanner/1.0"})
        sets_resp.raise_for_status()
        sets_json = sets_resp.json()
        sets_data = sets_json.get("data", [])
        # Slim down set objects to just what the app needs
        sets_data = [
            {
                "code": s.get("code"),
                "name": s.get("name"),
                "released_at": s.get("released_at"),
                "set_type": s.get("set_type"),
                "icon_svg_uri": s.get("icon_svg_uri"),
                "card_count": s.get("card_count"),
                "parent_set_code": s.get("parent_set_code"),
            }
            for s in sets_data
        ]
        print(f"  {len(sets_data)} sets fetched")
    except Exception as e:
        print(f"  Warning: Failed to fetch sets: {e}")

    # 4c. Fetch price summary for popular cards (top movers)
    # Prices are embedded in card objects already, so no separate fetch needed.
    # The delta cards include prices in their JSON.

    has_card_changes = bool(new_cards or updated_cards or removed_ids)
    has_set_changes = bool(sets_data)
    has_prices = bool(price_map)

    # Always publish if we have prices (they change daily)
    if not has_card_changes and not has_set_changes and not has_prices:
        print("No changes detected.")
        set_github_env("HAS_CHANGES", "false")
        return

    # 5. Write delta JSON (gzipped)
    delta = {
        "version": "2.0",
        "scryfall_updated_at": scryfall_updated_at,
        "previous_updated_at": prev_updated_at,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "new": new_cards,
        "updated": updated_cards,
        "removed": removed_ids,
        "sets": sets_data,  # Always include latest set metadata
    }

    delta_path = OUTPUT_DIR / "delta.json.gz"
    with gzip.open(delta_path, "wt", encoding="utf-8") as f:
        json.dump(delta, f)

    delta_size = delta_path.stat().st_size
    print(f"  Delta file: {delta_size / 1024:.0f} KB")

    # 5b. Write sets as standalone file
    if sets_data:
        sets_path = OUTPUT_DIR / "sets.json"
        with open(sets_path, "w") as f:
            json.dump({"sets": sets_data, "updated_at": scryfall_updated_at}, f)
        print(f"  Sets file: {sets_path.stat().st_size / 1024:.0f} KB")

    # 5c. Write compact price map (gzipped)
    # Published EVERY run (even if no card changes) because prices change daily.
    # Format: { "card_uuid": {"usd": "0.34", "usd_foil": "1.20"}, ... }
    # ~3-5MB gzipped for ~100k cards with prices.
    prices_data = {
        "updated_at": scryfall_updated_at,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "card_count": len(price_map),
        "prices": price_map,
    }

    # Current prices (published with every delta release)
    prices_path = OUTPUT_DIR / "prices.json.gz"
    with gzip.open(prices_path, "wt", encoding="utf-8") as f:
        json.dump(prices_data, f, separators=(",", ":"))
    prices_size = prices_path.stat().st_size
    print(f"  Prices file: {prices_size / 1024:.0f} KB ({len(price_map)} cards)")

    # Daily price archive (one per day, kept forever for historical trends)
    # The app can download past price snapshots to build price history charts
    # for cards the user doesn't own (without needing per-card API calls).
    archive_date = datetime.utcnow().strftime("%Y-%m-%d")
    archive_path = OUTPUT_DIR / f"prices-{archive_date}.json.gz"
    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        json.dump(prices_data, f, separators=(",", ":"))
    print(f"  Price archive: prices-{archive_date}.json.gz")

    # 6. Write manifest (card hashes for next delta comparison)
    manifest = {
        "scryfall_updated_at": scryfall_updated_at,
        "card_count": len(current_hashes),
        "hashes": current_hashes,
    }

    manifest_path = OUTPUT_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)

    manifest_size = manifest_path.stat().st_size
    print(f"  Manifest file: {manifest_size / 1024 / 1024:.1f} MB")

    # 7. Set GitHub env vars
    # Use datetime with hour for unique tags when running every 2 hours
    today = datetime.utcnow().strftime("%Y-%m-%d-%H%M")
    set_github_env("HAS_CHANGES", "true")
    set_github_env("DELTA_DATE", today)
    set_github_env("NEW_COUNT", str(len(new_cards)))
    set_github_env("UPDATED_COUNT", str(len(updated_cards)))
    set_github_env("REMOVED_COUNT", str(len(removed_ids)))
    set_github_env("DELTA_SIZE", f"{delta_size / 1024:.0f} KB")
    set_github_env("SCRYFALL_UPDATED_AT", scryfall_updated_at)

    print("\nDelta generation complete!")


if __name__ == "__main__":
    main()
