"""Local tests for scryfall_bulk.py using a fake requests session."""
import gzip
import io
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import scryfall_bulk


class FakeResponse:
    def __init__(self, payload: bytes, chunk_size: int = 7):
        self.payload = payload
        self.chunk = chunk_size
        self.headers = {"content-length": str(len(payload))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self.payload), self.chunk):
            yield self.payload[i:i + self.chunk]


def run(payload, chunk_size=7):
    scryfall_bulk.requests.get = lambda url, **kw: FakeResponse(payload, chunk_size)
    return list(scryfall_bulk.stream_cards("http://x/bulk"))


def gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


JSONL = b'{"id":"a","name":"One"}\n{"id":"b","name":"Two"}\n'
JSONL_NO_TRAILING_NL = b'{"id":"a","name":"One"}\n{"id":"b","name":"Two"}'
OLD_ARRAY = b'[\n  {"id":"a","name":"One"},\n  {"id":"b","name":"Two"}\n]\n'

failures = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)

# 1. Gzipped JSONL (tiny chunks to stress boundaries)
cards = run(gz(JSONL), chunk_size=3)
check("gzip jsonl", [c["id"] for c in cards] == ["a", "b"])

# 2. Old plain array format
cards = run(OLD_ARRAY)
check("old array", [c["id"] for c in cards] == ["a", "b"])

# 3. Gzipped JSONL, final line missing newline
cards = run(gz(JSONL_NO_TRAILING_NL))
check("no trailing newline", [c["id"] for c in cards] == ["a", "b"])

# 4. Plain JSONL, final line missing newline (uncompressed tail path)
cards = run(JSONL_NO_TRAILING_NL)
check("plain, no trailing newline", [c["id"] for c in cards] == ["a", "b"])

# 5. Multi-member gzip
cards = run(gz(JSONL) + gz(JSONL), chunk_size=5)
check("multi-member gzip", [c["id"] for c in cards] == ["a", "b", "a", "b"])

# 6. Truncated gzip raises
try:
    run(gz(JSONL)[:-8])
    check("truncated raises", False)
except RuntimeError as e:
    check("truncated raises", "Truncated" in str(e))

# 7. One-byte payload doesn't crash the sniffer
cards = run(b"x", chunk_size=1)
check("tiny payload", cards == [])

# 8. Gzip magic split across 1-byte chunks
cards = run(gz(JSONL), chunk_size=1)
check("magic split across chunks", [c["id"] for c in cards] == ["a", "b"])

# 9. resolve_download_info old, new, null-old, neither
check("resolve old", scryfall_bulk.resolve_download_info(
    {"download_uri": "http://old", "size": 5}) == ("http://old", 5))
check("resolve new", scryfall_bulk.resolve_download_info(
    {"jsonl_download_uri": "http://new", "compressed_size": 7}) == ("http://new", 7))
check("resolve null old", scryfall_bulk.resolve_download_info(
    {"download_uri": None, "size": None,
     "jsonl_download_uri": "http://new", "compressed_size": 7}) == ("http://new", 7))
try:
    scryfall_bulk.resolve_download_info({"object": "bulk_data"})
    check("resolve neither raises", False)
except RuntimeError as e:
    check("resolve neither raises", "available keys" in str(e))

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("All scryfall_bulk tests passed")
