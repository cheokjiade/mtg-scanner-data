#!/usr/bin/env python3
"""
Update art hash database with new cards from Scryfall.
Reads existing art_hashes.bin, finds new cards, computes pHash, merges.
"""

import struct
import uuid
import os
import time
import json
from pathlib import Path

import requests
from PIL import Image
from io import BytesIO
import hashlib


def compute_phash(image):
    """64-bit perceptual hash (same algorithm as Swift ArtHasher)."""
    # Resize to 32x32 grayscale
    img = image.convert("L").resize((32, 32), Image.LANCZOS)
    pixels = list(img.getdata())

    # 2D DCT
    import math
    size = 32
    def dct1d(row):
        n = len(row)
        out = []
        scale = math.pi / n
        for k in range(n):
            s = sum(row[i] * math.cos(scale * k * (i + 0.5)) for i in range(n))
            out.append(s)
        return out

    # Row DCT
    row_result = []
    for y in range(size):
        row = [float(pixels[y * size + x]) for x in range(size)]
        row_result.extend(dct1d(row))

    # Column DCT
    result = [0.0] * (size * size)
    for x in range(size):
        col = [row_result[y * size + x] for y in range(size)]
        dct_col = dct1d(col)
        for y in range(size):
            result[y * size + x] = dct_col[y]

    # Extract 8x8 low freq
    hash_size = 8
    low_freq = []
    for y in range(hash_size):
        for x in range(hash_size):
            low_freq.append(result[y * size + x])

    # Median threshold (excluding DC)
    without_dc = sorted(low_freq[1:])
    median = without_dc[len(without_dc) // 2]

    # Build 64-bit hash
    h = 0
    for i, val in enumerate(low_freq):
        if val > median:
            h |= (1 << (63 - i))

    # Convert to signed int64 (matching Swift Int64)
    if h >= (1 << 63):
        h -= (1 << 64)
    return h


def read_existing_hashes(path):
    """Read existing art_hashes.bin, return dict of uuid -> hash."""
    hashes = {}
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return hashes

    data = open(path, "rb").read()
    entry_size = 24
    count = len(data) // entry_size

    for i in range(count):
        offset = i * entry_size
        uid = uuid.UUID(bytes=data[offset:offset + 16])
        h = struct.unpack("<q", data[offset + 16:offset + 24])[0]
        hashes[str(uid)] = h

    print(f"Loaded {len(hashes)} existing hashes")
    return hashes


def fetch_all_cards():
    """Fetch all English cards from Scryfall bulk data."""
    print("Fetching Scryfall bulk data info...")
    resp = requests.get("https://api.scryfall.com/bulk-data",
                       headers={"User-Agent": "MTGScannerData/1.0"})
    bulk = resp.json()

    url = None
    for item in bulk["data"]:
        if item["type"] == "default_cards":
            url = item["download_uri"]
            break

    print(f"Downloading bulk data...")
    cards = requests.get(url, headers={"User-Agent": "MTGScannerData/1.0"}).json()
    print(f"  {len(cards)} total cards")
    return cards


def main():
    hash_file = "art_hashes.bin"
    existing = read_existing_hashes(hash_file)
    cards = fetch_all_cards()

    # Filter to English cards with art_crop URLs
    new_cards = []
    for card in cards:
        layout = card.get("layout", "normal")
        if layout in ("token", "double_faced_token", "emblem", "art_series"):
            continue
        lang = card.get("lang", "en")
        if lang != "en":
            continue

        sid = card.get("id", "")
        if sid in existing:
            continue

        art_url = None
        if "image_uris" in card:
            art_url = card["image_uris"].get("art_crop")
        elif "card_faces" in card and card["card_faces"]:
            face = card["card_faces"][0]
            if "image_uris" in face:
                art_url = face["image_uris"].get("art_crop")

        if art_url:
            new_cards.append((sid, art_url))

    print(f"New cards to hash: {len(new_cards)}")

    if not new_cards:
        print("No new cards. Hash file unchanged.")
        return

    # Compute hashes for new cards
    new_hashes = {}
    failed = 0

    for i, (sid, url) in enumerate(new_cards):
        try:
            resp = requests.get(url, timeout=10,
                              headers={"User-Agent": "MTGScannerData/1.0"})
            if resp.status_code != 200:
                failed += 1
                continue
            img = Image.open(BytesIO(resp.content))
            h = compute_phash(img)
            new_hashes[sid] = h
        except Exception:
            failed += 1
            continue

        time.sleep(0.05)  # Rate limit

        if (i + 1) % 500 == 0:
            print(f"  Progress: {i + 1}/{len(new_cards)} ({failed} failed)")

    print(f"Computed {len(new_hashes)} new hashes ({failed} failed)")

    # Merge into existing
    existing.update(new_hashes)

    # Write full file
    with open(hash_file, "wb") as f:
        for sid, h in sorted(existing.items()):
            uid = uuid.UUID(sid)
            f.write(uid.bytes)
            f.write(struct.pack("<q", h))

    size_kb = os.path.getsize(hash_file) // 1024
    print(f"Wrote {len(existing)} hashes to {hash_file} ({size_kb} KB)")


if __name__ == "__main__":
    main()
