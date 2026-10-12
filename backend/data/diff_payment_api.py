#!/usr/bin/env python3
"""Before/after check that the payment API is unchanged by the lifecycle-history-out migration
(success criterion 3). Talks HTTP to the transactions service; touches no database.

    # before cutover, on the old build
    python diff_payment_api.py snapshot --base-url http://localhost:8000 --out before.json
    # after cutover, on the new build, same payment ids
    python diff_payment_api.py snapshot --base-url http://localhost:8000 --out after.json --ids before.json
    python diff_payment_api.py compare before.json after.json

Ignored in the comparison: `lifecycle.eventCount` and `lifecycle.checkCount` (new on purpose) and
`createdAt`/`updatedAt` wobble is NOT ignored — a payment must not change between snapshots, so
snapshot a quiet system.
"""

import argparse
import json
import sys
import urllib.request

_NEW_ON_PURPOSE = (("lifecycle", "eventCount"), ("lifecycle", "checkCount"))


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def snapshot(base: str, out: str, ids_from: str | None, sample: int) -> None:
    if ids_from:
        ids = list(json.load(open(ids_from)))
    else:
        page = _get(f"{base}/workflow/payments?limit={min(sample, 100)}")
        ids = [p["paymentId"] for p in page["items"]]
    data = {pid: _get(f"{base}/workflow/payments/{pid}") for pid in ids}
    json.dump(data, open(out, "w"), indent=1, sort_keys=True, default=str)
    print(f"{len(data)} payment(s) written to {out}")


def normalise(payment: dict) -> dict:
    out = json.loads(json.dumps(payment, sort_keys=True))
    for parent, key in _NEW_ON_PURPOSE:
        (out.get(parent) or {}).pop(key, None)
    out.pop("_id", None)
    return out


def diff(a, b, path=""):
    """Yield the paths where two JSON values differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                yield f"{path}.{key}: only in {'after' if key in b else 'before'}"
            else:
                yield from diff(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}: {str(a)[:60]!r} != {str(b)[:60]!r}"


def compare(before: str, after: str) -> int:
    old, new = json.load(open(before)), json.load(open(after))
    failures = 0
    for pid in sorted(set(old) | set(new)):
        if pid not in old or pid not in new:
            print(f"FAIL {pid}: missing from {'before' if pid not in old else 'after'}")
            failures += 1
            continue
        problems = list(diff(normalise(old[pid]), normalise(new[pid])))
        if problems:
            failures += 1
            print(f"FAIL {pid}:")
            for line in problems[:10]:
                print("   ", line)
    print(f"{'PASS' if not failures else 'FAIL'}: {len(set(old) | set(new))} payment(s), {failures} differing")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--base-url", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--ids", help="reuse the payment ids of an earlier snapshot")
    snap.add_argument("--sample", type=int, default=50)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    args = parser.parse_args()
    if args.cmd == "snapshot":
        snapshot(args.base_url, args.out, args.ids, args.sample)
        return 0
    return compare(args.before, args.after)


if __name__ == "__main__":
    sys.exit(main())
