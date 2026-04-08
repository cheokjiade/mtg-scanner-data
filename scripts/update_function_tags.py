#!/usr/bin/env python3
"""
Fetch Scryfall community function tags and write a binary file.

Binary format (MTGF):
  Header: "MTGF" (4 bytes) + version (uint32) + tag_count (uint32) + entry_count (uint32)
  Tag table: tag_count entries of (bit_index: uint8, name: 32-byte null-padded ASCII)
  Entries: entry_count entries of (UUID: 16 bytes, bitmask: uint32)

The tag table is self-describing so the app knows which bit maps to which tag,
and new tags can be added without app updates.

Requires: pip install requests
"""

import struct
import os
import sys
import time
import json
import uuid as uuid_mod

import requests

HEADERS = {
    "User-Agent": "MTGScannerData/1.0 (function-tags)",
    "Accept": "application/json",
}

OUTPUT_FILE = "function_tags.bin"
MAGIC = b"MTGF"
VERSION = 1

# Tag definitions: (bit_index, tag_name, scryfall_queries)
# Multiple queries are unioned for tags that combine several Scryfall tagger categories.
TAGS = [
    (0,  "draw",             ["function:draw"]),
    (1,  "removal",          ["function:removal"]),
    (2,  "ramp",             ["function:ramp"]),
    (3,  "board_wipe",       ["function:board-wipe"]),
    (4,  "tutor",            ["function:tutor"]),
    (5,  "recursion",        ["function:recursion"]),
    (6,  "counterspell",     ["function:counterspell"]),
    (7,  "sacrifice_outlet", ["function:sacrifice-outlet"]),
    (8,  "lifegain",         ["function:lifegain"]),
    (9,  "flicker",          ["function:flicker"]),
    (10, "graveyard_hate",   ["function:graveyard-hate"]),
    (11, "protection",       [
        "function:protects-creature",
        "function:protects-artifact",
        "function:protects-enchantment",
        "function:protects-planeswalker",
        "function:protects-land",
        "function:protects-permanent",
        "function:gives-player-protection",
        "function:fog",
    ]),
    # 12-31 reserved for future tags
]


def fetch_all_ids_for_tag(query):
    """Fetch all card IDs matching a Scryfall search query, handling pagination."""
    ids = set()
    url = f"https://api.scryfall.com/cards/search?q={query}&order=name&page=1"

    while url:
        resp = requests.get(url, headers=HEADERS)
        if resp.status_code == 404:
            return ids
        if resp.status_code in (429, 503):
            print(f"    Rate limited ({resp.status_code}), waiting 30s...")
            time.sleep(30)
            continue
        if resp.status_code != 200:
            print(f"    Error {resp.status_code} for {query}")
            return ids

        data = resp.json()
        for card in data.get("data", []):
            card_id = card.get("id")
            if card_id:
                ids.add(card_id)

        if data.get("has_more") and data.get("next_page"):
            url = data["next_page"]
        else:
            url = None

        # Scryfall rate limit: 50-100ms between requests (use 150ms for safety)
        time.sleep(0.15)

    return ids


def save_binary(path, tags, card_bitmasks):
    """Write the MTGF binary file."""
    with open(path, "wb") as f:
        # Header
        f.write(MAGIC)
        f.write(struct.pack("<I", VERSION))
        f.write(struct.pack("<I", len(tags)))
        f.write(struct.pack("<I", len(card_bitmasks)))

        # Tag table
        for bit_index, tag_name, _ in tags:
            f.write(struct.pack("<B", bit_index))
            # 32-byte null-padded tag name
            name_bytes = tag_name.encode("ascii")[:31]
            f.write(name_bytes + b"\x00" * (32 - len(name_bytes)))

        # Entries: UUID (16 bytes) + bitmask (4 bytes)
        for card_id_str, bitmask in card_bitmasks.items():
            uuid_bytes = uuid_mod.UUID(card_id_str).bytes
            f.write(uuid_bytes)
            f.write(struct.pack("<I", bitmask))


def main():
    print("=" * 50)
    print("  Scryfall Function Tags Updater")
    print("=" * 50)

    # Collect card IDs per tag
    card_bitmasks = {}  # card_id_str -> bitmask

    for bit_index, tag_name, queries in TAGS:
        print(f"\nFetching {tag_name}...")
        ids = set()
        for query in queries:
            query_ids = fetch_all_ids_for_tag(query)
            print(f"  {query}: {len(query_ids)} cards")
            ids |= query_ids
        if len(queries) > 1:
            print(f"  Combined: {len(ids)} unique cards")

        for card_id in ids:
            if card_id not in card_bitmasks:
                card_bitmasks[card_id] = 0
            card_bitmasks[card_id] |= (1 << bit_index)

    # Stats
    print(f"\n{'=' * 50}")
    print(f"Total unique cards with tags: {len(card_bitmasks)}")
    for bit_index, tag_name, _ in TAGS:
        count = sum(1 for m in card_bitmasks.values() if m & (1 << bit_index))
        print(f"  {tag_name}: {count}")

    # Multi-tagged cards
    multi = sum(1 for m in card_bitmasks.values() if bin(m).count("1") > 1)
    print(f"  Cards with 2+ tags: {multi}")

    # Save
    save_binary(OUTPUT_FILE, TAGS, card_bitmasks)
    size = os.path.getsize(OUTPUT_FILE)
    print(f"\nSaved {OUTPUT_FILE}: {size:,} bytes ({size/1024:.0f} KB)")
    print(f"  {len(TAGS)} tags, {len(card_bitmasks)} entries")


if __name__ == "__main__":
    main()
