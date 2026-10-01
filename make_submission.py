#!/usr/bin/env python3
"""Build submission.jsonl for the 30 canonical test pairs (brief §7.2).

  python make_submission.py [--dataset ../dataset] [--out submission.jsonl] [--now 2026-04-26T10:00:00Z]

Runs the official generator (dataset/generate_dataset.py) into a temp dir so the
pairs are exactly the deterministic set every participant gets.
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from bot import compose

HERE = Path(__file__).resolve().parent


def load(dirpath: Path, key: str) -> dict:
    return {d[key]: d for d in (json.load(open(f)) for f in glob.glob(str(dirpath / "*.json")))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(HERE.parent / "dataset"))
    ap.add_argument("--out", default=str(HERE / "submission.jsonl"))
    ap.add_argument("--now", default="2026-04-26T10:00:00Z")
    a = ap.parse_args()
    ds = Path(a.dataset).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, str(ds / "generate_dataset.py"), "--seed-dir", str(ds), "--out", tmp],
                       check=True, capture_output=True)
        out = Path(tmp)
        cats = load(out / "categories", "slug")
        merchants = load(out / "merchants", "merchant_id")
        customers = load(out / "customers", "customer_id")
        triggers = load(out / "triggers", "id")
        pairs = json.load(open(out / "test_pairs.json"))["pairs"]
    with open(a.out, "w") as f:
        for p in pairs:
            t = triggers[p["trigger_id"]]
            m = merchants[p["merchant_id"]]
            c = customers.get(p["customer_id"]) if p.get("customer_id") else None
            msg = compose(cats[m["category_slug"]], m, t, c, now=a.now)
            row = {"test_id": p["test_id"], "trigger_id": p["trigger_id"], "merchant_id": p["merchant_id"],
                   "customer_id": p.get("customer_id"), **msg}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(pairs)} lines -> {a.out}")


if __name__ == "__main__":
    main()
