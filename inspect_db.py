#!/usr/bin/env python3
"""
Read-only MongoDB database inspector. DB-agnostic: point it at ANY database and it
dumps a full structural report — collections, type (regular / time-series / view),
document counts, regular indexes (key, unique, sparse, TTL, partial), Atlas
search/vector indexes (name, type, status, definition), JSON-schema validators, and
time-series options.

READ-ONLY: issues only read commands (list_collections, list_indexes,
list_search_indexes, count_documents, aggregate $collStats). It never creates, drops,
updates, or writes anything. Safe to run against production.

Usage:
    export MONGODB_URI="mongodb+srv://..."
    python inspect_db.py --db agentic_capital_markets
    python inspect_db.py --db fin_migration --json > report.json
    python inspect_db.py --db leafy_bank_bian --no-counts          # skip counts (faster)
    python inspect_db.py --db agentic_capital_markets --grep index  # only index detail

Options:
    --db             database to inspect (required)
    --uri            connection string (default: $MONGODB_URI)
    --json           emit machine-readable JSON instead of the text report
    --no-counts      skip document counts (count_documents can be slow on huge collections)
    --include-system include system.* collections (hidden by default)
"""

import argparse
import json
import os
import sys

from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError


def collection_report(db, name, coll_type, options, want_counts):
    """Build a read-only detail dict for one collection."""
    coll = db[name]
    rep = {"name": name, "type": coll_type}

    # --- time-series options ---
    if "timeseries" in options:
        rep["timeseries"] = options["timeseries"]

    # --- view definition ---
    if coll_type == "view":
        rep["view_on"] = options.get("viewOn")
        rep["pipeline"] = options.get("pipeline")

    # --- JSON-schema validator ---
    if options.get("validator"):
        rep["validator"] = options["validator"]
        rep["validationLevel"] = options.get("validationLevel")
        rep["validationAction"] = options.get("validationAction")

    # --- document count (read-only; accurate but scans) ---
    if want_counts and coll_type != "view":
        try:
            rep["count"] = coll.count_documents({})
        except PyMongoError as e:
            rep["count"] = f"(error: {e})"

    # --- regular indexes ---
    indexes = []
    if coll_type != "view":
        try:
            for ix in coll.list_indexes():
                entry = {"name": ix["name"], "key": dict(ix["key"])}
                for flag in ("unique", "sparse", "expireAfterSeconds",
                             "partialFilterExpression", "collation"):
                    if flag in ix:
                        entry[flag] = ix[flag]
                indexes.append(entry)
        except PyMongoError as e:
            indexes = [{"error": str(e)}]
    rep["indexes"] = indexes

    # --- Atlas search / vector-search indexes ---
    search = []
    if coll_type != "view":
        try:
            for sx in coll.list_search_indexes():
                search.append({
                    "name": sx.get("name"),
                    "type": sx.get("type", "search"),
                    "status": sx.get("status"),
                    "queryable": sx.get("queryable"),
                    "definition": sx.get("latestDefinition", sx.get("definition")),
                })
        except OperationFailure:
            # cluster tier / version without Atlas Search; not an error for this tool
            pass
        except PyMongoError:
            pass
    rep["searchIndexes"] = search
    return rep


def gather(db, want_counts, include_system):
    out = []
    for info in sorted(db.list_collections(), key=lambda c: c["name"]):
        name = info["name"]
        if not include_system and name.startswith("system."):
            continue
        out.append(collection_report(
            db, name, info.get("type", "collection"), info.get("options", {}), want_counts))
    return out


def print_text(db_name, reports):
    line = "=" * 78
    print(line)
    print(f"DATABASE: {db_name}    collections: {len(reports)}")
    print(line)

    # summary table
    print(f"\n{'collection':40s} {'type':11s} {'docs':>10s} {'idx':>4s} {'srch':>5s}")
    print("-" * 78)
    for r in reports:
        cnt = r.get("count", "-")
        print(f"{r['name']:40s} {r['type']:11s} {str(cnt):>10s} "
              f"{len(r['indexes']):>4d} {len(r['searchIndexes']):>5d}")

    # per-collection detail
    for r in reports:
        print(f"\n{line}\n{r['name']}  ({r['type']})")
        if "count" in r:
            print(f"  documents: {r['count']}")
        if "timeseries" in r:
            print(f"  timeseries: {json.dumps(r['timeseries'])}")
        if "view_on" in r:
            print(f"  view on: {r['view_on']}  pipeline: {json.dumps(r['pipeline'])}")
        if "validator" in r:
            print(f"  validator: level={r.get('validationLevel')} "
                  f"action={r.get('validationAction')}")
        print(f"  indexes ({len(r['indexes'])}):")
        for ix in r["indexes"]:
            if "error" in ix:
                print(f"    ! {ix['error']}")
                continue
            extra = "".join(
                f"  {k}={ix[k]}" for k in
                ("unique", "sparse", "expireAfterSeconds", "partialFilterExpression")
                if k in ix)
            print(f"    {ix['name']:42s} {json.dumps(ix['key'])}{extra}")
        if r["searchIndexes"]:
            print(f"  search/vector indexes ({len(r['searchIndexes'])}):")
            for sx in r["searchIndexes"]:
                print(f"    {sx['name']:30s} type={sx['type']} "
                      f"status={sx.get('status')} queryable={sx.get('queryable')}")
                if sx.get("definition"):
                    print(f"      definition: {json.dumps(sx['definition'], default=str)}")


def main():
    ap = argparse.ArgumentParser(description="Read-only MongoDB database inspector.")
    ap.add_argument("--db", required=True, help="database to inspect")
    ap.add_argument("--uri", default=os.getenv("MONGODB_URI"), help="connection string")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--no-counts", action="store_true", help="skip document counts")
    ap.add_argument("--include-system", action="store_true", help="include system.* collections")
    args = ap.parse_args()

    if not args.uri:
        sys.exit("ERROR: no connection string (pass --uri or set MONGODB_URI)")

    client = MongoClient(args.uri)
    db = client[args.db]
    reports = gather(db, want_counts=not args.no_counts, include_system=args.include_system)

    if args.json:
        print(json.dumps({"database": args.db, "collections": reports},
                         indent=2, default=str))
    else:
        print_text(args.db, reports)
    client.close()


if __name__ == "__main__":
    main()
