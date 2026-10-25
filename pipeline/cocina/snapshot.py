"""Write every published card to a versioned JSONL snapshot, optionally as a GitHub release.

    python -m cocina.snapshot [--release]

Consumers pin a snapshot tag (cartoTaco's evals do), so knowledge changes
never make their tests flaky. One card per line, keys sorted, so diffs read well.
"""

import argparse
import json
import subprocess
from datetime import date
from pathlib import Path

from cocina.db import client as db


def snapshot(release: bool) -> Path:
    # ponytail: PostgREST caps a response at max_rows (1000); page through cards() past that
    cards = db().rpc("cards").execute().data
    version = db().rpc("kb_version").execute().data
    tag = f"kb-{date.today():%Y.%m.%d}"
    path = Path(__file__).resolve().parents[2] / "snapshots" / f"{tag}.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cards))
    claims = sum(len(c["claims"]) for c in cards)
    print(f"{path}: {len(cards)} entities, {claims} claims, kb_version {version}")
    if release:
        subprocess.run(["gh", "release", "create", tag, str(path), "--title", tag,
                        "--notes", f"{len(cards)} entities, {claims} claims. kb_version {version}."], check=True)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", action="store_true", help="publish as a GitHub release (needs gh)")
    snapshot(ap.parse_args().release)
