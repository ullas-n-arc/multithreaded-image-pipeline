# Multithreaded Image Pipeline

A Linux C11 project for processing one PPM image through an ordered sequence of
filters. It provides a sequential execution path and a parallel path backed by
a persistent, bounded POSIX thread pool.

Here, **pipeline means ordered filter stages**, not overlapped streaming:
loading finishes first, every stage finishes before the next starts, and saving
happens last. The worker pool is reused across stages of one invocation.

The focus is explicit task ownership, synchronization, failure handling, and
reproducible comparisons between sequential and multithreaded execution.

## Features

- P3 text and P6 binary PPM input; P6 output with 8-bit RGB samples.
- Grayscale, 3x3 blur, sharpen, and edge filters in configurable order.
- Sequential and parallel paths with the same filter mathematics.
- Fixed worker count, bounded ring-buffer queue, and producer backpressure.
- Row tiles with disjoint output regions; no per-pixel mutex.
- A completion barrier between stages and two reusable full-image buffers.
- Cooperative SIGINT/SIGTERM handling without `pthread_cancel`.
- Same-directory temporary output followed by checked atomic replacement.
- JSON phase timings, automated tests, sanitizer targets, and benchmark tools.

## Prerequisites

The executable targets Linux/POSIX, not native Windows. Required tools are:

- GNU Make.
- GCC or Clang with C11 and POSIX pthread support.
- Python 3.8 or newer, using only the standard library, for tests and helper tools.

On Debian or Ubuntu:

```sh
sudo apt-get update
sudo apt-get install build-essential python3 git
```

On Windows, open an installed Linux distribution through WSL2 from PowerShell:

```powershell
wsl --distribution Debian
```

Run the remaining commands in the Linux shell. A clone in the Linux home
directory avoids Windows-mounted filesystem overhead during builds and I/O.

## Build

```sh
git clone https://github.com/ullas-n-arc/multithreaded-image-pipeline.git
cd multithreaded-image-pipeline
make
./bin/imgpipe --help
```

The normal build uses `-O3 -g`, C11, warnings as errors, and `-pthread` during
compilation and linking. Headers are tracked through generated dependencies.
The compiler can be selected explicitly:

```sh
make clean
make CC=gcc
# Alternatively, after installing Clang:
make clean
make CC=clang
```

Clean before switching compilers or normal-build flags; Make does not track
compiler-option changes as source dependencies. Sanitizer builds use separate
directories and do not replace the normal executable.

| Target | Purpose |
| --- | --- |
| `make` or `make all` | Build `bin/imgpipe`. |
| `make test` | Build/run the C pool tests and Python CLI/image tests. |
| `make sanitize` | Run tests with ASan and UBSan in `build/asan`, `bin/asan`. |
| `make tsan` | Run tests with ThreadSanitizer in `build/tsan`, `bin/tsan`. |
| `make demo` | Generate a 512x512 input and run three filters with four workers. |
| `make benchmark` | Run the benchmark tool; write `benchmarks/results.csv`. |
| `make clean` | Remove `build` and `bin`; retain generated examples/results. |

## Quickstart

```sh
make demo
```

The demo creates `examples/input.ppm` and `examples/output.ppm`, applies
`grayscale,blur,sharpen`, and prints timing statistics. Use an image viewer that
supports PPM to inspect the files; the executable has no graphical interface.

To generate and process a different image:

```sh
mkdir -p examples
python3 tools/generate_ppm.py --width 1024 --height 768 --output examples/input.ppm
./bin/imgpipe -i examples/input.ppm -o examples/parallel.ppm \
  -f grayscale,blur,sharpen -m parallel -t 4 -q 64 -r 32 --stats
./bin/imgpipe -i examples/input.ppm -o examples/sequential.ppm \
  -f grayscale,blur,sharpen -m sequential --stats
cmp examples/sequential.ppm examples/parallel.ppm
sha256sum examples/sequential.ppm examples/parallel.ppm
```

`cmp` should report no differences for matching input and filter order. This is
a useful differential check, not by itself proof that both implementations use
the intended mathematics. Tiny independently computed examples are also needed.

For an edge-filter example:

```sh
./bin/imgpipe --input examples/input.ppm --output examples/edges.ppm \
  --filters grayscale,edge --threads 4
```

## Command Reference

```text
imgpipe --input PATH --output PATH [options]
```

| Option | Meaning | Default / valid values |
| --- | --- | --- |
| `-i`, `--input PATH` | Required input PPM path. | Required. |
| `-o`, `--output PATH` | Required destination path. | Required. |
| `-f`, `--filters LIST` | Comma-separated, ordered filter names. | `grayscale,blur`; 1 to 16 stages. |
| `-m`, `--mode MODE` | Execution path. | `parallel`; `sequential` or `parallel`. |
| `-t`, `--threads N` | Parallel worker count. | Online CPU count capped at 32; 1 to 256. |
| `-q`, `--queue-capacity N` | Maximum queued, not active, jobs. | 64; 1 to 65536. |
| `-r`, `--tile-rows N` | Maximum rows in a submitted tile. | 32; 1 to 1048576. |
| `--stats` | Emit JSON timing/configuration statistics to stdout after successful output. | Disabled. |
| `-h`, `--help` | Show usage. | No processing. |

Filter names are `grayscale`, `blur`, `sharpen`, and `edge`. Repetition and order
matter: `blur,sharpen` is not generally equivalent to `sharpen,blur`. A final
tile can be shorter than `tile_rows`, and a tile taller than the image is valid.
Sequential mode does not create a pool; its statistics report one thread and
zero queue capacity, regardless of parallel settings supplied on the command line.
Those numeric options must still pass validation in sequential mode. Names are
case-sensitive; filter lists cannot contain empty entries or spaces around names.
Numeric options accept decimal digits only, and positional arguments are rejected.
If the online CPU count cannot be obtained, the default worker count is one.
Successful processing or help returns 0; usage errors return 2 and ordinary
runtime failures return 1. Signal-specific exit statuses are described below.
Failed stdout writes, including flushing `--help` or `--stats`, return 1.
Statistics are emitted after image publication, so a statistics-output failure
does not undo a successfully written image.

### Timing Fields

`--stats` records elapsed wall-clock time with `clock_gettime(CLOCK_MONOTONIC)`.
Times are milliseconds, not CPU time and not a claim about another machine.

| JSON field | Interpretation |
| --- | --- |
| `mode` | `sequential` or `parallel`. |
| `width`, `height` | Loaded image dimensions. |
| `filters` | Ordered list of applied filter names. |
| `threads` | Worker count, or 1 for sequential mode. |
| `queue_capacity` | Configured parallel queue capacity, or 0 sequentially. |
| `tile_rows` | Configured tile height. |
| `load_ms` | Input reading, validation, and normalization. |
| `setup_ms` | Scratch-image allocation and, in parallel mode, pool creation. |
| `process_ms` | All filter stages, including task dispatch and stage waits. |
| `shutdown_ms` | Worker-pool drain/join/destruction. |
| `save_ms` | Temporary-file write and output replacement. |
| `total_ms` | Timed load-through-save interval, including those phases. |
| `stage_ms` | Per-stage times in the same order as `filters`. |

`process_ms` excludes loading, scratch/pool setup, pool shutdown, and saving.
`total_ms` is not whole-process timing: process startup, argument parsing, and
final image-buffer deallocation are outside its scope. Subtracting timestamps
is more meaningful than treating displayed rounded fields as an exact identity.

## PPM Formats

The `Image` struct stores `size_t` width and height and a pointer to a separate
contiguous RGB allocation: three interleaved `unsigned char` samples per pixel.

| Property | Behavior |
| --- | --- |
| Input magic | `P3` (text samples) or `P6` (binary samples). |
| Dimensions | Positive values, checked for arithmetic overflow. |
| Pixel limit | At most 100000000 pixels. |
| Input maximum sample | `maxval` from 1 through 255; 16-bit PPM is rejected. |
| Normalization | `(sample * 255 + maxval / 2) / maxval`, using integer arithmetic. |
| Invalid samples/data | Out-of-range samples and malformed/truncated files are rejected. |
| Header syntax | Supported whitespace/comments in text fields; comments end at CR or LF; CRLF supported. |
| Output | Always `P6`, `maxval` 255, with binary RGB samples. |

For P6, the reader consumes the required delimiter after `maxval`. It does
**not** skip arbitrary whitespace or comments after it: newline, space, and `#`
may be pixel values. Both bare CR and CRLF are supported. After a CR, exactly
the expected raster length makes any initial LF a sample; one extra leading LF
is interpreted as the CRLF delimiter instead. A shortened CRLF image whose
bytes also form a complete bare-CR image cannot be distinguished from that
valid interpretation. No other trailing binary data is accepted.

The reader accepts exactly one image. P3 may end with whitespace/comments but
not extra sample tokens. P6 must end after its exact raster size; extra bytes
are rejected. Concatenated images and permissive trailing binary whitespace
are intentionally unsupported.

Two RGB image buffers at the pixel limit require approximately 600 MB decimal
before queue storage, task descriptors, worker stacks, and other allocations.
The limit is a validation bound, not a guarantee that allocation will succeed.

## Filter Mathematics

Every stage reads an immutable source and writes a separate destination.
Convolutions operate independently on each RGB channel using signed integer
accumulation and clamp the result to `[0, 255]`.

### Grayscale

```text
gray = (299 * R + 587 * G + 114 * B + 500) / 1000
output = (gray, gray, gray)
```

Division is integer division. The added 500 implements nearest-integer
rounding for this nonnegative weighted sum. These are conventional encoded-RGB
weights, **not gamma-correct, linear-light perceptual color processing**.

### Convolution Kernels

```text
blur:                  sharpen:               edge:
 1  1  1                0 -1  0               -1 -1 -1
 1  1  1               -1  5 -1               -1  8 -1
 1  1  1                0 -1  0               -1 -1 -1
```

Blur returns `(sum + 4) / 9`. Sharpen and edge use their signed sums directly
before clamping; negative responses become zero rather than wrapping or taking
an absolute value. This edge filter is a Laplacian-style kernel, not Sobel.

At boundaries, coordinates are clamped to the nearest valid source pixel
(replicated edges). There is no zero padding, wraparound, or image cropping.
For a 1x1 image, blur and sharpen preserve each channel, while edge returns zero.

## Architecture

```text
                          one CLI invocation / one image
                                      |
                                  PPM load
                                      |
                       allocate scratch + create pool
                                      |
              +---------------- ordered stages ----------------+
              |                                                |
              |  source A -> row-tile jobs -> destination B     |
              |            wait: queued == 0 && active == 0     |
              |                  swap A and B                   |
              |            repeat for the next filter          |
              +------------------------------------------------+
                                      |
                          close/drain/join worker pool
                                      |
                         temporary P6 write -> rename
```

The diagram shows the parallel path. Sequential mode computes row tiles directly
without creating workers or submitting queue records.

The producer creates tiles `[start_row, end_row)` spanning the image width.
Workers write only their tile's destination rows. Convolution tiles can read
neighboring source rows (the halo), but the source is never modified during
the stage. This ownership split removes the need for per-pixel locks.

The two buffers are swapped only after a successful completed stage. The
completion barrier prevents the next stage from reading partially written
data and keeps both buffers alive until all accepted work is finished.

### Pool Synchronization

The pool contains a fixed set of workers and a heap-allocated ring buffer of
jobs, each storing a function pointer and an argument pointer. One mutex protects
the queue positions, closed state, and queued/active counters.

| Condition variable | Predicate checked while holding the mutex |
| --- | --- |
| `not_empty` | A queued job exists, or the pool has closed. |
| `not_full` | A queue slot is available, or the pool has closed. |
| `idle` | Both `queued == 0` and `active == 0`. |

All condition waits use `while` loops: wakeups are notifications to recheck
state, not proof that the predicate is now true. A worker removes a job and
increments `active` under the mutex, executes the callback **outside** the
mutex, then decrements `active` under the mutex. Thus callbacks can overlap
without serializing the actual filtering.

`thread_pool_wait` waits for queued **and** running work; an empty queue alone
does not establish stage completion. The main producer finishes submitting a
stage before waiting, so no unrelated producer can extend that stage's batch.
The wait leaves the pool open and reusable.

`thread_pool_close` rejects new submissions, wakes blocked producers/workers,
and lets accepted jobs drain. `thread_pool_destroy` closes, joins the workers,
then destroys synchronization objects and frees pool storage. Create/submit
return zero on success or a positive errno/pthread error code on failure.

### Bounded Ownership

Each parallel tile descriptor is heap-allocated by the producer. Ownership transfers to the
callback only after successful submission, and the callback frees it. Failed
submission leaves the producer responsible for freeing its descriptor.
The generic pool does not free arbitrary argument pointers; freeing a tile is
the image callback's ownership convention, not automatic pool behavior.

In this single-producer pipeline, at most `queue_capacity + workers + 1` heap-allocated tile
descriptors are live: queued jobs,
active jobs, and one producer-held descriptor awaiting submission. The program
does not preallocate one descriptor per pixel or per eventual tile. Full image
storage still scales with the number of pixels; bounded metadata does not mean
constant total memory usage.

Pool callbacks must not submit to, wait on, or destroy their own pool.
`thread_pool_destroy` requires other API callers to have stopped; it is not
safe to race destruction against new calls. See the header contracts in
`src/thread_pool.h`, `src/image.h`, and `src/filters.h`.

## Shutdown And Errors

The `sigaction` handlers for SIGINT and SIGTERM only set a
`volatile sig_atomic_t` flag. They do not print, allocate, lock, signal a
condition variable, free memory, or cancel threads.

Before creating workers, main blocks these signals so workers inherit the
blocked mask, then restores main's prior mask. Only the producer reads the
flag during processing; worker callbacks do not use it as shared-thread state.
`volatile` is not a substitute for a mutex or C11 atomics in ordinary threading.

After observing a signal between tiles, main stops producing work and drains
already accepted jobs before releasing images and the pool. The signal exit
status is `128 + signal_number` (130 for SIGINT, 143 for SIGTERM). There is no
`pthread_cancel`, forced thread termination, or discard of accepted jobs.

Shutdown is cooperative, not immediate. A producer already blocked in a full
queue waits until workers make progress before it can observe cancellation.
Loading and writing are not tile-interruptible; signals received during I/O
are observed after that I/O phase. Large tiles and slow I/O can increase latency.
The installed handlers use `SA_RESTART`, so applicable interrupted system calls
can resume rather than serving as an immediate cancellation mechanism.
A stalled FIFO or device can therefore defer cancellation indefinitely until
the I/O is unblocked. Regular image files are the intended input; this is not
a deadline-bounded streaming interface.

Malformed input, invalid options, allocation failures, and output errors produce
diagnostics and a nonzero exit. An unsuccessful stage is drained before cleanup
and is not saved as a partially completed image.

### Output Replacement

The saver calls `mkstemp` on `.imgpipe.tmp.XXXXXX` in the destination's directory,
checks the header/raster writes, `fflush`, and `fclose`, checks cancellation,
then calls `rename`. The previous destination is replaced only by a completely
written image. The directory must already exist and permit temporary-file
creation; input and output may be the same path because input is closed first.
Replacement installs the temporary file rather than updating the old inode;
the saver does not copy the old destination's permissions, ownership, or metadata.
The fixed temporary basename also permits destinations at the filesystem's
filename-length limit without appending characters to that destination name.
For dot-prefixed destinations the temporary prefix is `_imgpipe.tmp.` instead;
different first characters ensure that the temporary name cannot be the final
destination itself, even when the requested name resembles a temporary file.

Atomic replacement is a visibility guarantee, **not crash durability**: there
is no file/directory `fsync`. SIGKILL or a crash can leave temporary files.
Cancellation is not transactional with rename; a signal arriving exactly at
the final check/rename boundary can coincide with a successful replacement.
Do not describe this as an always-rollback or power-loss-safe transaction.

## Tests

```sh
make test
make sanitize
make tsan
```

The C suite exercises the pool API. The Python suite exercises the executable,
PPM parsing, filter outputs, and error paths. The useful verification strategy
combines independently calculated small images with sequential/parallel
comparisons, bounded-queue stress, and malformed inputs.
Pool cases include concurrent producers, exactly-once execution, wait/reuse,
capacity-one backpressure, close/wakeup/drain, and destruction without a prior wait.
CLI cases include independent scalar filter references, FIFO-synchronized
SIGINT/SIGTERM runs, and Linux `/proc`-observed worker-activity cancellation.
The latter waits for CPU-accounted workers with the inherited signal mask,
then interrupts a multi-stage run with a capacity-one queue. It is stronger
than a guessed startup delay, but does not force every possible interleaving.
Output tests cover maximum-length filenames and stdout failures as well as
parsing, configuration, and ordinary output errors.

Important cases include queue capacity one, fewer rows than workers, partial
last tiles, oversized tiles, replicated corners, lower `maxval` normalization,
P6 pixels beginning with whitespace or `#`, truncation, trailing data, and
existing-output preservation on failure. Cancellation checks should also
verify that accepted jobs finish and cleanup does not hang.

ASan/UBSan check exercised memory/undefined-behavior paths; TSan checks exercised
thread interactions. A successful run is evidence for that build and workload,
not proof of all possible executions. Some WSL/kernel/toolchain combinations
cannot initialize sanitizer runtimes, especially TSan; record that separately
from application failures instead of claiming a sanitizer pass.

For an x86_64 TSan startup failure such as `unexpected memory mapping` before
main, this process-only troubleshooting command may help:

```sh
setarch x86_64 -R make tsan
```

This disables ASLR for that command's process tree, not system-wide. It is a
local sanitizer workaround, not a normal runtime recommendation or a test-pass
claim. If `setarch` is unavailable or denied, report the environment limitation.

The commands above are general usage instructions. The actual local validation
record, sanitizer workaround, and coverage limits are in
[Recorded Results And Validation](benchmarks/RESULTS.md).

## Benchmark Methodology

```sh
python3 tools/benchmark.py --binary ./bin/imgpipe \
  --sizes 512,1024,2048 --threads 1,2,4,8 --repeats 5 --warmup 1 \
  --filters grayscale,blur,sharpen --queue-capacity 64 --tile-rows 32 \
  --output benchmarks/results.csv
```

The benchmark generates square images, runs sequential and parallel variants,
performs warmups, and summarizes repeated timing samples with medians. It
actually hashes output files with SHA-256 to verify sequential/parallel
agreement; it does not infer correctness from the presence of an output file.
A separate sequential run establishes the reference hash. Warmup rounds precede
shuffled, interleaved measured rounds for each size; only measured rounds enter
the timing summaries. Raw samples and external subprocess wall time are retained
in the companion environment JSON, separately from the executable's `total_ms`.

The CSV includes configuration columns, `process_ms_median/min/max`,
`total_ms_median/min/max`, `speedup`, `efficiency`, and `output_sha256`.
Here the slash notation groups separate columns, such as `process_ms_median`
and `process_ms_min`. Efficiency is a ratio in the CSV, not a percentage.

| Tool argument | Meaning |
| --- | --- |
| `--binary PATH` | Executable to measure; default project `bin/imgpipe`. |
| `--sizes LIST` | Comma-separated square dimensions; default `256,512,1024,2048`. |
| `--threads LIST` | Comma-separated parallel worker counts; default `1,2,4,8`. |
| `--repeats N` | Measured repetitions; default 5. |
| `--warmup N` | Unmeasured warmup repetitions; default 1. |
| `--filters LIST` | Ordered stages; default `grayscale,blur,sharpen`. |
| `--queue-capacity N` | Queue size; default 64. |
| `--tile-rows N` | Tile height; default 32. |
| `--timeout SECONDS` | Per-processing-subprocess timeout; default 120 seconds. |
| `--output PATH` | Destination CSV; default `benchmarks/results.csv`; companion `.environment.json`. |

Repeat and warmup counts must be positive; zero warmups are not accepted. Square
sizes obey the pixel limit, and duplicate sizes/thread counts are rejected.

Processing speedup is `median(sequential process_ms) / median(parallel process_ms)`.
Efficiency is `speedup / worker_count`; multiply by 100 only when explicitly
presenting it as a percentage. End-to-end speedup uses `total_ms` instead and
must be labeled separately. Parallel mode with one worker is not the sequential
baseline: it still pays queue, dispatch, and synchronization costs.

Keep filters, image data, compiler flags, tile size, and queue capacity fixed
when comparing worker counts. Record CPU/OS, compiler, available resources,
warmups/repeats, and measurement scope. The companion environment file records
available run context; add any compiler flags or resource limits it does not
capture when publishing a result. WSL `/mnt/c` I/O, filesystem caches, background
load, CPU frequency changes, and thermal limits can influence timings.
The compiler metadata is the available `CC`/`cc` version at benchmark time,
not proof of the compiler that built an existing binary. Record the actual
build command and flags separately, especially when switching compilers.

Small inputs can be slower in parallel. Large inputs can become memory-bandwidth
limited. More threads do not imply proportional speedup. Benchmark the normal
optimized binary, not a sanitizer binary, for performance claims.

Recorded local measurements and their interpretation are in
[Recorded Results And Validation](benchmarks/RESULTS.md), with raw
[verified_results.csv](benchmarks/verified_results.csv) and
[verified_results.environment.json](benchmarks/verified_results.environment.json)
for the reviewed build. The report also preserves the earlier snapshot rather
than silently reusing its measurements for changed source.
`make benchmark` uses `results.csv` and `results.environment.json` instead.
These are environment-specific measurements, not production metrics or a
hardware-independent speedup guarantee.

## Project Tree

```text
multithreaded-image-pipeline/
|-- README.md
|-- Makefile
|-- src/
|   |-- main.c                  CLI, lifecycle, signals, and phase timing
|   |-- image.c / image.h       PPM validation, buffers, atomic output
|   |-- filters.c / filters.h   Filter math and row-tile dispatch
|   `-- thread_pool.c / .h      Bounded queue and persistent pthread workers
|-- tests/
|   |-- test_thread_pool.c     Pool API tests
|   `-- test_cli.py            CLI/image tests
|-- tools/
|   |-- generate_ppm.py        Deterministic PPM generation
|   `-- benchmark.py           Timing, hashes, CSV, and environment metadata
|-- benchmarks/               Generated measurement artifacts
|-- examples/                 Generated demo inputs/outputs
|-- build/                    Generated objects/dependencies; sanitizer subdirs
`-- bin/                      Generated executables; sanitizer subdirs
```

## Limitations

- One PPM image per invocation; no JPEG/PNG codec, alpha, or 16-bit support.
- Full-image buffering, not streaming or overlapped stage execution.
- Encoded-RGB integer filters, not linear-light color science.
- Fixed workers and FIFO queue; no work stealing, affinity, explicit SIMD kernels, or GPU path.
- No asynchronous job cancellation, per-job futures, or callback error channel.
- No signal-safe immediate shutdown or crash-durable output transaction.
- No guarantee that the largest accepted image fits available memory.
- Performance depends on workload and environment; measure rather than assume.

Possible extensions include SIMD kernels, additional image formats, and
streaming with explicit halo dependencies; these are not implemented features.
