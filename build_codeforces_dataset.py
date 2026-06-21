#!/usr/bin/env python3
"""Build a Codeforces eval dataset (+ sidecar test files) from the open-r1/codeforces
benchmark on the Hugging Face Hub.

The CSV is committed; the (potentially large) test data is NOT — both <tests-dir> and
<gen-tests-dir> are git-ignored. So after cloning, run this script once to FETCH the tests
the CSV references (the CSV's selection is deterministic, so it regenerates identically).

Output:
  <out-csv>                       CSV with columns: id, problem (statement), solution
                                  (a small JSON blob referencing the sidecar tests).
  <tests-dir>/<id>.json           fetched sidecar: {tests: official tests, checker}.
  <gen-tests-dir>/<id>.json       (only with --with-generated) the large generated tests,
                                  appended to the official ones for stronger grading.

The CSV stays small (statements + tiny blobs); the (possibly large) tests live in sidecars.
The grading checker (agents.make_ground_truth) loads tests_file, then appends gen_tests_file
if it exists — so the same checker drives both the agent's early-termination and the scorer.

Requires: huggingface_hub, pyarrow, pandas  (data tooling; not needed to run the eval itself).

Examples:
  python build_codeforces_dataset.py --lo 3000 --hi 3500 --n 50 \
      --out-csv datasets/codeforces_3000_3500.csv
  python build_codeforces_dataset.py --lo 3000 --hi 3500 --n 50 \
      --out-csv datasets/codeforces_3000_3500.csv --with-generated   # heavy: pulls GBs of shards
"""
from __future__ import annotations

import argparse
import csv
import json
import os

csv.field_size_limit(10 ** 8)

HF_DATA = "datasets/open-r1/codeforces/data/*.parquet"
HF_GEN = "datasets/open-r1/codeforces/generated_tests/test_cases_{contest}.parquet"
STATEMENT_COLS = ["id", "title", "description", "input_format", "output_format", "note",
                  "examples", "official_tests", "generated_checker",
                  "time_limit", "memory_limit", "input_mode"]


def _statement(r) -> str:
    parts = [f"# {r['title']}", "", "## Statement", str(r["description"]),
             "", "## Input", str(r["input_format"]),
             "", "## Output", str(r["output_format"])]
    if str(r["note"] or "").strip():
        parts += ["", "## Note", str(r["note"])]
    ex = [] if r["examples"] is None else list(r["examples"])
    parts += ["", "## Examples"]
    for i, e in enumerate(ex, 1):
        parts += [f"### Example {i}", "Input:", "```", str(e["input"]).rstrip(), "```",
                  "Output:", "```", str(e["output"]).rstrip(), "```", ""]
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", type=int, default=3000)
    ap.add_argument("--hi", type=int, default=3500)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out-csv", default="datasets/codeforces_3000_3500.csv")
    ap.add_argument("--tests-dir", default="datasets/codeforces_tests")
    ap.add_argument("--gen-tests-dir", default="datasets/codeforces_tests_generated")
    ap.add_argument("--with-generated", action="store_true",
                    help="also pull the large generated tests into --gen-tests-dir (heavy: GBs)")
    ap.add_argument("--per-problem-generated", type=int, default=40,
                    help="cap on generated tests kept per problem")
    args = ap.parse_args()

    import pyarrow.parquet as pq
    from huggingface_hub import HfFileSystem
    fs = HfFileSystem()
    shards = sorted(fs.glob(HF_DATA))

    # --- light pass: ids + ratings + split, to select a rating-balanced set ---
    buckets = list(range(args.lo, args.hi + 1, 100))
    per_bucket = {rb: 0 for rb in buckets}
    base = args.n // len(buckets)
    for i, rb in enumerate(buckets):
        per_bucket[rb] = base + (1 if i >= len(buckets) - (args.n - base * len(buckets)) else 0)

    cand_pool = {rb: [] for rb in buckets}      # rb -> [(split_order, id)]
    for path in shards:
        split_order = 0 if "/test-" in path else 1   # split is in the filename, not a column
        with fs.open(path) as f:
            t = pq.read_table(f, columns=["id", "rating"]).to_pandas()
        for _, r in t.iterrows():
            try:
                rb = int(r["rating"])
            except (TypeError, ValueError):
                continue  # unrated (NaN) — skip
            if rb in cand_pool:
                cand_pool[rb].append((split_order, r["id"]))
    candidates = set()
    for rb in buckets:
        cand_pool[rb].sort()
        candidates.update(pid for _, pid in cand_pool[rb][: per_bucket[rb] * 3])

    # --- heavy pass: statement + official tests + checker for candidates ---
    rows = {}
    for path in shards:
        with fs.open(path) as f:
            t = pq.read_table(f, columns=STATEMENT_COLS + ["rating"]).to_pandas()
        for _, r in t[t["id"].isin(candidates)].iterrows():
            if r["id"] in rows:
                continue
            ot = [] if r["official_tests"] is None else list(r["official_tests"])
            rows[r["id"]] = dict(r=r, rating=int(r["rating"]), n=len(ot), ot=ot)
        if len(rows) >= len(candidates):
            break

    # final selection: per bucket, most official tests first
    final = []
    for rb in buckets:
        pool = [pid for pid in rows if rows[pid]["rating"] == rb and rows[pid]["n"] >= 1]
        pool.sort(key=lambda p: (-rows[p]["n"], p))
        final += pool[: per_bucket[rb]]

    os.makedirs(args.tests_dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.out_csv) or ".", exist_ok=True)
    if args.with_generated:
        os.makedirs(args.gen_tests_dir, exist_ok=True)

    with open(args.out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "problem", "solution"])
        for pid in final:
            d = rows[pid]; r = d["r"]; sid = pid.replace("/", "")
            # committed sidecar: official tests + checker
            with open(os.path.join(args.tests_dir, f"{sid}.json"), "w") as sf:
                json.dump({"tests": [{"input": str(x["input"]), "output": str(x["output"])}
                                     for x in d["ot"]],
                           "checker": str(r["generated_checker"] or "")}, sf)
            blob = {
                "tests_file": f"{args.tests_dir}/{sid}.json",
                "gen_tests_file": f"{args.gen_tests_dir}/{sid}.json",
                "time_limit": float(r["time_limit"]),
                "memory_limit": float(r["memory_limit"]),
                "input_mode": str(r["input_mode"]),
            }
            w.writerow([pid, _statement(r), json.dumps(blob)])

    print(f"wrote {args.out_csv} and {len(final)} sidecars in {args.tests_dir}/")

    if args.with_generated:
        from collections import defaultdict
        by_contest = defaultdict(list)
        for pid in final:
            by_contest[pid.split("/")[0]].append(pid)
        for contest, pids in sorted(by_contest.items()):
            try:
                with fs.open(HF_GEN.format(contest=contest)) as f:
                    gt = pq.read_table(f, columns=["problem_id", "input", "output"]).to_pandas()
            except Exception as e:
                print(f"  contest {contest}: generated tests unavailable ({e})")
                continue
            for pid in pids:
                sub = gt[gt["problem_id"] == pid].head(args.per_problem_generated)
                tests = [{"input": str(x["input"]), "output": str(x["output"])}
                         for _, x in sub.iterrows()]
                with open(os.path.join(args.gen_tests_dir, f"{pid.replace('/', '')}.json"), "w") as sf:
                    json.dump({"tests": tests}, sf)
                print(f"  {pid}: +{len(tests)} generated tests")


if __name__ == "__main__":
    main()
