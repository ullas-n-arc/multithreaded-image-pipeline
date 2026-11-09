#!/usr/bin/env python3
"""Measure the real CLI, checking every result against a sequential SHA256."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shlex
import statistics
import subprocess
import sys
import tempfile
import time

if __package__:
    from .generate_ppm import MAX_PIXELS, PATTERN_VERSION, generate_ppm, positive_int
else:
    from generate_ppm import MAX_PIXELS, PATTERN_VERSION, generate_ppm, positive_int


MEASUREMENT_SEED = 20261109
FILTER_NAMES = {"grayscale", "blur", "sharpen", "edge"}
TIMING_FIELDS = ("load_ms", "setup_ms", "process_ms", "shutdown_ms", "save_ms", "total_ms")
STATS_FIELDS = {
    "mode", "width", "height", "filters", "threads", "queue_capacity", "tile_rows",
    *TIMING_FIELDS, "stage_ms",
}
CSV_FIELDS = (
    "width", "height", "pixels", "mode", "threads", "queue_capacity", "tile_rows",
    "filters", "repeats", "warmup", "process_ms_median", "process_ms_min",
    "process_ms_max", "total_ms_median", "total_ms_min", "total_ms_max",
    "speedup", "efficiency", "output_sha256",
)


def comma_integers(value: str) -> list:
    parts = value.split(",")
    if not parts or any(not part.strip() for part in parts):
        raise argparse.ArgumentTypeError("expected a comma-separated list of positive integers")
    values = [positive_int(part.strip()) for part in parts]
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("duplicate list entries are not allowed")
    return values


def filter_list(value: str) -> list:
    filters = value.split(",")
    if not 1 <= len(filters) <= 16 or any(name not in FILTER_NAMES for name in filters):
        raise argparse.ArgumentTypeError(
            "expected 1..16 filters from grayscale,blur,sharpen,edge (no empty entries)"
        )
    return filters


def positive_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return seconds


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment_information() -> dict:
    cpu_model = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"model name", "Hardware"}:
                cpu_model = value.strip()
                break
    except OSError:
        pass

    compiler_command = shlex.split(os.environ.get("CC", "cc")) + ["--version"]
    compiler_version = None
    try:
        result = subprocess.run(compiler_command, capture_output=True, text=True,
                                timeout=10, check=False)
        if result.returncode == 0:
            compiler_version = (result.stdout or result.stderr).strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "kernel": {"system": platform.system(), "release": platform.release(),
                   "version": platform.version()},
        "cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
        "compiler_command": compiler_command,
        "compiler_version": compiler_version,
        "python_version": platform.python_version(),
        "working_directory": str(Path.cwd()),
    }


def validate_stats(stats: object, size: int, mode: str, threads: int, arguments) -> dict:
    if not isinstance(stats, dict) or set(stats) != STATS_FIELDS:
        raise RuntimeError("--stats did not return the required single JSON object/schema")
    expected = {
        "mode": mode, "width": size, "height": size, "filters": arguments.filters,
        "threads": 1 if mode == "sequential" else threads,
        "queue_capacity": 0 if mode == "sequential" else arguments.queue_capacity,
        "tile_rows": arguments.tile_rows,
    }
    for name, value in expected.items():
        if stats[name] != value or type(stats[name]) is not type(value):
            raise RuntimeError(f"unexpected stats {name}: {stats[name]!r}, expected {value!r}")
    if not isinstance(stats["stage_ms"], list) or len(stats["stage_ms"]) != len(arguments.filters):
        raise RuntimeError("stage_ms is not aligned with the filter chain")
    for value in [stats[name] for name in TIMING_FIELDS] + stats["stage_ms"]:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise RuntimeError(f"invalid timing in stats: {value!r}")
    return stats


def run_sample(binary: Path, source: Path, output: Path, size: int, mode: str,
               threads: int, arguments, phase: str, repeat: int,
               expected_hash: str, raw_samples: list) -> dict:
    output.unlink(missing_ok=True)
    command = [
        str(binary), "--input", str(source), "--output", str(output),
        "--filters", ",".join(arguments.filters), "--mode", mode,
        "--threads", str(threads), "--queue-capacity", str(arguments.queue_capacity),
        "--tile-rows", str(arguments.tile_rows), "--stats",
    ]
    started = time.perf_counter_ns()
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=arguments.timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"{size}x{size} {mode}/{threads}: subprocess exceeded {arguments.timeout:g}s"
        ) from error
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000
    if result.returncode != 0:
        raise RuntimeError(
            f"{size}x{size} {mode}/{threads}: exit {result.returncode}\n"
            f"command: {shlex.join(command)}\n{result.stderr.strip()}"
        )
    try:
        stats = validate_stats(json.loads(result.stdout), size, mode, threads, arguments)
    except (ValueError, RuntimeError) as error:
        raise RuntimeError(f"{size}x{size} {mode}/{threads}: {error}") from error
    if not output.is_file():
        raise RuntimeError(f"{size}x{size} {mode}/{threads}: no output was published")
    output_hash = sha256_file(output)
    if expected_hash is not None and output_hash != expected_hash:
        raise RuntimeError(
            f"SHA256 mismatch for {phase} {size}x{size} {mode}/{threads}, repeat {repeat}:\n"
            f"expected {expected_hash}\nactual   {output_hash}"
        )
    sample = {
        "run_index": len(raw_samples) + 1, "phase": phase, "repeat": repeat,
        "width": size, "height": size, "mode": mode, "threads": threads,
        "command": command, "output_sha256": output_hash, "wall_ms": wall_ms,
        "stats": stats,
    }
    raw_samples.append(sample)
    return sample


def summarize(size: int, modes: list, samples: list, arguments, output_hash: str) -> list:
    grouped = {
        mode: [sample["stats"] for sample in samples
               if (sample["mode"], sample["threads"]) == mode]
        for mode in modes
    }
    sequential_median = statistics.median(
        stats["process_ms"] for stats in grouped[("sequential", 1)]
    )
    rows = []
    for mode, threads in modes:
        timings = grouped[(mode, threads)]
        processing = [stats["process_ms"] for stats in timings]
        total = [stats["total_ms"] for stats in timings]
        median = statistics.median(processing)
        # Rounded-zero timings cannot support a defensible speedup ratio.
        speedup = sequential_median / median if sequential_median > 0 and median > 0 else None
        rows.append({
            "width": size, "height": size, "pixels": size * size,
            "mode": mode, "threads": threads,
            "queue_capacity": 0 if mode == "sequential" else arguments.queue_capacity,
            "tile_rows": arguments.tile_rows, "filters": ",".join(arguments.filters),
            "repeats": len(timings), "warmup": arguments.warmup,
            "process_ms_median": median, "process_ms_min": min(processing),
            "process_ms_max": max(processing), "total_ms_median": statistics.median(total),
            "total_ms_min": min(total), "total_ms_max": max(total), "speedup": speedup,
            "efficiency": speedup / threads if speedup is not None else None,
            "output_sha256": output_hash,
        })
    return rows


def write_results(output: Path, metadata_path: Path, rows: list, metadata: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=output.parent, prefix=".imgpipe-csv-",
                                         delete=False) as stream:
            csv_temporary = Path(stream.name)
            pending.append(csv_temporary)
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: f"{value:.6f}" if isinstance(value, float) else value
                                 for key, value in row.items()})
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=output.parent, prefix=".imgpipe-json-",
                                         delete=False) as stream:
            json_temporary = Path(stream.name)
            pending.append(json_temporary)
            json.dump(metadata, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(json_temporary, metadata_path)
        os.replace(csv_temporary, output)
    finally:
        for path in pending:
            path.unlink(missing_ok=True)


def print_table(rows: list) -> None:
    print("       Size  Mode        Thr     Process ms (med/min/max)"
          "          Total ms (med/min/max)   Speedup  Efficiency")
    print("-" * 115)
    for row in rows:
        size = f"{row['width']}x{row['height']}"
        processing = "/".join(f"{row['process_ms_' + field]:.3f}"
                              for field in ("median", "min", "max"))
        total = "/".join(f"{row['total_ms_' + field]:.3f}"
                         for field in ("median", "min", "max"))
        speedup = f"{row['speedup']:.2f}x" if row["speedup"] is not None else "n/a"
        efficiency = f"{100 * row['efficiency']:.1f}%" if row["efficiency"] is not None else "n/a"
        print(f"{size:>11}  {row['mode']:<10}  {row['threads']:>3}  "
              f"{processing:>28}  {total:>30}  {speedup:>8}  {efficiency:>10}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path,
                        default=Path(__file__).resolve().parents[1] / "bin" / "imgpipe")
    parser.add_argument("--sizes", type=comma_integers, default="256,512,1024,2048")
    parser.add_argument("--threads", type=comma_integers, default="1,2,4,8")
    parser.add_argument("--repeats", type=positive_int, default=5)
    parser.add_argument("--warmup", type=positive_int, default=1)
    parser.add_argument("--filters", type=filter_list, default="grayscale,blur,sharpen")
    parser.add_argument("--queue-capacity", type=positive_int, default=64)
    parser.add_argument("--tile-rows", type=positive_int, default=32)
    parser.add_argument("--timeout", type=positive_timeout, default=120.0,
                        help="maximum seconds per image-processing subprocess (default: 120)")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results.csv"))
    arguments = parser.parse_args()
    if any(size * size > MAX_PIXELS for size in arguments.sizes):
        parser.error(f"square sizes must be 1..{math.isqrt(MAX_PIXELS)}")
    if any(threads > 256 for threads in arguments.threads):
        parser.error("thread counts must be 1..256")
    if arguments.queue_capacity > 65536:
        parser.error("queue capacity must be 1..65536")
    if arguments.tile_rows > 1048576:
        parser.error("tile rows must be 1..1048576")
    binary = arguments.binary.expanduser().resolve()
    if not binary.is_file() or (os.name == "posix" and not os.access(binary, os.X_OK)):
        parser.error(f"binary is not an executable file: {binary}")
    output = arguments.output.expanduser().resolve()
    metadata_path = output.with_suffix(".environment.json")
    if output == binary or metadata_path == binary:
        parser.error("results must not overwrite the benchmark binary")

    try:
        binary_hash = sha256_file(binary)
        metadata = environment_information()
        metadata.update({
            "schema_version": 1,
            "binary": {"path": str(binary), "sha256": binary_hash},
            "configuration": {
                "sizes": arguments.sizes, "threads": arguments.threads,
                "repeats": arguments.repeats, "warmup": arguments.warmup,
                "filters": arguments.filters, "queue_capacity": arguments.queue_capacity,
                "tile_rows": arguments.tile_rows, "timeout_seconds": arguments.timeout,
                "measurement_seed": MEASUREMENT_SEED,
                "order": "separate warmup rounds; shuffled/interleaved measured rounds per size",
            },
            "input_pattern": PATTERN_VERSION, "inputs": [], "raw_samples": [],
        })
        rows = []
        modes = [("sequential", 1)] + [("parallel", threads) for threads in arguments.threads]
        with tempfile.TemporaryDirectory(prefix="imgpipe-benchmark-") as directory:
            temporary = Path(directory)
            for size in arguments.sizes:
                source = temporary / f"input-{size}.ppm"
                result_path = temporary / f"output-{size}.ppm"
                generate_ppm(source, size, size)
                metadata["inputs"].append({
                    "width": size, "height": size, "sha256": sha256_file(source),
                })
                baseline = run_sample(binary, source, result_path, size, "sequential", 1,
                                      arguments, "baseline", 0, None, metadata["raw_samples"])
                reference_hash = baseline["output_sha256"]
                warmup_rng = random.Random(MEASUREMENT_SEED ^ size ^ 0x5741524D)
                for repeat in range(1, arguments.warmup + 1):
                    order = list(modes)
                    warmup_rng.shuffle(order)
                    for mode, threads in order:
                        run_sample(binary, source, result_path, size, mode, threads,
                                   arguments, "warmup", repeat, reference_hash, metadata["raw_samples"])
                measured = []
                measurement_rng = random.Random(MEASUREMENT_SEED ^ size)
                for repeat in range(1, arguments.repeats + 1):
                    order = list(modes)
                    measurement_rng.shuffle(order)
                    for mode, threads in order:
                        measured.append(run_sample(
                            binary, source, result_path, size, mode, threads, arguments,
                            "measured", repeat, reference_hash, metadata["raw_samples"],
                        ))
                rows.extend(summarize(size, modes, measured, arguments, reference_hash))
        if sha256_file(binary) != binary_hash:
            raise RuntimeError("binary changed during benchmarking; results were not published")
        metadata["completed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        metadata["summary"] = rows
        write_results(output, metadata_path, rows, metadata)
        print_table(rows)
        print(f"CSV: {output}")
        print(f"Environment and raw samples: {metadata_path}")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
