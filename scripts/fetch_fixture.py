"""Save IATI.cloud Solr activity documents as test fixtures.

Usage:
    uv run python scripts/fetch_fixture.py <iati-identifier> [<iati-identifier> ...]
    uv run python scripts/fetch_fixture.py --solr-url https://example.org/search/activity/ <id>
    uv run python scripts/fetch_fixture.py --copies <iati-identifier>

Each document is fetched with the fields the extractor reads and written, unchanged, to
tests/fixtures/activities/<identifier>.json. An identifier can have several documents in the index: without --copies
that is an error; with it, all of them are written as a JSON list to <identifier>.copies.json. Characters that are unsafe in file names (such as "/") become "_".
The endpoint must accept Solr select parameters directly, like the reference instance does.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from app.iati_cloud_dcma.links import SOLR_FIELDS

REFERENCE_SOLR_URL = "https://fcdo.iati.cloud/search/activity/"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "activities"
TIMEOUT_SECONDS = 30
MAX_COPIES = 500


def fixture_filename(identifier: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier) + ".json"


def fetch_copies(solr_url: str, identifier: str) -> list[dict]:
    """Every document published under `identifier`, in a stable order."""
    escaped = identifier.replace("\\", "\\\\").replace('"', '\\"')
    params = {
        "q": f'iati-identifier:"{escaped}"',
        "fl": ",".join(SOLR_FIELDS),
        "rows": MAX_COPIES,
        "sort": "id asc",
        "wt": "json",
    }
    url = f"{solr_url}?{urllib.parse.urlencode(params)}"
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"not an http(s) URL: {solr_url}")
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # nosec B310 - scheme checked above
        return json.load(response)["response"]["docs"]


def fetch(solr_url: str, identifier: str) -> dict:
    docs = fetch_copies(solr_url, identifier)
    if len(docs) != 1:
        raise LookupError(f"expected exactly one document for {identifier!r}, got {len(docs)} (use --copies)")
    return docs[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("identifiers", nargs="+", metavar="iati-identifier")
    parser.add_argument("--solr-url", default=REFERENCE_SOLR_URL)
    parser.add_argument("--copies", action="store_true", help="save every document of each identifier, as a list")
    args = parser.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for identifier in args.identifiers:
        if args.copies:
            content: dict | list[dict] = fetch_copies(args.solr_url, identifier)
            path = FIXTURE_DIR / fixture_filename(identifier).replace(".json", ".copies.json")
        else:
            content = fetch(args.solr_url, identifier)
            path = FIXTURE_DIR / fixture_filename(identifier)
        path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"saved {identifier} -> {path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
