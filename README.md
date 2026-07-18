# Gaia DR3 EpochPhotometry Analyzer

Identifies variable astronomical objects in Gaia DR3 epoch photometry data using IRIS Embedded Python and [polars](https://pola.rs/) for parallel gzip CSV processing. Built for [InterSystems Employee Programming Challenge #1](https://openexchange.intersystems.com/contest/47).

## Challenge

InterSystems Employee Programming Challenge #1 posed a simple question: given 20 gzip-compressed CSV files from the [Gaia DR3 epoch photometry archive](https://www.cosmos.esa.int/web/gaia/dr3), find every astronomical source whose BP or RP flux changed by more than 100% over its observation period.

The files total ~380 MB compressed. Each row is one source's complete light curve, arrays of flux measurements stored as quoted JSON-style strings like `"[1234.5,null,6789.0,...]"`. There are 48 columns per row but we need only 3.

For each qualifying source, output:

| Column | Description |
|---|---|
| `source_id` | Gaia source identifier |
| `bp_min_flux` | Minimum valid BP flux across all observations |
| `bp_max_flux` | Maximum valid BP flux across all observations |
| `rp_min_flux` | Minimum valid RP flux across all observations |
| `rp_max_flux` | Maximum valid RP flux across all observations |
| `percentage_change` | `max((bp_max−bp_min)/bp_min, (rp_max−rp_min)/rp_min) × 100` |

Invalid (null/NaN) flux values are ignored. Sources with non-positive minimum flux are excluded.

## Solution

An ObjectScript entry point (`RunScript.mac`) delegates all data processing to a Python module (`process.py`) via IRIS Embedded Python. The Python module reads all 20 `.csv.gz` files concurrently using `ThreadPoolExecutor` (one thread per file), with [polars](https://pola.rs/) performing native gzip decompression, column projection, array parsing, and vectorized filtering entirely in Rust.

## Prerequisites

- [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- [Docker Desktop](https://www.docker.com/products/docker-desktop)

## Build

Clone the repository and place the 20 Gaia EpochPhotometry `.csv.gz` files into `data/in/`:

```bash
git clone <your-repo-url>
cd intersystems-challenge-GAIA
```

Start the IRIS container:

```bash
docker-compose up --build -d
```

## Run

Open an IRIS terminal:

```bash
docker compose exec iris iris session iris -U USER
```

Compile and run:

```objectscript
do $System.OBJ.Load("/home/irisowner/dev/src/RunScript.mac","ck")
do ^RunScript
```

Output is written to `data/out/results.csv`.

## How the evaluator checks your work

```bash
docker-compose exec iris iris session iris
USER>do ^RunScript
```

## Performance

~1.71s average over 20 files (~380 MB compressed), measured with `$ZHOROLOG` on IRIS Community 2026.1.

Profiling showed `read_csv` accounts for 94% of elapsed time — the bottleneck is I/O and gzip decompression, not the flux computation. Key design decisions:

- **Direct `.csv.gz` reads** — polars' native Rust decompressor is faster than pre-extracting to plain CSV (more bytes to read from disk)
- **`ThreadPoolExecutor(20)`** — one thread per file saturates I/O better than polars' internal lazy scan scheduler for this workload
- **Early filter `max >= min * 2`** — equivalent to `>100%` change but avoids division; eliminates most rows before the percentage is computed
- Only 3 of 48 CSV columns are read (`source_id`, `bp_flux`, `rp_flux`)

## Technologies

- [InterSystems IRIS](https://www.intersystems.com/products/intersystems-iris/) — runtime and ObjectScript entry point
- [IRIS Embedded Python](https://docs.intersystems.com/irislatest/csp/docbook/DocBook.UI.Page.cls?KEY=AEPYTHON) — bridge between ObjectScript and Python
- [polars](https://pola.rs/) — vectorized DataFrame library (Rust-backed) for CSV parsing and filtering
- [concurrent.futures.ThreadPoolExecutor](https://docs.python.org/3/library/concurrent.futures.html) — parallel file processing

---

# Finding Variable Stars with IRIS Embedded Python and Polars

## Understanding the Data

The files are in ECSV 1.0 format (Enhanced CSV, used by the Gaia archive). Each file begins with 365 comment lines (`#`) describing the schema, followed by a column-name header, then data rows starting at line 367.

The key columns are:
- Column 1: `source_id` — scalar integer
- Column 11: `bp_flux` — quoted array of floats with nulls, e.g. `"[1820.8,null,2013.8,...]"`
- Column 16: `rp_flux` — same structure

For each source the task is:
1. Extract all finite flux values from both arrays
2. Find min and max per band
3. Compute `percentage_change = max((bp_max−bp_min)/bp_min, (rp_max−rp_min)/rp_min) × 100`
4. Output the source if `percentage_change > 100`

---

## Using IRIS Embedded Python

IRIS 2021.2+ ships with an embedded CPython interpreter accessible from ObjectScript via `%SYS.Python`. The entry point stays in ObjectScript (the evaluator runs `do ^RunScript`), but the heavy lifting moves to Python:

```objectscript
Set sys = ##class(%SYS.Python).Import("sys")
Do sys.path."append"("/home/irisowner/dev/src")
Set proc = ##class(%SYS.Python).Import("process")
Do proc.run()
```

This keeps the interface clean, one ObjectScript routine, one Python module, nothing else.

---

## The Initial Python Solution

The first working Python implementation used stdlib `csv.reader` and `math.isfinite`, no dependencies:

```python
def _flux_stats(raw):
    lo = hi = None
    for v in raw[1:-1].split(','):
        if v == 'null': continue
        f = float(v)
        if not math.isfinite(f): continue
        if lo is None: lo = hi = f
        elif f < lo: lo = f
        elif f > hi: hi = f
    return lo, hi
```

And `ThreadPoolExecutor` to process all 20 files concurrently. This worked, but ran at about **14 seconds**.

---

## Profiling: Finding the Real Bottleneck

Before optimizing blindly, we added a `benchmark()` function to time each phase on a single file:

```
read_csv:       0.386s  (5345 rows)   ← 94% of time
parse+minmax:   0.021s
filter:         0.002s
pct_change:     0.001s
--- single file total: 0.410s
```

This was the key insight: **94% of time is reading and decompressing the CSV**. The array parsing and math are essentially free. Any optimization that targets the parsing logic will have minimal impact.

The solution had to address I/O and decompression, not computation.

---

## Enter Polars

[Polars](https://pola.rs/) is a DataFrame library written in Rust. It reads gzip-compressed CSV natively (no separate decompression step), uses Rust's memory model to avoid Python's GIL during I/O, and supports column projection — meaning it can skip 45 of the 48 columns without reading them at all.

Switching from stdlib `csv.reader` to `polars.read_csv` with `columns=[1, 11, 16]`:

```python
df = pl.read_csv(gz_path, comment_prefix="#", columns=[1, 11, 16],
                 new_columns=["source_id", "bp_flux", "rp_flux"],
                 infer_schema_length=0)
```

Combined with `ThreadPoolExecutor(max_workers=20)` — one thread per file, this brought the time down to **~2.5 seconds**.

One important note on polars and the Python GIL: polars releases the GIL during its Rust I/O and computation phases, so multiple threads running `read_csv` concurrently do achieve real CPU parallelism, not just I/O overlap.

---

## Parsing the Flux Arrays

The flux columns arrive as strings like `"[1820.8,null,2013.8,...]"`. Polars' `list.eval` provides a vectorized way to parse them:

```python
def _band_stats(series):
    return (
        series
        .str.strip_chars("[]")
        .str.split(",")
        .list.eval(
            pl.element()
            .filter(pl.element() != "null")
            .cast(pl.Float64, strict=False)
            .drop_nulls()
        )
    )
```

This runs entirely in Rust — no Python loop over rows.

---

## The Filter Optimization

The naive approach computes `percentage_change` for every source and then filters. But `percentage_change > 100` is mathematically equivalent to `max_flux >= min_flux * 2`. The multiply-and-compare avoids the division entirely and runs before the percentage calculation:

```python
df = df.filter(
    (pl.col("bp_min_flux") > 0) & (pl.col("rp_min_flux") > 0) &
    (
        (pl.col("bp_max_flux") >= pl.col("bp_min_flux") * 2) |
        (pl.col("rp_max_flux") >= pl.col("rp_min_flux") * 2)
    )
)
```

This eliminates most rows before the more expensive percentage computation runs on the survivors.

---

## Eight Versions, One Winner

Reaching the final result took systematic experimentation. Eight approaches were benchmarked using an alternating 10-run benchmark (running V_a then V_b back-to-back each round to neutralize OS resource differences):

| Version | Approach | Avg time |
|---|---|---|
| V1 | Parquet cache, but `prepare()` inside timer | ~2.3s |
| **V2** | Direct `.csv.gz`, `ThreadPoolExecutor(20)`, polars | **1.71s** |
| V3 | Gunzip → CSV → parquet inside timer | ~5.0s |
| V4 | `pl.scan_csv` lazy glob (Rust Rayon pool) | ~4.3s |
| V5 | Gunzip before timer, read plain CSV inside timer | ~5.78s |
| V6 | V2 + regex `str.extract_all` for null-free parsing | ~1.9s |
| V7 | V2 + V6 combined, single `with_columns` pass | ~1.90s |
| V8 | V2 + streaming file append (no `pl.concat`) | ~1.91s |

### What the experiments revealed

**V3 and V1** confirmed that writing parquet as an intermediate format, while theoretically faster to read back, adds enough overhead inside the timer to be net negative.

**V4** tested whether polars' own internal Rust thread pool (`pl.scan_csv` with a glob) would outperform Python's `ThreadPoolExecutor`. It did not — for 20 independent gzip files, the Python-managed one-thread-per-file approach saturated I/O more effectively than polars' internal scheduler.

**V5** was the most counterintuitive result: gunzipping the files before the timer (removing decompression entirely from the timed section) made things *slower*. The reason: uncompressed files are ~5–10× larger on disk. Reading more bytes from disk was more expensive than the decompression that polars' Rust engine performs in parallel with I/O.

**V6 and V7** tested whether replacing `str.split` + `list.eval` with a regex `str.extract_all` would be faster (regex skips nulls automatically, no `filter` step needed). At ~16 elements per array, the regex engine's overhead exceeded what was saved.

**V8** tested whether eliminating the final `pl.concat(frames).write_csv(...)` — which allocates one large combined frame in memory — would help. Writing each frame's CSV output directly to the file in append mode was slower: 20 file open/close operations cost more than the single in-memory concat.

**V2 won** with consistent ~1.71s across all benchmark rounds.

---

## Final Architecture

```
do ^RunScript
    │
    ├── ##class(%File).CreateDirectoryChain(data/out)
    ├── sys.path.append(/src)
    ├── ── start timer ──────────────────────────────────────────
    │
    ├── process.run()
    │       ├── glob(data/in/*.csv.gz)[:20]
    │       ├── ThreadPoolExecutor(workers=20)
    │       │       └── _process_gz(file) × 20 parallel
    │       │               ├── pl.read_csv(gz, columns=[1,11,16])
    │       │               ├── _band_stats(bp_flux) → min, max
    │       │               ├── _band_stats(rp_flux) → min, max
    │       │               ├── filter: min>0 and max>=min*2
    │       │               └── select + compute percentage_change
    │       ├── pl.concat(frames)
    │       └── write_csv(data/out/results.csv)
    │
    └── ── stop timer ── print elapsed ──────────────────────────
```

Two files. No intermediate storage. No pre-processing step. The entire pipeline from compressed input to CSV output runs in ~1.71 seconds.

---

## Lessons Learned

**Profile before optimizing.** The first instinct was to optimize the array parsing logic — that was 2% of the time. The real bottleneck was always the read.

**Counterintuitive I/O.** Decompression is not always more expensive than raw bytes. Polars' Rust-based gzip reader pipelines decompression with disk reads efficiently enough that the compressed representation is faster end-to-end than the uncompressed one.

**The GIL is not always the enemy.** `ThreadPoolExecutor` is often dismissed for CPU-bound Python work because of the GIL. But when the underlying library (polars) releases the GIL during its Rust operations, threads achieve real parallelism — and without the process-spawn overhead of `multiprocessing`, which carries additional risk when spawned from within IRIS's embedded Python environment.
