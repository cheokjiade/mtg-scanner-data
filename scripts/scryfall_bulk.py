"""Shared helpers for fetching Scryfall bulk card data.

Mid-2026 Scryfall replaced `download_uri` (a plain pretty-printed JSON
array) with `jsonl_download_uri` (gzipped JSONL) and `size` with
`compressed_size` in the bulk-data API. These helpers normalize both
formats and stream-parse either file type so every script in this repo
shares one battle-tested code path.
"""

import json
import zlib

import requests

USER_AGENT = "MTGScannerData/1.0"

GZIP_MAGIC = b"\x1f\x8b"


def resolve_download_info(info: dict) -> tuple[str, int]:
    """Return (download_uri, size_bytes) from a bulk_data object, old or new format.

    Raises RuntimeError with the available keys if neither URI field is
    present, so the next Scryfall API change is diagnosable at a glance.
    """
    uri = info.get("download_uri") or info.get("jsonl_download_uri")
    if not uri:
        raise RuntimeError(
            f"No download URI in bulk-data response; available keys: {sorted(info)}"
        )
    size = info.get("size") or info.get("compressed_size") or 0
    return uri, size


def _parse_card_line(line: bytes):
    """Parse one line of bulk data. Returns a card dict or None.

    Tolerates both formats: JSONL (bare object per line) and the old
    pretty-printed array (objects with trailing commas, bracket lines).
    """
    line = line.strip()
    if not line.startswith(b"{"):
        return None
    if line.endswith(b","):
        line = line[:-1]
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def stream_cards(url: str, user_agent: str = USER_AGENT):
    """Stream-download and parse Scryfall bulk data, yielding card dicts.

    Handles both plain JSON-array files and gzipped JSONL, detected by
    the gzip magic bytes of the payload (not the URL, which may gain
    query strings or change suffix). Decompression is streaming, so the
    ~500MB decompressed payload never sits in memory, and multi-member
    gzip files are decoded in full.

    Raises RuntimeError if a gzip stream ends before its terminator —
    a truncated download must fail loudly rather than yield a silently
    short card list.
    """
    resp = requests.get(url, stream=True, headers={"User-Agent": user_agent})
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    sniff = b""
    sniffed = False
    decompressor = None
    buffer = b""
    card_count = 0

    for chunk in resp.iter_content(chunk_size=256 * 1024):
        downloaded += len(chunk)

        # Decide gzip vs plain from the first two payload bytes.
        if not sniffed:
            sniff += chunk
            if len(sniff) < 2:
                continue
            sniffed = True
            chunk, sniff = sniff, b""
            if chunk[:2] == GZIP_MAGIC:
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)

        if decompressor:
            out = decompressor.decompress(chunk)
            # A gzip file may contain several concatenated members
            # (e.g. pigz/bgzf output); decode them all.
            while decompressor.eof and decompressor.unused_data:
                leftover = decompressor.unused_data
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                out += decompressor.decompress(leftover)
            chunk = out

        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            card = _parse_card_line(line)
            if card is not None:
                card_count += 1
                if card_count % 10000 == 0:
                    pct = (downloaded * 100 // total) if total else 0
                    print(f"  Parsed {card_count} cards ({pct}%)...")
                yield card

    if not sniffed and sniff:
        # Payload smaller than the sniff threshold — treat as plain.
        buffer += sniff

    if decompressor:
        if not decompressor.eof:
            raise RuntimeError(
                "Truncated gzip stream: download ended before the gzip "
                "terminator. Refusing to return a partial card list."
            )
        buffer += decompressor.flush()

    # Parse any remaining lines, including a final one with no newline.
    for line in buffer.split(b"\n"):
        card = _parse_card_line(line)
        if card is not None:
            card_count += 1
            yield card

    print(f"  Total: {card_count} cards")
