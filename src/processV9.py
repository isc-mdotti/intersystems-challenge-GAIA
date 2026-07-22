import os
os.environ["POLARS_MAX_THREADS"] = "1"

import glob
from concurrent.futures import ThreadPoolExecutor
import polars as pl

IN  = "/home/irisowner/dev/data/in"
OUT = "/home/irisowner/dev/data/out"

SKIP_ROWS = 365  # leading '#' comment lines in every Gaia DR3 EpochPhotometry file


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


def _process_gz(gz_path):
    df = pl.read_csv(gz_path, skip_rows=SKIP_ROWS, has_header=True,
                     columns=[1, 11, 16],
                     new_columns=["source_id", "bp_flux", "rp_flux"],
                     infer_schema_length=0)
    bp = _band_stats(df["bp_flux"])
    rp = _band_stats(df["rp_flux"])
    df = df.with_columns([
        bp.list.min().alias("bp_min_flux"),
        bp.list.max().alias("bp_max_flux"),
        rp.list.min().alias("rp_min_flux"),
        rp.list.max().alias("rp_max_flux"),
    ])
    df = df.filter(
        (pl.col("bp_min_flux") > 0) & (pl.col("rp_min_flux") > 0) &
        (
            (pl.col("bp_max_flux") >= pl.col("bp_min_flux") * 2) |
            (pl.col("rp_max_flux") >= pl.col("rp_min_flux") * 2)
        )
    )
    return df.select(
        pl.col("source_id"),
        pl.col("bp_min_flux"), pl.col("bp_max_flux"),
        pl.col("rp_min_flux"), pl.col("rp_max_flux"),
        (pl.max_horizontal(
            (pl.col("bp_max_flux") - pl.col("bp_min_flux")) / pl.col("bp_min_flux"),
            (pl.col("rp_max_flux") - pl.col("rp_min_flux")) / pl.col("rp_min_flux"),
        ) * 100).alias("percentage_change")
    )


def run():
    files = sorted(glob.glob(os.path.join(IN, "*.csv.gz")), key=os.path.getsize, reverse=True)[:20]
    max_workers = min(len(files), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        frames = list(ex.map(_process_gz, files))
    pl.concat([f for f in frames if len(f) > 0]).write_csv(
        os.path.join(OUT, "results.csv")
    )
