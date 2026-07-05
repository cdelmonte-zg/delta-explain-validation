# delta-explain-validation

An independent, black-box validation of [delta-explain](https://github.com/cdelmonte-zg/delta-explain)
against a **real NYC-taxi Delta table** — and a window into the IR pipeline at
its core.

[delta-explain](https://github.com/cdelmonte-zg/delta-explain) is a
metadata-only diagnostic for Delta Lake file pruning: given a table and a
`WHERE` predicate, it reports which files partition pruning and data skipping
would eliminate, reading only the transaction log, never the data. This repo
does two things:

1. **Shows a query move from layer to layer.** delta-explain is shaped like a
   small compiler: SQL is parsed once into an owned AST, and that single AST is
   then read by three independent interpreters. `validate.py --layers` prints a
   real query descending through those intermediate representations.
2. **Checks that every claim holds against real data.** 82 assertions over a
   real Delta table — classification, survivor counts, metamorphic invariants,
   and an independent log-replay oracle — all through delta-explain's *public*
   contract (its CLI and versioned JSON), so a green run certifies the release
   you can `pip install`.

## The theme: one parse, three interpreters

The reason a metadata pruning tool needs a compiler's shape is drift. "What the
kernel evaluates", "what the user is told", and "what the partition literals
decide" must never disagree. delta-explain guarantees that by giving all three a
single source of truth — one owned AST — and three interpreters over it:

```
   SQL  --sqlparser-->  owned AST  --normalize-->  owned AST'
                                                      |
                        +-----------------------------+-----------------------------+
                        |                             |                             |
                   analyzer                     kernel bridge              partition-literal
                (classification)              (delta_kernel IR)           evaluator (exact)
                what the user reads          what the kernel runs        what the literals decide
```

`validate.py --layers` makes that concrete, regenerated live from the binary:

```
QUERY:  NOT (pickup_date != '2024-01-03' OR trip_distance <= 5)

  [1] parse -> owned AST (sqlparser used once, then converted)
      NOT (pickup_date <> '2024-01-03' OR trip_distance <= 5)

  [2] normalize (De Morgan; prefix-LIKE -> range; OR factoring)
      pickup_date = '2024-01-03' AND trip_distance > 5

  --- one owned AST, three interpreters ---

  [3a] analyzer      -> classification (what the user reads)
       partition-safe : pickup_date = '2024-01-03'
       stats-safe     : trip_distance > 5
       confidence     : conservative

  [3b] kernel bridge -> delta_kernel Predicate IR (what the kernel evaluates)
       lowered ops    : And Equal GreaterThan
       scan predicate : pickup_date = '2024-01-03' AND trip_distance > 5

  [3c] partition eval-> decides partition-exact fragments against the
       partition literals (idle here; fires e.g. on pickup_date LIKE '%01-03%')

  [4] survivor sets (comparative metadata scans over one snapshot)
      baseline 5 -> partition-only 1 -> full 1
```

The negated spelling of the query is deliberate: normalization has to visibly do
work (push the `NOT` to the leaves) before the three interpreters ever see it.

## What is validated

`validate.py` runs five sections; each check is a single assertion.

| Section | What it certifies | Needs |
|---|---|---|
| **A. Per-case expectations** | For a battery of predicates, the classification buckets, confidence label, diagnostic note codes, and per-phase survivor counts match. | public JSON |
| **B. Metamorphic invariants** | Logically equivalent predicates keep the identical file set: negated vs plain, prefix `LIKE` vs its lexicographic range, substring `LIKE` over a partition column vs the equality it decides, factored `OR` vs its expansion; a degraded predicate keeps exactly what its supported remainder keeps. | public JSON |
| **C. Differential oracle** | An independent replay of the Delta log in plain Python evaluates each predicate conservatively and reproduces the tool's kept set file by file. This is the **soundness** check: the tool must never drop a file the oracle keeps. | public JSON |
| **D. Contract surface** | `--explain-why` names the expected reason codes for a query that cannot prune; the error contract holds (exit codes, empty stdout on failure). | public JSON |
| **E. IR & interpretation** | The layer-by-layer dumps the [companion article](#related) quotes are real and current: parse, normalization, classification, kernel lowering, and survivor sets all match. | debug-ir build |

Sections A–D go through the shipped binary's public contract only, so they run
against whatever you `pip install`. Section E verifies the internal IR dumps and
needs a `--features debug-ir` build (it is skipped, with a note, otherwise).

## Run it

```bash
pip install delta-explain          # or: cargo install delta-explain
python validate.py                 # 75 checks (A-D); section E skipped
```

To include the IR-layer verification and the `--layers` walk, build delta-explain
with the internal diagnostic feature and point the script at it:

```bash
# in a delta-explain checkout:
cargo build --features debug-ir

# here:
DX_DEBUG_BIN=/path/to/delta-explain/target/debug/delta-explain python validate.py           # 82 checks
DX_DEBUG_BIN=/path/to/delta-explain/target/debug/delta-explain python validate.py --layers  # the walk
```

Environment: `DX_BIN` (release binary; defaults to `delta-explain` on `PATH`),
`DX_DEBUG_BIN` (debug-ir build, enables section E), `DX_TABLE` (defaults to the
bundled `taxi-nyc/`).

## The table

`taxi-nyc/` is a small, real Delta table written by a real Delta writer
(`deltalake`) from NYC TLC yellow-taxi trip data, January 2024 — partitioned by
`pickup_date`, five days, with the per-file min/max statistics a production
table carries. It is checked in (172 KB) so the validation needs no network. To
regenerate it from source:

```bash
pip install -r requirements.txt
python create_taxi_table.py        # downloads the TLC source, writes taxi-nyc/
```

Data attribution: [NYC Taxi & Limousine Commission (TLC) trip record data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page),
redistributed here as a small derived subset for testing under the TLC's data
usage terms.

## Related

- [delta-explain](https://github.com/cdelmonte-zg/delta-explain) — the tool under test.
- *How delta-explain Measures Pruning* — the companion deep dive on the IR
  pipeline and the three interpreters; the dumps it quotes are the ones section E
  regenerates.
