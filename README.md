# Gaia DR3 EpochPhotometry Analyzer

Identifies variable astronomical objects in Gaia DR3 epoch photometry data using IRIS Embedded Python and [polars](https://pola.rs/) for parallel gzip CSV processing. Built for [InterSystems Employee Programming Challenge #1](https://openexchange.intersystems.com/contest/47).

## Challenge

Process the first 20 files from the Gaia DR3 epoch photometry archive and identify all astronomical sources whose BP or RP photometric flux changed by more than 100% over the observation period.

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
