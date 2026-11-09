#!/usr/bin/env python3
"""Generate a reproducible P6 image using only one RGB row of working memory."""

import argparse
from pathlib import Path
import sys


MAX_PIXELS = 100_000_000
PATTERN_VERSION = "rgb-v1"


def positive_int(value: str) -> int:
    try:
        number = int(value, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def generate_ppm(output: Path, width: int, height: int) -> None:
    """Write a deterministic 8-bit RGB PPM; dimensions obey the pipeline limit."""
    if (type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0):
        raise ValueError("width and height must be positive integers")
    if width * height > MAX_PIXELS:
        raise ValueError(f"image exceeds {MAX_PIXELS} pixels")

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = bytearray(width * 3)
    with output.open("wb") as stream:
        stream.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        for y in range(height):
            for x in range(width):
                offset = x * 3
                row[offset] = (17 * x + 3 * y) & 255
                row[offset + 1] = (5 * x + 11 * y + (x ^ y)) & 255
                row[offset + 2] = (
                    7 * x + 23 * y + ((x // 16) ^ (y // 16)) * 61
                ) & 255
            stream.write(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=positive_int, required=True)
    parser.add_argument("--height", type=positive_int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.width * arguments.height > MAX_PIXELS:
        parser.error(f"image exceeds {MAX_PIXELS} pixels")
    try:
        generate_ppm(arguments.output, arguments.width, arguments.height)
    except (OSError, ValueError) as error:
        print(f"generate_ppm: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
