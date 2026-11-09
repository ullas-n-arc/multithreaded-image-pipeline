#!/usr/bin/env python3
"""Black-box CLI tests with independent integer filter math and no dependencies."""

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import random
import runpy
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


BINARY = Path(__file__).resolve().parents[1] / "bin" / "imgpipe"
TOOLS = Path(__file__).resolve().parents[1] / "tools"
SUBPROCESS_TIMEOUT = 30
WHITESPACE = b" \t\r\n\v\f"
TIMING_FIELDS = ("load_ms", "setup_ms", "process_ms", "shutdown_ms", "save_ms", "total_ms")
STATS_FIELDS = {
    "mode", "width", "height", "filters", "threads", "queue_capacity", "tile_rows",
    *TIMING_FIELDS, "stage_ms",
}
KERNELS = {
    "blur": ((1, 1, 1), (1, 1, 1), (1, 1, 1)),
    "sharpen": ((0, -1, 0), (-1, 5, -1), (0, -1, 0)),
    "edge": ((-1, -1, -1), (-1, 8, -1), (-1, -1, -1)),
}


def encode_ppm(width: int, height: int, pixels: bytes,
               magic: str = "P6", maxval: int = 255) -> bytes:
    header = f"{magic}\n{width} {height}\n{maxval}\n".encode("ascii")
    if magic == "P6":
        return header + pixels
    return header + " ".join(str(sample) for sample in pixels).encode("ascii") + b"\n"


def decode_output(data: bytes) -> tuple:
    """Read an output P6 header without skipping any raster whitespace/# bytes."""
    position = 0
    tokens = []
    for _ in range(4):
        while position < len(data):
            if data[position] in WHITESPACE:
                position += 1
            elif data[position] == ord("#"):
                while position < len(data) and data[position] not in b"\r\n":
                    position += 1
            else:
                break
        start = position
        while (position < len(data) and data[position] not in WHITESPACE
               and data[position] != ord("#")):
            position += 1
        tokens.append(data[start:position])
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise ValueError(f"output must be P6 with maxval 255, got {tokens!r}")
    width, height = int(tokens[1]), int(tokens[2])
    if width <= 0 or height <= 0 or position >= len(data) or data[position] not in WHITESPACE:
        raise ValueError("invalid output dimensions/header delimiter")
    position += 2 if data[position:position + 2] == b"\r\n" else 1
    pixels = data[position:]
    if len(pixels) != width * height * 3:
        raise ValueError(f"output raster has {len(pixels)} bytes, expected {width * height * 3}")
    return width, height, pixels


def reference_filter(width: int, height: int, pixels: bytes, name: str) -> bytes:
    result = bytearray(len(pixels))
    if name == "grayscale":
        for offset in range(0, len(pixels), 3):
            red, green, blue = pixels[offset:offset + 3]
            gray = (299 * red + 587 * green + 114 * blue + 500) // 1000
            result[offset:offset + 3] = bytes((gray, gray, gray))
        return bytes(result)

    kernel = KERNELS[name]
    for y in range(height):
        for x in range(width):
            for channel in range(3):
                total = 0
                for kernel_y in range(3):
                    source_y = min(height - 1, max(0, y + kernel_y - 1))
                    for kernel_x in range(3):
                        source_x = min(width - 1, max(0, x + kernel_x - 1))
                        sample = pixels[(source_y * width + source_x) * 3 + channel]
                        total += sample * kernel[kernel_y][kernel_x]
                value = (total + 4) // 9 if name == "blur" else total
                result[(y * width + x) * 3 + channel] = min(255, max(0, value))
    return bytes(result)


def reference_pipeline(width: int, height: int, pixels: bytes, filters) -> bytes:
    for name in filters:
        pixels = reference_filter(width, height, pixels, name)
    return pixels


class ImagePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="imgpipe tests with spaces ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "input image.ppm"
        self.output = self.directory / "output image.ppm"

    def invoke(self, arguments) -> subprocess.CompletedProcess:
        return subprocess.run([str(BINARY), *(str(argument) for argument in arguments)],
                              cwd=self.directory, capture_output=True, check=False,
                              timeout=SUBPROCESS_TIMEOUT)

    def invoke_tool(self, name: str, arguments, timeout: int = SUBPROCESS_TIMEOUT):
        return subprocess.run([sys.executable, "-B", str(TOOLS / name),
                               *(str(argument) for argument in arguments)],
                              cwd=self.directory, capture_output=True, check=False, timeout=timeout)

    def assert_stats(self, result, width: int, height: int, filters, mode: str,
                     threads: int, capacity: int, tile_rows: int) -> dict:
        stats = json.loads(result.stdout)
        self.assertIsInstance(stats, dict)
        self.assertEqual(set(stats), STATS_FIELDS)
        expected = {
            "mode": mode, "width": width, "height": height, "filters": list(filters),
            "threads": 1 if mode == "sequential" else threads,
            "queue_capacity": 0 if mode == "sequential" else capacity,
            "tile_rows": tile_rows,
        }
        for name, value in expected.items():
            self.assertIs(type(stats[name]), type(value), name)
            self.assertEqual(stats[name], value, name)
        self.assertIsInstance(stats["stage_ms"], list)
        self.assertEqual(len(stats["stage_ms"]), len(filters))
        for value in [stats[name] for name in TIMING_FIELDS] + stats["stage_ms"]:
            self.assertIn(type(value), (int, float))
            self.assertTrue(math.isfinite(value))
            self.assertGreaterEqual(value, 0)
        self.assertGreaterEqual(stats["total_ms"] + 0.1, stats["process_ms"])
        return stats

    def run_pipeline(self, width: int, height: int, pixels: bytes, filters,
                     mode: str = "parallel", threads: int = 3,
                     capacity: int = 1, tile_rows: int = 2, raw: bytes = None) -> tuple:
        self.source.write_bytes(raw if raw is not None else encode_ppm(width, height, pixels))
        result = self.invoke([
            "--input", self.source, "--output", self.output,
            "--filters", ",".join(filters), "--mode", mode, "--threads", threads,
            "--queue-capacity", capacity, "--tile-rows", tile_rows, "--stats",
        ])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        output_width, output_height, actual = decode_output(self.output.read_bytes())
        self.assertEqual((output_width, output_height), (width, height))
        self.assertEqual(actual, reference_pipeline(width, height, pixels, filters))
        stats = self.assert_stats(result, width, height, filters, mode, threads, capacity, tile_rows)
        self.assertEqual(set(self.directory.iterdir()), {self.source, self.output},
                         "successful run left a temporary output behind")
        return actual, stats

    def assert_failure_preserves_output(self, arguments, code: int) -> None:
        sentinel = b"existing destination must survive failure\x00\xff\n"
        self.output.write_bytes(sentinel)
        before = set(self.directory.iterdir())
        result = self.invoke(arguments)
        self.assertEqual(result.returncode, code, result.stderr.decode(errors="replace"))
        self.assertFalse(result.stdout.lstrip().startswith(b"{"), "failure emitted stats")
        self.assertEqual(self.output.read_bytes(), sentinel)
        self.assertEqual(set(self.directory.iterdir()), before, "failure leaked a temporary file")

    def test_help(self) -> None:
        for option in ("-h", "--help"):
            with self.subTest(option=option):
                result = self.invoke([option])
                self.assertEqual(result.returncode, 0)
                self.assertIn(b"--input", result.stdout)
                self.assertIn(b"--filters", result.stdout)

    def test_known_grayscale_rounding(self) -> None:
        pixels = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255,
                        1, 0, 0, 0, 1, 0, 0, 0, 1, 255, 255, 255))
        expected = bytes(value for gray in (76, 150, 29, 0, 1, 0, 255)
                         for value in (gray, gray, gray))
        for mode in ("sequential", "parallel"):
            with self.subTest(mode=mode):
                actual, _ = self.run_pipeline(7, 1, pixels, ["grayscale"], mode=mode)
                self.assertEqual(actual, expected)

    def test_known_convolution_borders_and_clamping(self) -> None:
        pixels = bytes(value for sample in (0, 90, 255) for value in (sample,) * 3)
        golden = {"blur": (30, 115, 200), "sharpen": (0, 15, 255), "edge": (0, 0, 255)}
        for name, levels in golden.items():
            for mode in ("sequential", "parallel"):
                with self.subTest(filter=name, mode=mode):
                    actual, _ = self.run_pipeline(3, 1, pixels, [name], mode=mode)
                    self.assertEqual(actual, bytes(value for level in levels for value in (level,) * 3))

    def test_one_pixel_convolutions(self) -> None:
        pixel = bytes((11, 200, 255))
        for name in KERNELS:
            for mode in ("sequential", "parallel"):
                with self.subTest(filter=name, mode=mode):
                    actual, _ = self.run_pipeline(1, 1, pixel, [name], mode=mode)
                    self.assertEqual(actual, bytes(3) if name == "edge" else pixel)

    def test_known_two_dimensional_filter_chain(self) -> None:
        pixels = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255))
        for filters, levels in ((["grayscale"], (76, 150, 29, 255)),
                                (["grayscale", "blur"], (102, 143, 103, 162))):
            for mode in ("sequential", "parallel"):
                with self.subTest(filters=filters, mode=mode):
                    actual, _ = self.run_pipeline(2, 2, pixels, filters, mode=mode,
                                                  raw=encode_ppm(2, 2, pixels, magic="P3"))
                    self.assertEqual(actual, bytes(value for level in levels for value in (level,) * 3))

    def test_random_odd_tiny_images_and_full_chains(self) -> None:
        rng = random.Random(20261109)
        chains = [[name] for name in ("grayscale", "blur", "sharpen", "edge")]
        chains += [
            ["grayscale", "blur", "sharpen", "edge"],
            ["edge", "sharpen", "blur", "grayscale"],
            ["blur", "blur", "sharpen"],
            ["sharpen", "edge", "sharpen"],
        ]
        for width, height in ((1, 1), (1, 7), (9, 1), (2, 3), (5, 7), (17, 11)):
            pixels = bytes(rng.randrange(256) for _ in range(width * height * 3))
            for filters in chains:
                for mode in ("sequential", "parallel"):
                    with self.subTest(width=width, height=height, filters=filters, mode=mode):
                        self.run_pipeline(width, height, pixels, filters, mode=mode)

    def test_parallel_thread_queue_and_tile_combinations(self) -> None:
        width, height = 23, 19
        rng = random.Random(42)
        pixels = bytes(rng.randrange(256) for _ in range(width * height * 3))
        filters = ["grayscale", "blur", "sharpen", "edge", "blur"]
        configurations = ((1, 1, 1), (2, 1, 3), (7, 2, 4), (32, 1, 1),
                          (4, 5, 19), (3, 64, 20), (2, 1, 1048576))
        for threads, capacity, tile_rows in configurations:
            with self.subTest(threads=threads, capacity=capacity, tile_rows=tile_rows):
                self.run_pipeline(width, height, pixels, filters, threads=threads,
                                  capacity=capacity, tile_rows=tile_rows)

    def test_maximum_filter_chain_and_numeric_boundaries(self) -> None:
        pixels = bytes(range(45))
        filters = ["grayscale", "blur"] * 8
        for mode in ("sequential", "parallel"):
            with self.subTest(mode=mode):
                self.run_pipeline(5, 3, pixels, filters, mode=mode)
        self.run_pipeline(5, 3, pixels, ["blur"], mode="sequential",
                          threads=256, capacity=65536, tile_rows=1048576)

    def test_short_options(self) -> None:
        pixels = bytes(range(18))
        self.source.write_bytes(encode_ppm(3, 2, pixels))
        result = self.invoke(["-i", self.source, "-o", self.output, "-f", "edge,blur",
                              "-m", "parallel", "-t", "3", "-q", "1", "-r", "2", "--stats"])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(decode_output(self.output.read_bytes()),
                         (3, 2, reference_pipeline(3, 2, pixels, ["edge", "blur"])))
        self.assert_stats(result, 3, 2, ["edge", "blur"], "parallel", 3, 1, 2)

    def test_defaults(self) -> None:
        pixels = bytes(range(18))
        self.source.write_bytes(encode_ppm(3, 2, pixels))
        result = self.invoke(["--input", self.source, "--output", self.output, "--stats"])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        online_cpus = (os.sysconf("SC_NPROCESSORS_ONLN") if hasattr(os, "sysconf")
                       else os.cpu_count()) or 1
        self.assert_stats(result, 3, 2, ["grayscale", "blur"], "parallel",
                          min(online_cpus, 32), 64, 32)
        self.assertEqual(decode_output(self.output.read_bytes()),
                         (3, 2, reference_pipeline(3, 2, pixels, ["grayscale", "blur"])))

    def test_success_without_stats(self) -> None:
        pixels = bytes((25, 40, 90))
        self.source.write_bytes(encode_ppm(1, 1, pixels))
        result = self.invoke(["-i", self.source, "-o", self.output, "-f", "sharpen", "-m", "sequential"])
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertFalse(result.stdout.lstrip().startswith(b"{"))
        self.assertEqual(decode_output(self.output.read_bytes()), (1, 1, pixels))

    @unittest.skipUnless(Path("/dev/full").exists(), "requires /dev/full")
    def test_stdout_write_failure_is_reported(self) -> None:
        pixels = bytes((25, 40, 90))
        self.source.write_bytes(encode_ppm(1, 1, pixels))
        for arguments in (["--help"], ["-i", self.source, "-o", self.output,
                                       "-f", "sharpen", "-m", "sequential", "--stats"]):
            with self.subTest(arguments=arguments), Path("/dev/full").open("wb") as full:
                result = subprocess.run(
                    [str(BINARY), *(str(argument) for argument in arguments)],
                    cwd=self.directory, stdout=full, stderr=subprocess.PIPE,
                    check=False, timeout=SUBPROCESS_TIMEOUT,
                )
                self.assertEqual(result.returncode, 1, result.stderr.decode(errors="replace"))
                self.assertIn(b"stdout", result.stderr)
        # Statistics are emitted after publication; their failure cannot undo it.
        self.assertEqual(decode_output(self.output.read_bytes()), (1, 1, pixels))
        self.assertEqual(set(self.directory.iterdir()), {self.source, self.output})

    def test_p3_comments_whitespace_and_normalization(self) -> None:
        for maxval in (1, 2, 7, 31, 254, 255):
            samples = bytes((index * 7 + 3) % (maxval + 1) for index in range(18))
            normalized = bytes((sample * 255 + maxval // 2) // maxval for sample in samples)
            raw = (b"P3\n# before dimensions\n3 \t# width\n2\v\f# before maxval\n"
                   + str(maxval).encode("ascii") + b"\r\n# before samples\n"
                   + b" \n# sample comment\n".join(str(sample).encode("ascii") for sample in samples)
                   + b" \n# trailing comments allowed\n# final comment without newline")
            for mode in ("sequential", "parallel"):
                with self.subTest(maxval=maxval, mode=mode):
                    self.run_pipeline(3, 2, normalized, ["blur", "grayscale"], mode=mode, raw=raw)

    def test_p6_normalization(self) -> None:
        for maxval in (1, 2, 7, 31, 254, 255):
            samples = bytes((index * 7 + 3) % (maxval + 1) for index in range(18))
            normalized = bytes((sample * 255 + maxval // 2) // maxval for sample in samples)
            with self.subTest(maxval=maxval):
                self.run_pipeline(3, 2, normalized, ["grayscale", "blur"],
                                  raw=encode_ppm(3, 2, samples, maxval=maxval))

    def test_cr_terminated_comments(self) -> None:
        pixel = bytes((35, 20, 30))
        for magic in ("P3", "P6"):
            raster = b"35 20 30\r# trailing comment\r" if magic == "P3" else pixel
            raw = (magic.encode("ascii") + b"\r# dimensions\r1 # width\r1\r# maxval\r255\r"
                   + raster)
            for mode in ("sequential", "parallel"):
                with self.subTest(magic=magic, mode=mode):
                    self.run_pipeline(1, 1, pixel, ["sharpen"], mode=mode, raw=raw)

    def test_p6_delimiters_and_first_raster_bytes(self) -> None:
        header = b"P6\n# dimensions follow\n1 # width\n1\n# maxval follows\n255"
        for delimiter in (b"\n", b"\r", b"\r\n", b" ", b"\t", b"\v", b"\f"):
            for first in (0, 10, 13, 32, 35):
                pixel = bytes((first, 13, 35))
                with self.subTest(delimiter=delimiter, first=first):
                    actual, _ = self.run_pipeline(1, 1, pixel, ["sharpen"],
                                                  raw=header + delimiter + pixel)
                    self.assertEqual(actual, pixel)
        self.run_pipeline(1, 1, b"# \r", ["sharpen"], raw=header + b"\r# \r")
        self.run_pipeline(1, 1, bytes((36, 73, 109)), ["sharpen"],
                          raw=b"P6\n1 1\n7\r\n\x01\x02\x03")

    def test_input_equals_output(self) -> None:
        pixels = bytes(range(45))
        filters = ["blur", "sharpen", "grayscale"]
        for magic in ("P3", "P6"):
            with self.subTest(magic=magic):
                self.source.write_bytes(encode_ppm(5, 3, pixels, magic=magic))
                before = set(self.directory.iterdir())
                result = self.invoke(["-i", self.source, "-o", self.source,
                                      "-f", ",".join(filters), "-t", "2", "-q", "1", "--stats"])
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(decode_output(self.source.read_bytes()),
                                 (5, 3, reference_pipeline(5, 3, pixels, filters)))
                self.assert_stats(result, 5, 3, filters, "parallel", 2, 1, 32)
                self.assertEqual(set(self.directory.iterdir()), before)

    def test_invalid_arguments(self) -> None:
        self.source.write_bytes(encode_ppm(1, 1, b"\x01\x02\x03"))
        base = ["-i", self.source, "-o", self.output, "--stats"]
        incomplete = ([], ["-i", self.source], ["-o", self.output])
        for arguments in incomplete:
            with self.subTest(arguments=arguments):
                self.assert_failure_preserves_output(arguments, 2)
        invalid = [["--unknown"], ["unexpected-positional"], ["--", "positional"]]
        invalid += [[option] for option in ("--input", "--output", "--filters", "--mode",
                                            "--threads", "--queue-capacity", "--tile-rows")]
        invalid += [["--mode", value] for value in ("", "serial", "Parallel")]
        invalid += [["--threads", value] for value in ("0", "-1", "257", "abc", "1.5",
                                                       "1x", "18446744073709551616")]
        invalid += [["--queue-capacity", value] for value in ("0", "-1", "65537", "abc")]
        invalid += [["--tile-rows", value] for value in ("0", "-1", "1048577", "abc")]
        invalid += [["--filters", value] for value in (
            "", ",", "blur,", ",blur", "blur,,edge", "unknown", "GrayScale",
            "blur, edge", ",".join(["blur"] * 17),
        )]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assert_failure_preserves_output(base + arguments, 2)

    def test_malformed_ppm_preserves_existing_output(self) -> None:
        malformed = {
            "empty": b"", "unsupported P1": b"P1\n1 1\n0\n",
            "unsupported P2": b"P2\n1 1\n255\n0\n", "unsupported P4": b"P4\n1 1\n\0",
            "unsupported P5": b"P5\n1 1\n255\n\0", "bad magic": b"P30\n1 1\n255\n0 0 0\n",
            "missing dimension": b"P3\n1", "zero width": b"P3\n0 1\n255\n",
            "negative width": b"P3\n-1 1\n255\n", "zero height": b"P3\n1 0\n255\n",
            "negative height": b"P3\n1 -1\n255\n", "bad width": b"P3\nx 1\n255\n",
            "overflow width": b"P3\n18446744073709551616 1\n255\n",
            "overflow dimensions": b"P3\n18446744073709551615 18446744073709551615\n255\n",
            "pixel limit": b"P6\n10001 10000\n255\n", "large image": b"P6\n20000 20000\n255\n",
            "zero maxval": b"P3\n1 1\n0\n0 0 0\n", "negative maxval": b"P3\n1 1\n-1\n0 0 0\n",
            "maxval 256": b"P6\n1 1\n256\n\0\0\0", "16 bit": b"P6\n1 1\n65535\n\0\0\0\0\0\0",
            "bad maxval": b"P3\n1 1\nfoo\n0 0 0\n", "missing maxval": b"P6\n1 1\n",
            "P3 missing sample": b"P3\n1 1\n255\n1 2\n",
            "P3 no samples": b"P3\n1 1\n255\n", "P3 negative": b"P3\n1 1\n255\n-1 2 3\n",
            "P3 above maxval": b"P3\n1 1\n15\n1 16 3\n",
            "P3 sample overflow": b"P3\n1 1\n255\n18446744073709551616 0 0\n",
            "P3 fractional": b"P3\n1 1\n255\n1.5 2 3\n",
            "P3 trailing sample": b"P3\n1 1\n255\n1 2 3 4\n",
            "P3 trailing word": b"P3\n1 1\n255\n1 2 3\n# comment\ngarbage\n",
            "P3 CR comment then extra sample": b"P3\n1 1\n255\n0 0 0 # comment\r99\n",
            "P6 no raster": b"P6\n1 1\n255\n", "P6 short raster": b"P6\n1 1\n255\n\x01\x02",
            "P6 above maxval": b"P6\n1 1\n15\n\x01\x10\x03",
            "P6 missing delimiter": b"P6\n1 1\n255\x01\x02\x03",
            "P6 double delimiter": b"P6\n1 1\n255\n\n\x01\x02\x03",
            "P6 trailing newline": b"P6\n1 1\n255\n\x01\x02\x03\n",
            "P6 trailing comment": b"P6\n1 1\n255\n\x01\x02\x03# not a comment",
            "P6 trailing NUL": b"P6\n1 1\n255\n\x01\x02\x03\0",
            "P6 CRLF trailing sample": b"P6\n1 1\n255\r\n\x01\x02\x03\x04",
            "P6 CR trailing sample": b"P6\n1 1\n255\r\x01\x02\x03\x04",
            "NUL dimension": b"P3\n1\x00 1\n255\n0 0 0\n",
            "NUL maxval": b"P6\n1 1\n25\x005\n\0\0\0",
            "NUL text sample": b"P3\n1 1\n255\n1\x002 0 0\n",
        }
        for name, data in malformed.items():
            with self.subTest(case=name):
                self.source.write_bytes(data)
                self.assert_failure_preserves_output(
                    ["-i", self.source, "-o", self.output, "-t", "2", "--stats"], 1)

    def test_failed_run_does_not_create_output_or_replace_input(self) -> None:
        malformed = b"P6\n1 1\n255\n\x01"
        self.source.write_bytes(malformed)
        result = self.invoke(["-i", self.source, "-o", self.output, "--stats"])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(set(self.directory.iterdir()), {self.source})
        result = self.invoke(["-i", self.source, "-o", self.source, "--stats"])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.stdout.lstrip().startswith(b"{"))
        self.assertEqual(self.source.read_bytes(), malformed)
        self.assertEqual(set(self.directory.iterdir()), {self.source})

    def test_missing_input_and_input_directory(self) -> None:
        for source in (self.source, self.directory):
            with self.subTest(source=source):
                self.assert_failure_preserves_output(
                    ["-i", source, "-o", self.output, "--stats"], 1)

    def test_output_directory_and_missing_parent(self) -> None:
        self.source.write_bytes(encode_ppm(1, 1, b"\x01\x02\x03"))
        destination_directory = self.directory / "destination directory"
        destination_directory.mkdir()
        keep = destination_directory / "keep.txt"
        keep.write_bytes(b"keep existing directory content")
        before = set(self.directory.iterdir())
        for output in (destination_directory, self.directory / "missing" / "out.ppm"):
            with self.subTest(output=output):
                result = self.invoke(["-i", self.source, "-o", output, "-m", "sequential", "--stats"])
                self.assertEqual(result.returncode, 1, result.stderr.decode(errors="replace"))
                self.assertFalse(result.stdout.lstrip().startswith(b"{"))
                self.assertEqual(set(self.directory.iterdir()), before)
                self.assertEqual(keep.read_bytes(), b"keep existing directory content")
                self.assertEqual(set(destination_directory.iterdir()), {keep})

    @unittest.skipUnless(hasattr(os, "pathconf"), "requires POSIX filename limits")
    def test_output_at_filesystem_filename_limit(self) -> None:
        maximum = os.pathconf(self.directory, "PC_NAME_MAX")
        if maximum < 16:
            self.skipTest("filesystem does not report a usable filename limit")
        pixels = bytes((25, 40, 90))
        self.source.write_bytes(encode_ppm(1, 1, pixels))
        for name in ("x" * (maximum - 4) + ".ppm", "." + "x" * (maximum - 5) + ".ppm",
                     ".imgpipe.tmp.ABC123", "_imgpipe.tmp.ABC123"):
            output = self.directory / name
            for destination in (output, Path(output.name)):
                with self.subTest(destination=destination):
                    output.write_bytes(b"previous destination")
                    result = self.invoke(["-i", self.source, "-o", destination,
                                          "-f", "sharpen", "-m", "sequential"])
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                    self.assertEqual(decode_output(output.read_bytes()), (1, 1, pixels))
                    self.assertEqual(set(self.directory.iterdir()), {self.source, output})
            output.unlink()

    def test_generator_reproducible_pattern(self) -> None:
        width, height = 17, 3
        expected = bytearray()
        for y in range(height):
            for x in range(width):
                expected.extend(((17 * x + 3 * y) % 256,
                                 (5 * x + 11 * y + (x ^ y)) % 256,
                                 (7 * x + 23 * y + ((x // 16) ^ (y // 16)) * 61) % 256))
        for output in (self.source, self.output):
            result = self.invoke_tool("generate_ppm.py", ["--width", width, "--height", height,
                                                          "--output", output])
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(decode_output(output.read_bytes()), (width, height, bytes(expected)))
        self.assertEqual(self.source.read_bytes(), self.output.read_bytes())

    def test_tool_argument_validation(self) -> None:
        sentinel = b"invalid tool arguments must not truncate existing results"
        self.output.write_bytes(sentinel)
        generator_base = ["--width", "3", "--height", "2", "--output", self.output]
        generator_invalid = (
            ["--width", "0"], ["--width", "-1"], ["--height", "0"], ["--height", "abc"],
            ["--width", "10001", "--height", "10000"], ["--width"],
        )
        benchmark_base = ["--binary", BINARY, "--output", self.output]
        benchmark_invalid = (
            ["--sizes", ""], ["--sizes", "0"], ["--sizes", "10001"], ["--sizes", "2,,3"],
            ["--sizes", "2,2"], ["--threads", "0"], ["--threads", "257"], ["--threads", "1,1"],
            ["--threads", "1,"], ["--repeats", "0"], ["--repeats", "-1"], ["--warmup", "0"],
            ["--queue-capacity", "0"], ["--queue-capacity", "65537"],
            ["--tile-rows", "0"], ["--tile-rows", "1048577"], ["--filters", ""],
            ["--filters", "unknown"], ["--filters", ",".join(["blur"] * 17)],
            ["--timeout", "0"], ["--timeout", "nan"], ["--timeout", "inf"],
            ["--binary", self.directory / "no-such-binary"],
        )
        for name, base, invalid in (("generate_ppm.py", generator_base, generator_invalid),
                                    ("benchmark.py", benchmark_base, benchmark_invalid)):
            for arguments in invalid:
                with self.subTest(tool=name, arguments=arguments):
                    result = self.invoke_tool(name, base + arguments)
                    self.assertEqual(result.returncode, 2, result.stderr.decode(errors="replace"))
                    self.assertEqual(self.output.read_bytes(), sentinel)
                    self.assertEqual(set(self.directory.iterdir()), {self.output})

    def test_benchmark_csv_and_every_raw_sample(self) -> None:
        output = self.directory / "audited timings.csv"
        result = self.invoke_tool("benchmark.py", [
            "--binary", BINARY, "--sizes", "3,5", "--threads", "1,2", "--repeats", "2",
            "--warmup", "1", "--filters", "grayscale,blur,sharpen", "--queue-capacity", "1",
            "--tile-rows", "2", "--timeout", "10", "--output", output,
        ], timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"Process ms", result.stdout)
        self.assertIn(b"Total ms", result.stdout)
        result.stdout.decode("ascii")
        with output.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        metadata_path = output.with_suffix(".environment.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(set(self.directory.iterdir()), {output, metadata_path})
        self.assertEqual(len(rows), 6)
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["binary"]["sha256"], hashlib.sha256(BINARY.read_bytes()).hexdigest())
        for field in ("date_utc", "completed_utc", "platform", "kernel", "cpu_count", "cpu_model",
                      "compiler_version", "compiler_command", "python_version", "configuration"):
            self.assertIn(field, metadata)
        self.assertEqual(metadata["configuration"]["measurement_seed"], 20261109)
        self.assertEqual(metadata["input_pattern"], "rgb-v1")
        self.assertEqual(len(metadata["inputs"]), 2)
        samples = metadata["raw_samples"]
        self.assertEqual(len(samples), 20)
        self.assertEqual([sample["run_index"] for sample in samples], list(range(1, 21)))
        modes = {("sequential", 1), ("parallel", 1), ("parallel", 2)}
        for size in (3, 5):
            size_samples = [sample for sample in samples if sample["width"] == size]
            baseline = [sample for sample in size_samples if sample["phase"] == "baseline"]
            self.assertEqual(len(baseline), 1)
            self.assertEqual((baseline[0]["mode"], baseline[0]["threads"]), ("sequential", 1))
            reference_hash = baseline[0]["output_sha256"]
            for sample in size_samples:
                self.assertEqual(sample["output_sha256"], reference_hash)
                self.assertGreaterEqual(sample["wall_ms"], 0)
                self.assertEqual(sample["stats"]["width"], size)
                self.assertEqual(sample["stats"]["height"], size)
                self.assertEqual(sample["stats"]["mode"], sample["mode"])
                self.assertEqual(sample["command"].count("--stats"), 1)
            warmups = [sample for sample in size_samples if sample["phase"] == "warmup"]
            measured = [sample for sample in size_samples if sample["phase"] == "measured"]
            self.assertEqual(len(warmups), 3)
            self.assertEqual({(sample["mode"], sample["threads"]) for sample in warmups}, modes)
            for repeat in (1, 2):
                round_samples = [sample for sample in measured if sample["repeat"] == repeat]
                self.assertEqual(len(round_samples), 3)
                self.assertEqual({(sample["mode"], sample["threads"]) for sample in round_samples}, modes)
            sequential_median = statistics.median(sample["stats"]["process_ms"] for sample in measured
                                                  if sample["mode"] == "sequential")
            for row in (row for row in rows if int(row["width"]) == size):
                mode, threads = row["mode"], int(row["threads"])
                values = [sample["stats"] for sample in measured
                          if (sample["mode"], sample["threads"]) == (mode, threads)]
                self.assertEqual(len(values), 2)
                self.assertEqual(row["repeats"], "2")
                self.assertEqual(row["warmup"], "1")
                self.assertEqual(row["output_sha256"], reference_hash)
                for name in ("process_ms", "total_ms"):
                    timings = [stats[name] for stats in values]
                    for suffix, expected in (("median", statistics.median(timings)),
                                             ("min", min(timings)), ("max", max(timings))):
                        self.assertAlmostEqual(float(row[name + "_" + suffix]), expected, delta=0.000001)
                process_median = statistics.median(stats["process_ms"] for stats in values)
                if sequential_median > 0 and process_median > 0:
                    speedup = sequential_median / process_median
                    self.assertAlmostEqual(float(row["speedup"]), speedup, delta=0.000001)
                    self.assertAlmostEqual(float(row["efficiency"]), speedup / threads, delta=0.000001)
                else:
                    self.assertEqual(row["speedup"], "")
                    self.assertEqual(row["efficiency"], "")

    def test_benchmark_rejects_mismatch_invalid_stats_and_timeout(self) -> None:
        with mock.patch.object(sys, "path", [str(TOOLS), *sys.path]):
            benchmark = runpy.run_path(str(TOOLS / "benchmark.py"), run_name="benchmark_under_test")
        arguments = argparse.Namespace(filters=["blur"], queue_capacity=1, tile_rows=1, timeout=1)
        stats = {"mode": "parallel", "width": 1, "height": 1, "filters": ["blur"],
                 "threads": 2, "queue_capacity": 1, "tile_rows": 1, "stage_ms": [1.0],
                 **{name: 1.0 for name in TIMING_FIELDS}}
        samples = []

        def different_output(command, **unused):
            self.output.write_bytes(b"not the sequential output")
            return subprocess.CompletedProcess(command, 0, json.dumps(stats), "")

        expected_hash = hashlib.sha256(b"sequential reference output").hexdigest()
        with mock.patch.object(subprocess, "run", side_effect=different_output):
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                benchmark["run_sample"](BINARY, self.source, self.output, 1, "parallel", 2,
                                        arguments, "warmup", 1, expected_hash, samples)
        self.assertEqual(samples, [])
        stats["stage_ms"] = [True]
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(stats), "")):
            with self.assertRaisesRegex(RuntimeError, "invalid timing"):
                benchmark["run_sample"](BINARY, self.source, self.output, 1, "parallel", 2,
                                        arguments, "measured", 1, expected_hash, samples)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaisesRegex(RuntimeError, "subprocess exceeded"):
                benchmark["run_sample"](BINARY, self.source, self.output, 1, "parallel", 2,
                                        arguments, "measured", 1, expected_hash, samples)
        self.assertEqual(samples, [])

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "requires POSIX FIFOs/signals")
    def test_cooperative_sigint_and_sigterm_preserve_output(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            for existing_output in (True, False):
                with self.subTest(signal=int(signal_number), existing_output=existing_output):
                    fifo = self.directory / f"signal-{int(signal_number)}-{existing_output}.fifo"
                    os.mkfifo(fifo)
                    sentinel = b"existing output survives cancellation\0\xff"
                    if existing_output:
                        self.output.write_bytes(sentinel)
                    else:
                        self.output.unlink(missing_ok=True)
                    before = set(self.directory.iterdir())
                    writer_result = queue.Queue(maxsize=1)

                    def open_writer() -> None:
                        try:
                            writer_result.put(os.open(fifo, os.O_WRONLY))
                        except OSError as error:
                            writer_result.put(error)

                    process = subprocess.Popen(
                        [str(BINARY), "-i", str(fifo), "-o", str(self.output),
                         "-t", "2", "-q", "1", "--stats"],
                        cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    writer = threading.Thread(target=open_writer, daemon=True)
                    writer.start()
                    descriptor = None
                    try:
                        descriptor = writer_result.get(timeout=SUBPROCESS_TIMEOUT)
                        if isinstance(descriptor, OSError):
                            error = descriptor
                            descriptor = None
                            raise error
                        # Opening the FIFO writer rendezvous with the child's
                        # fopen, after handler installation. Leave the raster
                        # incomplete so input cannot finish before the signal.
                        os.write(descriptor, b"P6\n1 1\n255\n")
                        process.send_signal(signal_number)
                        # EOF also unblocks a read restarted by SA_RESTART.
                        os.close(descriptor)
                        descriptor = None
                        stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
                        self.assertEqual(process.returncode, 128 + int(signal_number),
                                         stderr.decode(errors="replace"))
                        self.assertEqual(stdout, b"")
                        if existing_output:
                            self.assertEqual(self.output.read_bytes(), sentinel)
                        else:
                            self.assertFalse(self.output.exists())
                        self.assertEqual(set(self.directory.iterdir()), before)
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)
                        wake_reader = None
                        if writer.is_alive():
                            wake_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                        writer.join(timeout=5)
                        if wake_reader is not None:
                            os.close(wake_reader)
                        try:
                            unused = writer_result.get_nowait()
                            if isinstance(unused, int):
                                os.close(unused)
                        except queue.Empty:
                            pass
                        self.assertFalse(writer.is_alive(), "FIFO writer cleanup failed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux /proc thread accounting")
    def test_signals_after_worker_activity_preserve_output(self) -> None:
        width = height = 2048
        self.source.write_bytes(encode_ppm(width, height, b"\x11\x7f\xe9" * (width * height)))
        inherited_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        worker_mask = sum(1 << (number - 1) for number in
                          inherited_mask | {signal.SIGINT, signal.SIGTERM})
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            for existing_output in (True, False):
                with self.subTest(signal=int(signal_number), existing_output=existing_output):
                    sentinel = b"existing output survives active filtering cancellation"
                    if existing_output:
                        self.output.write_bytes(sentinel)
                    else:
                        self.output.unlink(missing_ok=True)
                    before = set(self.directory.iterdir())
                    process = subprocess.Popen(
                        [str(BINARY), "-i", str(self.source), "-o", str(self.output),
                         "-f", ",".join(["blur"] * 16), "-t", "2", "-q", "1", "-r", "8", "--stats"],
                        cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    try:
                        deadline = time.monotonic() + SUBPROCESS_TIMEOUT
                        active_workers = False
                        while process.poll() is None and time.monotonic() < deadline:
                            worker_ticks = []
                            try:
                                for task in Path(f"/proc/{process.pid}/task").iterdir():
                                    if task.name == str(process.pid):
                                        continue
                                    status = dict(line.split(":", 1) for line in
                                                  (task / "status").read_text().splitlines())
                                    if int(status["SigBlk"].strip(), 16) != worker_mask:
                                        continue
                                    fields = (task / "stat").read_text().rsplit(")", 1)[1].split()
                                    worker_ticks.append(int(fields[11]) + int(fields[12]))
                            except FileNotFoundError:
                                continue
                            # Match the pool's inherited mask, then wait for actual CPU
                            # activity rather than guessing a startup delay.
                            if len(worker_ticks) == 2 and max(worker_ticks) >= 2:
                                active_workers = True
                                break
                            time.sleep(0.005)
                        self.assertTrue(active_workers, "did not observe CPU-active pool workers")
                        process.send_signal(signal_number)
                        stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
                        self.assertEqual(process.returncode, 128 + int(signal_number),
                                         stderr.decode(errors="replace"))
                        self.assertEqual(stdout, b"")
                        self.assertIn(b"accepted tasks drained", stderr)
                        if existing_output:
                            self.assertEqual(self.output.read_bytes(), sentinel)
                        else:
                            self.assertFalse(self.output.exists())
                        self.assertEqual(set(self.directory.iterdir()), before)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)


def main() -> None:
    global BINARY
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--binary", type=Path, default=BINARY)
    arguments, unittest_arguments = parser.parse_known_args()
    BINARY = arguments.binary.expanduser().resolve()
    if not BINARY.is_file() or (os.name == "posix" and not os.access(BINARY, os.X_OK)):
        parser.error(f"binary is not an executable file: {BINARY}")
    unittest.main(argv=[sys.argv[0], *unittest_arguments])


if __name__ == "__main__":
    main()
