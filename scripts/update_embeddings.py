#!/usr/bin/env python3
"""
Update embedding database with new cards from Scryfall.
Reads existing embeddings.bin, finds new cards, computes embeddings via ONNX, merges.

Requires: pip install onnxruntime Pillow requests numpy
"""

import struct
import os
import time
import json
from pathlib import Path

import requests
import numpy as np
from PIL import Image
from io import BytesIO

SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"
EMBEDDINGS_FILE = "embeddings.bin"
ONNX_MODEL_URL_ENV = "ONNX_MODEL_URL"  # Set via env var or download from release
ONNX_MODEL_FILE = "card_embedder.onnx"
EMBEDDING_DIM = 128
MAGIC = b"MTGE"
VERSION = 1

HEADERS = {
    "User-Agent": "MTGScannerData/1.0 (embedding-updater)",
    "Accept": "application/json",
}


def load_existing_embeddings(path):
    """Load existing embeddings.bin, return dict of uuid_hex -> vector."""
    entries = {}
    if not os.path.exists(path) or os.path.getsize(path) < 16:
        return entries

    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != MAGIC:
            print(f"Warning: invalid magic in {path}, starting fresh")
            return entries
        version = struct.unpack("<I", f.read(4))[0]
        dim = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<I", f.read(4))[0]

        for _ in range(count):
            uuid_bytes = f.read(16)
            vec_bytes = f.read(dim * 4)
            if len(uuid_bytes) < 16 or len(vec_bytes) < dim * 4:
                break
            uuid_hex = uuid_bytes.hex()
            vec = np.frombuffer(vec_bytes, dtype=np.float32).copy()
            entries[uuid_hex] = vec

    print(f"Loaded {len(entries)} existing embeddings")
    return entries


def save_embeddings(path, entries, dim=EMBEDDING_DIM):
    """Save embeddings.bin in MTGE format."""
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", VERSION))
        f.write(struct.pack("<I", dim))
        f.write(struct.pack("<I", len(entries)))
        for uuid_hex, vec in entries.items():
            f.write(bytes.fromhex(uuid_hex))
            f.write(vec.astype(np.float32).tobytes())
    print(f"Saved {len(entries)} embeddings to {path}")


def uuid_str_to_bytes(uuid_str):
    """Convert UUID string (with dashes) to 16 bytes."""
    import uuid as uuid_mod
    u = uuid_mod.UUID(uuid_str)
    return u.bytes


def get_all_card_ids():
    """Get all card IDs with art_crop URLs from Scryfall bulk data."""
    print("Fetching Scryfall bulk data catalog...")
    resp = requests.get(SCRYFALL_BULK_URL, headers=HEADERS)
    bulk = resp.json()

    # Find the "default_cards" bulk file
    default_cards = None
    for item in bulk.get("data", []):
        if item.get("type") == "default_cards":
            default_cards = item
            break

    if not default_cards:
        print("Error: could not find default_cards bulk data")
        return []

    download_url = default_cards["download_uri"]
    print(f"Downloading bulk data ({default_cards.get('size', '?')} bytes)...")
    resp = requests.get(download_url, headers=HEADERS, stream=True)

    cards = []
    import ijson
    # Use streaming JSON parser to handle large file
    try:
        import ijson
        parser = ijson.items(resp.raw, "item")
        for card in parser:
            art_crop = None
            if "image_uris" in card and "art_crop" in card["image_uris"]:
                art_crop = card["image_uris"]["art_crop"]
            elif "card_faces" in card and card["card_faces"]:
                face = card["card_faces"][0]
                if "image_uris" in face and "art_crop" in face["image_uris"]:
                    art_crop = face["image_uris"]["art_crop"]

            if art_crop and card.get("id"):
                cards.append({
                    "id": card["id"],
                    "name": card.get("name", ""),
                    "art_crop": art_crop,
                })
    except ImportError:
        # Fallback: load entire JSON into memory
        print("ijson not available, loading full JSON into memory...")
        data = resp.json()
        for card in data:
            art_crop = None
            if "image_uris" in card and "art_crop" in card["image_uris"]:
                art_crop = card["image_uris"]["art_crop"]
            elif "card_faces" in card and card["card_faces"]:
                face = card["card_faces"][0]
                if "image_uris" in face and "art_crop" in face["image_uris"]:
                    art_crop = face["image_uris"]["art_crop"]

            if art_crop and card.get("id"):
                cards.append({
                    "id": card["id"],
                    "name": card.get("name", ""),
                    "art_crop": art_crop,
                })

    print(f"Found {len(cards)} cards with art crops")
    return cards


def compute_embedding(session, image):
    """Compute 128-d embedding from PIL Image using ONNX Runtime."""
    input_name = session.get_inputs()[0].name

    # Preprocess: resize to 224x224, normalize with ImageNet stats
    img = image.resize((224, 224), Image.LANCZOS).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    arr = arr.transpose(2, 0, 1)  # HWC -> CHW
    arr = np.expand_dims(arr, 0)  # Add batch dim

    result = session.run(None, {input_name: arr})
    vec = result[0][0]

    # L2 normalize
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm

    return vec.astype(np.float32)


def main():
    import onnxruntime as ort

    # Load existing embeddings
    entries = load_existing_embeddings(EMBEDDINGS_FILE)
    existing_ids = set(entries.keys())

    # Load ONNX model
    if not os.path.exists(ONNX_MODEL_FILE):
        model_url = os.environ.get(ONNX_MODEL_URL_ENV, "")
        if model_url:
            print(f"Downloading ONNX model...")
            resp = requests.get(model_url)
            with open(ONNX_MODEL_FILE, "wb") as f:
                f.write(resp.content)
            # Also download .data file if exists
            data_url = model_url + ".data"
            try:
                resp = requests.get(data_url)
                if resp.status_code == 200:
                    with open(ONNX_MODEL_FILE + ".data", "wb") as f:
                        f.write(resp.content)
            except Exception:
                pass
        else:
            print(f"Error: {ONNX_MODEL_FILE} not found and no download URL set")
            return

    session = ort.InferenceSession(ONNX_MODEL_FILE)
    print(f"ONNX model loaded")

    # Get all cards from Scryfall
    cards = get_all_card_ids()

    # Find new cards
    new_cards = []
    for card in cards:
        uuid_hex = uuid_str_to_bytes(card["id"]).hex()
        if uuid_hex not in existing_ids:
            new_cards.append((uuid_hex, card))

    print(f"New cards to process: {len(new_cards)}")

    if not new_cards:
        print("No new cards, saving unchanged file")
        save_embeddings(EMBEDDINGS_FILE, entries)
        return

    # Process new cards
    processed = 0
    failed = 0
    for uuid_hex, card in new_cards:
        try:
            resp = requests.get(card["art_crop"], headers={
                "User-Agent": "MTGScannerData/1.0",
            }, timeout=10)
            if resp.status_code != 200:
                failed += 1
                continue

            img = Image.open(BytesIO(resp.content)).convert("RGB")
            vec = compute_embedding(session, img)
            entries[uuid_hex] = vec
            processed += 1

            if processed % 1000 == 0:
                print(f"  Progress: {processed}/{len(new_cards)} (failed: {failed})")

            # Rate limit
            time.sleep(0.05)

        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f"  Error processing {card['name']}: {e}")

    print(f"Processed {processed} new cards ({failed} failed)")

    # Save merged file
    save_embeddings(EMBEDDINGS_FILE, entries)


if __name__ == "__main__":
    main()
