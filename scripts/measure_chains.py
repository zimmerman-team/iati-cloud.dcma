"""Time live traversals of a set of seeds and print a Markdown table (plan step U7).

Usage:
    uv run python scripts/measure_chains.py [--runs 3] [--solr-url URL] [<seed> ...]

Each seed is traversed in both modes, `--runs` times, uncached (the Redis cache is not involved). The table reports
the median and the slowest run, the node and edge counts, the Solr queries per request and why a result was cut off.
"""

import argparse
import statistics
import sys
import time

from app.iati_cloud_dcma.source import SolrLinkSource, SourceError, create_solr_client
from app.iati_cloud_dcma.traversal import Limits, SeedNotFound, build_chain

REFERENCE_SOLR_URL = "https://fcdo.iati.cloud/search/activity/"
DEFAULT_SEEDS = [
    "GB-GOV-1-300978",  # FCDO programme (the worked example)
    "GB-GOV-1-300978-408",  # FCDO project
    "GB-CHC-1047501-23SOELNF",  # NGO activity funded by it
    "NO-BRC-977538319-SOFM2343",  # NGO activity with free-text and name-only receivers
    "IE-CHY-20009601-FCDO24SOM",  # second-tier NGO activity
    "XM-OCHA-CBPF-AFG23-2022",  # OCHA country-based pooled fund
    "XM-OCHA-CERF",  # unpublished identifier cited by more than 10,000 activities
    "XM-DAC-903-SII-45517",  # one identifier, 357 documents
    "XI-IATI-WBTF-TF071624",  # World Bank trust fund
    "XM-DAC-41108-1100001621",  # multilateral (IFAD)
    "NL-KVK-41160054-201133",  # Dutch NGO in a joint response
    "ZA-ROC-1999/05072/08-GPAF-IMP-001",  # duplicated identifier whose copies differ
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("seeds", nargs="*", default=DEFAULT_SEEDS)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--solr-url", default=REFERENCE_SOLR_URL)
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--max-nodes", type=int, default=1000)
    args = parser.parse_args()

    client = create_solr_client(args.solr_url, timeout=30)
    limits = Limits(max_depth=args.max_depth, max_nodes=args.max_nodes)
    print("| Seed | Mode | Nodes | Edges | Queries | Median (s) | Slowest (s) | Cut off by |")
    print("|---|---|---|---|---|---|---|---|")
    for seed in args.seeds:
        for mode in ("constrained", "full"):
            print(_row(client, seed, mode, limits, args.runs), flush=True)
    return 0


def _row(client, seed: str, mode: str, limits: Limits, runs: int) -> str:
    seconds = []
    try:
        for _ in range(runs):
            source = SolrLinkSource(client=client, batch_size=200)
            started = time.perf_counter()
            chain = build_chain(source, seed, mode, limits)
            seconds.append(time.perf_counter() - started)
    except SeedNotFound:
        return f"| `{seed}` | {mode} | not found | | | | | |"
    except SourceError as exc:
        return f"| `{seed}` | {mode} | error: {exc} | | | | | |"
    reasons = ", ".join(chain.truncation_reasons) or "—"
    return (
        f"| `{seed}` | {mode} | {len(chain.nodes)} | {len(chain.edges)} | {source.query_count} | "
        f"{statistics.median(seconds):.2f} | {max(seconds):.2f} | {reasons} |"
    )


if __name__ == "__main__":
    sys.exit(main())
