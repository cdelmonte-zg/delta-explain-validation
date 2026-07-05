#!/usr/bin/env python3
"""Validate delta-explain's pruning claims against a real NYC-taxi Delta table.

This is an independent, black-box check of the tool. It never reads
delta-explain's source and never touches an internal diagnostic flag: every
expectation is asserted through the *public* contract only -- the CLI and the
versioned JSON report (schema documented in the delta-explain repo). If it runs
green against the binary you get from `pip install delta-explain` or
`cargo install delta-explain`, the claims below hold for that release.

The table under test is `taxi-nyc/`: a small, real Delta table written by a
real Delta writer from NYC TLC yellow-taxi trip data (January 2024), partitioned
by `pickup_date`, with per-file min/max statistics -- the shape a production
table has, not a synthetic blob. See `create_taxi_table.py` to regenerate it.

Four sections:

  A. Per-case expectations. A battery of predicates, each with declared
     expectations read straight from the JSON report: classification buckets
     (partition-safe / partition-exact / stats-safe / unsplittable), the
     confidence label, diagnostic note codes, and the per-phase survivor
     counts (input/output files of each pruning phase).
  B. Metamorphic invariants. Predicates that are logically equivalent must
     produce identical kept sets: a negated form and its plain form; a prefix
     LIKE and the lexicographic range it rewrites to; a substring LIKE over a
     partition column and the equality it decides; a factored OR and its
     expansion. And a degraded predicate (one carrying an unsupported fragment)
     must keep exactly the files its supported remainder keeps on its own.
  C. Differential oracle. An independent replay of the Delta log in plain
     Python -- add actions, partition values, per-column min/max -- evaluates
     each predicate conservatively ("may this file contain a matching row?")
     and must reproduce the tool's kept set file by file. This is the soundness
     check: the tool must never drop a file the oracle keeps.
  D. Contract surface. `--explain-why` names the expected reason codes for a
     query that cannot prune; the error contract holds (unknown column and
     malformed SQL exit 1 with empty stdout, a usage error exits 2).

Usage:
    pip install delta-explain           # or: cargo install delta-explain
    python validate.py                  # finds `delta-explain` on PATH

    DX_BIN=/path/to/delta-explain python validate.py   # explicit binary
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE = Path(os.environ.get("DX_TABLE", HERE / "taxi-nyc"))
BIN = os.environ.get("DX_BIN") or shutil.which("delta-explain")

# A binary built with `cargo build --features debug-ir` exposes --debug-ir, the
# internal dump of every intermediate representation. It is off by default and
# absent from the shipped release, so the black-box sections below never need
# it; section E (IR & interpretation) uses it, when present, to verify that the
# layer-by-layer dumps the companion article quotes are real and current.
DEBUG_BIN = os.environ.get("DX_DEBUG_BIN")

CHECKS = []  # (section, case, name, ok, detail)


def check(section, case, name, ok, detail=""):
    CHECKS.append((section, case, name, bool(ok), detail))


def run(*args):
    proc = subprocess.run([str(BIN), str(TABLE), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def report_for(sql, *extra):
    """Run one analysis and return the parsed JSON report (verbose, so the
    per-file kept flags are present)."""
    code, out, err = run("-w", sql, "--format", "json", "--verbose", *extra)
    if code != 0:
        raise RuntimeError(f"run failed for {sql!r} (exit {code}): {err.strip()}")
    return json.loads(out)


def kept_set(report):
    return {f["path"] for f in report.get("files", []) if f["kept"]}


def phases_of(report):
    return [(p["name"].split("(")[0].strip(), p["input_files"], p["output_files"])
            for p in report.get("phases", [])]


# ── Section A: per-case expectations ────────────────────────────────────────

# Expectation keys are all optional; only declared ones are checked. Buckets
# use the JSON's snake_case analysis fields; a bucket declared None must be
# absent (null) in the report.
BATTERY = [
    {
        # Both mechanisms cut: partition pruning isolates the day (20 -> 4),
        # then data skipping drops the two low-distance files in it (4 -> 2).
        "name": "plain-and",
        "sql": "pickup_date = '2024-01-03' AND trip_distance > 5",
        "partition_safe": "pickup_date = '2024-01-03'",
        "stats_safe": "trip_distance > 5",
        "unsplittable": None,
        "confidence": "conservative",
        "notes": [],
        "phases": [("Partition pruning", 20, 4), ("Data skipping", 4, 2)],
        "final": 2,
    },
    {
        "name": "de-morgan",
        "sql": "NOT (pickup_date != '2024-01-03' OR trip_distance <= 5)",
        "partition_safe": "pickup_date = '2024-01-03'",
        "stats_safe": "trip_distance > 5",
        "confidence": "conservative",
        "phases": [("Partition pruning", 20, 4), ("Data skipping", 4, 2)],
        "final": 2,
    },
    {
        "name": "partition-only",
        "sql": "pickup_date = '2024-01-03'",
        "partition_safe": "pickup_date = '2024-01-03'",
        "stats_safe": None,
        "confidence": "exact",
        "phases": [("Partition pruning", 20, 4)],
        "final": 4,
    },
    {
        "name": "data-skipping",
        "sql": "trip_distance > 8",
        "partition_safe": None,
        "stats_safe": "trip_distance > 8",
        "confidence": "conservative",
        "phases": [("Data skipping", 20, 7)],
        "final": 7,
    },
    {
        # #72: a prefix LIKE on a string column is rewritten into a range and
        # prunes as partition-safe. All five dates match '2024-01-0%', so the
        # partition phase keeps all 20 files; the stats conjunct does the cutting.
        "name": "prefix-like",
        "sql": "pickup_date LIKE '2024-01-0%' AND trip_distance > 8",
        "partition_safe": "pickup_date >= '2024-01-0' AND pickup_date < '2024-01-1'",
        "stats_safe": "trip_distance > 8",
        "confidence": "conservative",
        "phases": [("Partition pruning", 20, 20), ("Data skipping", 20, 7)],
        "final": 7,
    },
    {
        # #75: a substring LIKE over a partition column is evaluated directly
        # against the partition literals -- partition-exact -- and prunes.
        "name": "contains-like",
        "sql": "pickup_date LIKE '%01-03%' AND trip_distance > 5",
        "partition_safe": None,
        "partition_exact": "pickup_date LIKE '%01-03%'",
        "stats_safe": "trip_distance > 5",
        "confidence": "conservative",
        "phases": [("Partition pruning", 20, 4), ("Data skipping", 4, 2)],
        "final": 2,
    },
    {
        # A function over a column stays unsupported: it degrades, and the
        # supported conjunct still prunes.
        "name": "degraded-function",
        "sql": "UPPER(pickup_date) = '2024-01-03' AND trip_distance > 8",
        "partition_safe": None,
        "stats_safe": "trip_distance > 8",
        "unsplittable": "UPPER(pickup_date) = '2024-01-03'",
        "confidence": "incomplete",
        "notes": ["UNSUPPORTED_EXPRESSION"],
        "phases": [("Data skipping", 20, 7)],
        "final": 7,
    },
    {
        "name": "mixed-or",
        "sql": "pickup_date = '2024-01-03' OR trip_distance > 30",
        "unsplittable": "pickup_date = '2024-01-03' OR trip_distance > 30",
        "confidence": "incomplete",
        "notes": ["UNSPLITTABLE_OR"],
        "phases": [("Data skipping", 20, 6)],
        "final": 6,
    },
    {
        "name": "column-to-column",
        "sql": "tip_amount > fare_amount",
        "unsplittable": "tip_amount > fare_amount",
        "confidence": "incomplete",
        "notes": ["UNSUPPORTED_EXPRESSION"],
        "phases": [("Data skipping", 20, 20)],
        "final": 20,
    },
    {
        # OR factoring releases the common conjunct to partition pruning.
        "name": "or-factoring",
        "sql": "(pickup_date = '2024-01-03' AND trip_distance > 5) "
               "OR (pickup_date = '2024-01-03' AND fare_amount > 50)",
        "partition_safe": "pickup_date = '2024-01-03'",
        "stats_safe": "trip_distance > 5 OR fare_amount > 50",
        "confidence": "conservative",
        "phases": [("Partition pruning", 20, 4), ("Data skipping", 4, 4)],
        "final": 4,
    },
]

RESULTS = {}  # name -> report


def section_a():
    for case in BATTERY:
        name = case["name"]
        rep = report_for(case["sql"])
        RESULTS[name] = rep
        analysis = rep["analysis"]

        for bucket in ("partition_safe", "partition_exact", "stats_safe", "unsplittable"):
            if bucket in case:
                check("A", name, f"bucket {bucket}",
                      analysis.get(bucket) == case[bucket],
                      f"{analysis.get(bucket)!r} vs expected {case[bucket]!r}")
        if "confidence" in case:
            check("A", name, "confidence",
                  analysis["confidence"] == case["confidence"],
                  f"{analysis['confidence']!r} vs {case['confidence']!r}")
        if "notes" in case:
            codes = [n["code"] for n in analysis.get("notes", [])]
            check("A", name, "note codes", codes == case["notes"],
                  f"{codes} vs expected {case['notes']}")
        if "phases" in case:
            check("A", name, "phase survivor counts",
                  phases_of(rep) == case["phases"],
                  f"{phases_of(rep)} vs expected {case['phases']}")
        if "final" in case:
            check("A", name, "final file count",
                  rep["final_files"] == case["final"],
                  f"{rep['final_files']} vs expected {case['final']}")


# ── Section B: metamorphic invariants ───────────────────────────────────────

def section_b():
    base = kept_set(RESULTS["plain-and"])
    check("B", "de-morgan", "identical kept set to plain form",
          kept_set(RESULTS["de-morgan"]) == base,
          f"{sorted(kept_set(RESULTS['de-morgan']))} vs {sorted(base)}")

    dn = kept_set(report_for("NOT (NOT (trip_distance > 8))"))
    plain_ds = kept_set(RESULTS["data-skipping"])
    check("B", "double-negation", "identical kept set to plain form",
          dn == plain_ds, f"{sorted(dn)} vs {sorted(plain_ds)}")

    factored = kept_set(report_for(
        "pickup_date = '2024-01-03' AND (trip_distance > 5 OR fare_amount > 50)"))
    check("B", "or-factoring", "identical kept set to expanded form",
          kept_set(RESULTS["or-factoring"]) == factored,
          f"{sorted(kept_set(RESULTS['or-factoring']))} vs {sorted(factored)}")

    # #72: prefix LIKE equals the lexicographic range it rewrites to.
    rng = kept_set(report_for(
        "pickup_date >= '2024-01-0' AND pickup_date < '2024-01-1' AND trip_distance > 8"))
    check("B", "prefix-like", "identical kept set to lexicographic range",
          kept_set(RESULTS["prefix-like"]) == rng,
          f"{sorted(kept_set(RESULTS['prefix-like']))} vs {sorted(rng)}")

    # #75: substring LIKE over the partition column equals the equality it
    # decides (only '2024-01-03' contains '01-03').
    eq = kept_set(report_for("pickup_date = '2024-01-03' AND trip_distance > 5"))
    check("B", "contains-like", "identical kept set to the equality it decides",
          kept_set(RESULTS["contains-like"]) == eq,
          f"{sorted(kept_set(RESULTS['contains-like']))} vs {sorted(eq)}")

    # A degraded predicate keeps exactly what its supported remainder keeps.
    stripped = kept_set(RESULTS["data-skipping"])  # remainder is trip_distance > 8
    check("B", "degraded-function", "same kept set as its stripped remainder",
          kept_set(RESULTS["degraded-function"]) == stripped,
          f"{sorted(kept_set(RESULTS['degraded-function']))} vs {sorted(stripped)}")

    check("B", "column-to-column", "unsupported predicate keeps all files",
          len(kept_set(RESULTS["column-to-column"])) == 20)


# ── Section C: differential oracle ──────────────────────────────────────────

def replay_log(table):
    """Independent replay of the JSON commits: adds minus removes, with
    partition values and per-column min/max parsed from the stats string."""
    files = {}
    for commit in sorted((Path(table) / "_delta_log").glob("*.json")):
        for line in commit.read_text().splitlines():
            action = json.loads(line)
            if "add" in action:
                add = action["add"]
                stats = json.loads(add["stats"]) if add.get("stats") else None
                files[add["path"]] = {"partition": add.get("partitionValues", {}),
                                      "stats": stats}
            elif "remove" in action:
                files.pop(action["remove"]["path"], None)
    return files


# Oracle predicate language: nested tuples, evaluated conservatively ("may this
# file contain a matching row?") the way the articles describe partition pruning
# and min/max data skipping. Partition columns compare against the concrete
# literal (exact); stats columns compare against the file's min/max interval.
def may_match(file, node):
    op = node[0]
    if op == "and":
        return all(may_match(file, c) for c in node[1])
    if op == "or":
        return any(may_match(file, c) for c in node[1])

    col, val = node[1], node[2]
    if col in file["partition"]:
        v = file["partition"][col]
        return {
            "eq": v == val, "ne": v != val, "in": v in val,
            "ge": v >= val, "gt": v > val, "le": v <= val, "lt": v < val,
        }[op]

    stats = file["stats"]
    lo, hi = stats["minValues"][col], stats["maxValues"][col]
    if op == "gt":
        return hi > val
    if op == "ge":
        return hi >= val
    if op == "lt":
        return lo < val
    if op == "le":
        return lo <= val
    if op == "eq":
        return lo <= val <= hi
    if op == "between":
        return hi >= val and lo <= node[3]
    if op == "notnull":
        return stats["nullCount"][col] < stats["numRecords"]
    raise ValueError(f"oracle: unsupported op {op!r}")


# (battery case, oracle tree of the predicate the tool actually evaluated, i.e.
# after normalization and after stripping unsupported fragments). contains-like
# is intentionally absent: a substring LIKE is outside the oracle's language,
# so section B carries that case instead.
ORACLE_CASES = [
    ("plain-and", ("and", [("eq", "pickup_date", "2024-01-03"), ("gt", "trip_distance", 5)])),
    ("de-morgan", ("and", [("eq", "pickup_date", "2024-01-03"), ("gt", "trip_distance", 5)])),
    ("partition-only", ("eq", "pickup_date", "2024-01-03")),
    ("data-skipping", ("gt", "trip_distance", 8)),
    ("prefix-like", ("and", [("ge", "pickup_date", "2024-01-0"),
                             ("lt", "pickup_date", "2024-01-1"),
                             ("gt", "trip_distance", 8)])),
    ("degraded-function", ("gt", "trip_distance", 8)),
    ("mixed-or", ("or", [("eq", "pickup_date", "2024-01-03"), ("gt", "trip_distance", 30)])),
    ("or-factoring", ("and", [("eq", "pickup_date", "2024-01-03"),
                              ("or", [("gt", "trip_distance", 5), ("gt", "fare_amount", 50)])])),
]


def section_c():
    files = replay_log(TABLE)
    check("C", "replay", "log replay sees the whole snapshot",
          len(files) == 20, f"{len(files)} files")
    for name, tree in ORACLE_CASES:
        oracle = {p for p, f in files.items() if may_match(f, tree)}
        tool = kept_set(RESULTS[name])
        check("C", name, "oracle kept set == tool kept set", oracle == tool,
              f"oracle-only {sorted(oracle - tool)}, tool-only {sorted(tool - oracle)}")


# ── Section D: contract surface ─────────────────────────────────────────────

def section_d():
    # A query on a non-partition column whose values scatter across every file
    # cannot prune; --explain-why must name both reasons.
    rep = report_for("PULocationID = 132", "--explain-why")
    codes = {d["code"] for d in rep.get("explain", [])}
    check("D", "explain-why", "names NO_PARTITION_FILTER and WEAK_DATA_SKIPPING",
          {"NO_PARTITION_FILTER", "WEAK_DATA_SKIPPING"} <= codes, f"{sorted(codes)}")

    code, out, err = run("-w", "no_such_column = 1")
    check("D", "unknown-column", "exit 1, empty stdout",
          code == 1 and out == "", f"exit {code}, {len(out)} stdout bytes")

    code, out, _ = run("-w", "AND AND")
    check("D", "malformed-sql", "exit 1, empty stdout",
          code == 1 and out == "", f"exit {code}, {len(out)} stdout bytes")

    code, _, _ = run("--nonsense-flag")
    check("D", "usage-error", "exit 2", code == 2, f"exit {code}")


# ── Section E: IR & interpretation (the layer walk) ─────────────────────────
#
# The heart of the tool is a compiler-style shape: sqlparser is used once, its
# output is converted into delta-explain's own owned AST, and that single AST is
# then read by three independent interpreters -- the analyzer (which classifies
# fragments), the kernel bridge (which lowers to delta_kernel's Predicate IR),
# and the partition-literal evaluator (which decides partition-exact fragments
# against the concrete literals). Because all three read the same AST, "what the
# user reads", "what the kernel evaluates", and "what the literals decide" can
# never drift. This section verifies that a query really moves through those IR
# layers as documented, using the internal --debug-ir dump.

# The tracer: the awkward, negated spelling of the plain-and predicate, chosen
# so normalization visibly does work (De Morgan pushes NOT to the leaves).
TRACER = "NOT (pickup_date != '2024-01-03' OR trip_distance <= 5)"
TRACER_EXPECT = {
    "parsed": "NOT (pickup_date <> '2024-01-03' OR trip_distance <= 5)",
    "normalized": "pickup_date = '2024-01-03' AND trip_distance > 5",
    "partition_safe": "pickup_date = '2024-01-03'",
    "stats_safe": "trip_distance > 5",
    "stripped": "pickup_date = '2024-01-03' AND trip_distance > 5",
    "survivors": {"baseline": 20, "partition": 4, "full": 2},
}


def dump_for(sql):
    """Run the debug binary with --debug-ir and return the parsed layers."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ir.txt"
        proc = subprocess.run(
            [DEBUG_BIN, str(TABLE), "-w", sql, "--debug-ir", str(path),
             "--format", "json"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"debug run failed for {sql!r}: {proc.stderr.strip()}")
        return parse_dump(path.read_text())


def parse_dump(dump):
    sections, current = {}, "header"
    sections[current] = []
    for line in dump.splitlines():
        m = re.match(r"^== (.+) ==$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        else:
            sections[current].append(line)
    sections = {k: "\n".join(v).strip() for k, v in sections.items()}

    def rendered(section):
        m = re.search(r"^rendered: (.+)$", sections.get(section, ""), re.M)
        return m.group(1) if m else None

    def bucket(name):
        cls = sections.get("classification", "")
        m = re.search(rf"{name}: Some\(\s*\"(.*?)\",?\s*\)", cls, re.S)
        return m.group(1) if m else None

    full = sections.get("kernel predicate: full scan", "")
    m = re.search(r"^scan predicate after stripping unsupported fragments: (.+)$",
                  full, re.M)
    stripped = m.group(1) if m else "(none)"
    kernel_ops = re.findall(r"op: (\w+)", full)

    survivors = {}
    body = sections.get("survivor sets", "")
    for key, pat in [("baseline", r"^baseline: (\d+) files"),
                     ("partition", r"^partition-only scan: (\d+) files"),
                     ("full", r"^full scan: (\d+) files")]:
        m = re.search(pat, body, re.M)
        survivors[key] = int(m.group(1)) if m else None

    return {
        "parsed": rendered("owned AST (parsed)"),
        "normalized": rendered("owned AST (normalized)"),
        "partition_safe": bucket("partition_safe"),
        "partition_exact": bucket("partition_exact"),
        "stats_safe": bucket("stats_safe"),
        "unsplittable": bucket("unsplittable"),
        "stripped": stripped,
        "kernel_ops": kernel_ops,
        "survivors": survivors,
    }


def section_e():
    if not DEBUG_BIN or not Path(DEBUG_BIN).exists():
        print("note: DX_DEBUG_BIN not set (no --features debug-ir binary); "
              "skipping section E (IR & interpretation).\n")
        return
    d = dump_for(TRACER)
    for key in ("parsed", "normalized", "partition_safe", "stats_safe", "stripped"):
        check("E", "tracer", f"IR layer: {key}",
              d[key] == TRACER_EXPECT[key],
              f"{d[key]!r} vs expected {TRACER_EXPECT[key]!r}")
    check("E", "tracer", "kernel bridge lowered And(Equal, GreaterThan)",
          d["kernel_ops"] == ["And", "Equal", "GreaterThan"],
          f"{d['kernel_ops']}")
    check("E", "tracer", "survivor sets across the three scans",
          d["survivors"] == TRACER_EXPECT["survivors"],
          f"{d['survivors']} vs {TRACER_EXPECT['survivors']}")


def show_layers():
    """Print the tracer moving from layer to layer -- the companion article's
    'one predicate, end to end' walk, regenerated from the live binary."""
    if not DEBUG_BIN or not Path(DEBUG_BIN).exists():
        sys.exit("--layers needs a debug-ir binary: build delta-explain with "
                 "`cargo build --features debug-ir` and set DX_DEBUG_BIN to it.")
    d = dump_for(TRACER)
    ds = report_for(TRACER)  # for the classification the user actually reads
    print(f"QUERY:  {TRACER}\n")
    print("  [1] parse -> owned AST (sqlparser used once, then converted)")
    print(f"      {d['parsed']}\n")
    print("  [2] normalize (De Morgan; prefix-LIKE -> range; OR factoring)")
    print(f"      {d['normalized']}\n")
    print("  --- one owned AST, three interpreters ---\n")
    print("  [3a] analyzer      -> classification (what the user reads)")
    print(f"       partition-safe : {d['partition_safe']}")
    print(f"       stats-safe     : {d['stats_safe']}")
    print(f"       confidence     : {ds['analysis']['confidence']}\n")
    print("  [3b] kernel bridge -> delta_kernel Predicate IR (what the kernel evaluates)")
    print(f"       lowered ops    : {' '.join(d['kernel_ops'])}")
    print(f"       scan predicate : {d['stripped']}\n")
    print("  [3c] partition eval-> decides partition-exact fragments against the")
    print("       partition literals (idle here; fires e.g. on pickup_date LIKE '%01-03%')\n")
    s = d["survivors"]
    print("  [4] survivor sets (comparative metadata scans over one snapshot)")
    print(f"      baseline {s['baseline']} -> partition-only {s['partition']} "
          f"-> full {s['full']}")


# ── Report ──────────────────────────────────────────────────────────────────

def main():
    if "--layers" in sys.argv[1:]:
        if not BIN:
            sys.exit("delta-explain not found: set DX_BIN or put it on PATH.")
        show_layers()
        return
    if not BIN:
        sys.exit("delta-explain not found: set DX_BIN or put it on PATH "
                 "(pip install delta-explain).")
    if not TABLE.exists():
        sys.exit(f"table not found at {TABLE}; run create_taxi_table.py first.")

    section_a()
    section_b()
    section_c()
    section_d()
    section_e()

    failures = [c for c in CHECKS if not c[3]]
    width = max(len(f"{s}:{case}:{name}") for s, case, name, _, _ in CHECKS)
    for s, case, name, ok, detail in CHECKS:
        label = f"{s}:{case}:{name}"
        line = f"{'PASS' if ok else 'FAIL'}  {label:<{width}}  {detail if not ok else ''}"
        print(line.rstrip())
    print(f"\n{len(CHECKS)} checks, {len(CHECKS) - len(failures)} passed, "
          f"{len(failures)} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
