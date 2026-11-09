# Recorded Results And Validation

These are local measurement snapshots, not hardware-independent performance
claims. The reviewed build was tested and benchmarked in Debian WSL2 on
November 9, 2026. Its results appear first; the original snapshot is retained
below with its own binary hash and validation record.

## Reviewed Build

The correctness audit reproduced and fixed three edge cases: an LF-valued
first P6 sample after a bare-CR delimiter, valid maximum-length destination
filenames, and silently failed stdout writes for help/statistics. Temporary
names also cannot equal the requested final pathname. The suite now includes
the exact 2x2 grayscale/blur example and cancellation after observed worker
activity, in addition to the existing input-phase signal checks.

### Environment And Artifacts

| Item | Recorded value |
| --- | --- |
| CPU / visible logical CPUs | AMD Ryzen 7 7435HS / 16 |
| OS / kernel | Debian WSL2 / `6.6.87.2-microsoft-standard-WSL2`, x86_64 |
| Compiler / Python | GCC 14.2.0 (`14.2.0-19`) / Python 3.13.5 |
| Normal build | `make -B -j4 test`; `-O3 -g -std=c11`, strict warnings, `-pthread` |
| Benchmark interval | `2026-11-09T08:49:55+00:00` to `2026-11-09T08:50:30+00:00` |
| Input/output filesystem | Linux `/tmp`; project/executable under WSL `/mnt/c` |

Reviewed benchmark binary SHA-256:

```text
c8ca7287cb8c34df862b7da94636e4f49a014b95155000d1fcc2b896400191cb
```

[verified_results.csv](verified_results.csv) contains 25 configuration rows.
[verified_results.environment.json](verified_results.environment.json) retains
all 155 executions: five sequential references, 25 warmups, and 125 measured
samples. Every warmup/measured output matched its size's sequential SHA-256.
Sanitizer and analyzer runs finished before this optimized benchmark. No CPU
affinity, isolated cores, or controlled thermal state was used.

### Reproduce The Reviewed Run

```sh
make -B -j4 test
python3 tools/benchmark.py --binary bin/imgpipe \
  --sizes 256,512,1024,2048,4096 --threads 1,2,4,8 \
  --repeats 5 --warmup 1 --filters grayscale,blur,sharpen \
  --queue-capacity 64 --tile-rows 32 \
  --output benchmarks/verified_results.csv
```

Use a different `--output` path to keep the committed measurement unchanged.
Rebuilding in another directory or with another toolchain can change the binary
hash, including debug-path information; a matching hash is not required to run
the same tests or reproduce the methodology.

### Eight-Worker Comparison

| Image | Sequential processing ms | 8-worker processing ms | Processing speedup | Sequential total ms | 8-worker total ms | Total speedup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 256x256 | 2.970 | 2.212 | 1.34x | 3.928 | 5.147 | 0.76x |
| 512x512 | 10.382 | 3.620 | 2.87x | 12.891 | 7.489 | 1.72x |
| 1024x1024 | 37.573 | 8.861 | 4.24x | 45.080 | 17.821 | 2.53x |
| 2048x2048 | 145.357 | 29.828 | 4.87x | 171.194 | 58.518 | 2.93x |
| 4096x4096 | 626.455 | 110.116 | 5.69x | 754.029 | 214.838 | 3.51x |

These are medians of five measured runs with a fixed eight-worker comparison,
not the fastest sample selected for each size. Processing efficiency at
4096x4096 is about 71.1%. At 256x256, eight workers improve filtering time but
make total time worse; two workers had the best medians in this particular run.
At 512x512, eight workers had the best processing median while four had the
best total median. The CSV includes every worker count and observed range.

Do not attribute differences from the original snapshot to these correctness
fixes: the filter kernels are unchanged, and neither run controls scheduling,
frequency, filesystem caches, or thermals. Ratios and absolute timings vary.
`process_ms` includes task dispatch and stage waits, whereas `total_ms` also
includes loading, setup, shutdown, and saving. Neither includes full process
startup; the JSON records external subprocess wall time separately.

### Validation

| Executed command | Result |
| --- | --- |
| `make -B -j4 test` | Clean optimized build; six C pool cases and 29 Python tests passed. |
| `make -B sanitize` | Same suite passed under ASan/UBSan with no diagnostics. |
| `setarch x86_64 -R make -B tsan` | Same suite passed under TSan with no race diagnostics. |
| `make -B BUILD_DIR=build/analyzer BIN_DIR=bin/analyzer CFLAGS='-O0 -g -fanalyzer'` | GCC static analysis build passed. |
| Benchmark command above | All 150 warmup/measured hashes matched their sequential references. |

The worker-activity cancellation test also passed when its runner inherited a
blocked SIGUSR1, verifying that it accounts for existing signal masks. Its
Linux `/proc` observation is an integration smoke test, not a synchronization
barrier: a heavily delayed observer can miss the intended interruption window.

These tests do not prove all thread/signal interleavings. Stalled FIFO/device
I/O can defer cooperative cancellation indefinitely, and interruption is not
transactional with final rename. Statistics-output failure does not undo an
already-written image. Exhaustive allocation/pthread/I/O fault
injection and fsync-based crash durability are not implemented. TSan uses the
process-local ASLR workaround shown in its executed command, not a system-wide
configuration change.

## Original Snapshot

The following records describe the pre-audit build and are retained as an
earlier measurement, not as the latest test result or the current binary hash.

### Environment

| Item | Recorded value |
| --- | --- |
| CPU | AMD Ryzen 7 7435HS |
| Visible logical CPUs | 16 |
| OS | Debian Linux through WSL2 |
| Kernel | `6.6.87.2-microsoft-standard-WSL2`, x86_64 |
| C compiler | GCC / `cc` 14.2.0, Debian `14.2.0-19` |
| Normal build | `-O3 -g -std=c11`, strict warnings, `-pthread` |
| Python | 3.13.5, standard library only |
| Benchmark interval | `2026-11-09T07:46:59+00:00` to `2026-11-09T07:47:37+00:00` |
| Input/output filesystem | Linux `/tmp`; project/executable under WSL `/mnt/c` |

The benchmark binary SHA-256 was:

```text
99471413aa7f201a6990f21a9ca0d97ff527e601edef5237e4b23c2edc1ca24c
```

The normal binary was built with the supplied Makefile, not a sanitizer build.
There was no CPU affinity, isolated-core policy, or controlled thermal state.
The other project tests had finished before this performance run.

### Original Command

From the project directory in Linux:

```sh
make
python3 tools/benchmark.py --binary bin/imgpipe \
  --sizes 256,512,1024,2048,4096 --threads 1,2,4,8 \
  --repeats 5 --warmup 1 --filters grayscale,blur,sharpen \
  --queue-capacity 64 --tile-rows 32 \
  --output benchmarks/local_results.csv
```

From PowerShell, the same benchmark command can be launched with:

```powershell
wsl --distribution Debian --cd '/mnt/c/Users/Ullas N/Desktop/LARP/multithreaded-image-pipeline' --exec python3 tools/benchmark.py --binary bin/imgpipe --sizes 256,512,1024,2048,4096 --threads 1,2,4,8 --repeats 5 --warmup 1 --filters grayscale,blur,sharpen --queue-capacity 64 --tile-rows 32 --output benchmarks/local_results.csv
```

Each size has one reference sequential execution, one warmup for each of five
configurations, and five measured executions per configuration. This produced
155 executions: 5 references, 25 warmups, and 125 measured samples. Measured
configurations are shuffled within each round using a recorded deterministic
seed. All 150 warmup/measured outputs matched their size's reference SHA-256.

The 25-row [CSV](local_results.csv) preserves every worker configuration,
minimum/median/maximum timings, speedup, efficiency, and output hash. The
[environment JSON](local_results.environment.json) preserves input hashes,
configuration, all 155 commands and samples, external process wall times, and
the binary hash. Temporary images are regenerated by the tool and cleaned up;
the absolute temporary paths in recorded commands are audit records, not paths
to files that remain on disk.

### Original Eight-Worker Comparison

The table fixes the parallel worker count at eight rather than selecting a
different winner for each size. Values are medians of five measured runs,
rounded for display. The raw artifacts retain more precision.

| Image | Sequential processing ms | 8-worker processing ms | Processing speedup | Sequential total ms | 8-worker total ms | Total speedup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 256x256 | 2.587 | 1.795 | 1.44x | 3.229 | 4.141 | 0.78x |
| 512x512 | 11.407 | 3.634 | 3.14x | 14.022 | 8.864 | 1.58x |
| 1024x1024 | 41.463 | 8.641 | 4.80x | 49.314 | 18.255 | 2.70x |
| 2048x2048 | 160.006 | 31.994 | 5.00x | 189.972 | 63.122 | 3.01x |
| 4096x4096 | 637.970 | 121.744 | 5.24x | 770.893 | 238.529 | 3.23x |

Processing speedup divides the sequential processing median by the parallel
processing median. Total speedup independently divides total medians; it is not
the median of paired ratios. `process_ms` includes all filter dispatch and stage
waits, but excludes loading, pool/scratch setup, worker shutdown, and saving.
`total_ms` includes those phases but not executable startup, argument parsing,
or final buffer freeing. Whole-process wall time is recorded separately in JSON.

### Original Interpretation

- Eight workers reduced filtering time for the largest image by about 5.24x,
  with processing efficiency `5.240262 / 8`, approximately 65.5%.
- Serial I/O and setup/shutdown reduce the largest image's total speedup to
  approximately 3.23x. Processing speedup is not an end-to-end claim.
- At 256x256, eight workers improved processing time but increased total time
  from 3.229 ms to 4.141 ms. Thread count should not be chosen from image size
  or CPU count alone without measuring overhead.
- Four workers had the lowest processing median at 256x256. At 512x512, eight
  had the lowest processing median, but four had the lowest total median.
  The best setting depends on the timing scope.
- Parallel mode with one worker was slightly faster than sequential in some
  larger-image medians. Differences near 1-2%, with overlapping observed
  ranges, are not compelling evidence of an algorithmic improvement.
- Scaling is sublinear. Queue/allocator costs, scheduling, cache behavior,
  and bandwidth are possible contributors, but hardware counters were not
  collected, so this run does not establish which bottleneck dominates.

This is one short, warmed-cache run using a deterministic synthetic RGB pattern.
Five samples and their ranges are useful evidence, not confidence intervals or
a universal scalability guarantee. Different filters, tiles, storage, power
settings, background workloads, or native Linux hardware can change the results.
The Windows-mounted demo's I/O timings are not interchangeable with this
benchmark's Linux `/tmp` timings.

### Original Validation Record

The original snapshot recorded the following successful commands before the
subsequent audit and additional regressions. The current rerun is listed above.

| Command | Result |
| --- | --- |
| `make -j4` | Strict optimized C build passed. |
| `make -j4 test` | Six C pool cases and 25 Python unittest methods passed. |
| `make sanitize` | Same final suite passed with ASan and UBSan; no diagnostics. |
| `setarch x86_64 -R make tsan` | Same final suite passed with TSan; no race diagnostics. |
| `make BUILD_DIR=build/analyzer BIN_DIR=bin/analyzer CFLAGS='-O0 -g -fanalyzer'` | GCC static analyzer build passed. |
| `make demo` | Generated and processed the example image successfully. |
| Benchmark command above | All measured/warmup hashes matched; artifacts published. |

Plain TSan initially failed before `main` with `unexpected memory mapping` on
this WSL configuration. `setarch x86_64 -R` resolved startup for the process tree
without changing system-wide ASLR. The successful race-check command above
includes that workaround; plain `make tsan` is not claimed to work on this host.

The pool suite covers 4,000 tasks from concurrent producers, repeated wait/reuse,
capacity-one backpressure, close/rejection/wakeup/drain, and destroy drainage.
The Python suite includes independent filter references, hand-computed cases,
random tiny/odd images, mixed and repeated stages, parser regressions, argument
validation, existing-output preservation, synchronized FIFO signal checks,
deterministic generation, real small-benchmark audits, and negative tooling cases.

Additional manual resource-limit probes using the 512x512 demo input exercised
worker-creation failure under a 64 MiB address-space limit and pixel-allocation
failure under a 3 MiB limit. Both returned diagnostics without hanging or
publishing `examples/resource-check.ppm`:

```sh
prlimit --as=67108864 ./bin/imgpipe -i examples/input.ppm \
  -o examples/resource-check.ppm --threads 16 --stats
prlimit --as=3145728 ./bin/imgpipe -i examples/input.ppm \
  -o examples/resource-check.ppm --mode sequential --stats
```

Coverage limits remain: automated signal cases synchronize a blocked input read,
not active computation or the write/rename boundary; pool tests separately
exercise accepted-job drainage. Individual allocation failures and every
write/flush/close failure have not been systematically fault-injected. Passing
tests and sanitizers do not prove all possible thread or signal interleavings.

For interview discussion, state the tested environment and distinguish the
implemented guarantees, the observed measurements, and these coverage limits.
