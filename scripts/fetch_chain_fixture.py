"""Capture every Solr document a set of live traversals reads, as one fixture for offline replay.

Usage:
    uv run python scripts/fetch_chain_fixture.py <name> <seed>:<mode> [<seed>:<mode> ...]
    uv run python scripts/fetch_chain_fixture.py worked_example GB-CHC-1047501-23SOELNF:full GB-GOV-1-300978:constrained

Runs build_chain against the live source (default: the reference instance) and records the raw documents of every
fetch and reverse lookup, including every copy of a duplicated identifier. They are written to tests/fixtures/chains/<name>.json. Loaded into FakeLinkSource, they
let the tests replay the same traversals without the network.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from app.iati_cloud_dcma.nodes import normalise_id
from app.iati_cloud_dcma.source import SolrLinkSource, create_solr_client
from app.iati_cloud_dcma.traversal import Limits, build_chain

REFERENCE_SOLR_URL = "https://fcdo.iati.cloud/search/activity/"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "chains"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name")
    parser.add_argument("runs", nargs="+", metavar="seed:mode")
    parser.add_argument("--solr-url", default=REFERENCE_SOLR_URL)
    parser.add_argument("--max-depth", type=int, default=10)
    parser.add_argument("--max-nodes", type=int, default=500)
    args = parser.parse_args()

    client = create_solr_client(args.solr_url, timeout=30)
    captured: dict[tuple[str, str], dict] = {}  # (identifier, document id) → raw document
    search = client.search

    def recording_search(query, **kwargs):
        results = search(query, **kwargs)
        for raw in results.docs:
            identifier = normalise_id(raw.get("iati-identifier"))
            if identifier is not None:
                captured.setdefault((identifier, str(raw.get("id", ""))), raw)
        return results

    client.search = recording_search
    limits = Limits(max_depth=args.max_depth, max_nodes=args.max_nodes)
    runs = []
    for run in args.runs:
        seed, _, mode = run.rpartition(":")
        source = SolrLinkSource(client=client, batch_size=100)
        started = time.perf_counter()
        chain = build_chain(source, seed, mode, limits, orgs="all")
        seconds = time.perf_counter() - started
        runs.append(
            {
                "seed": seed,
                "mode": mode,
                "orgs": "all",
                "nodes": len(chain.nodes),
                "edges": len(chain.edges),
                "truncation_reasons": list(chain.truncation_reasons),
            }
        )
        print(
            f"{seed} {mode}: {len(chain.nodes)} nodes, {source.query_count} queries, {seconds:.1f}s, "
            f"truncation_reasons={list(chain.truncation_reasons)}"
        )

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{args.name}.json"
    payload = {
        "captured": date.today().isoformat(),
        "solr_url": args.solr_url,
        "limits": {"max_depth": args.max_depth, "max_nodes": args.max_nodes},
        "runs": runs,
        "docs": [captured[key] for key in sorted(captured)],
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"saved {len(captured)} documents -> {path.relative_to(Path.cwd())} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
