#include "filters.h"
#include "image.h"
#include "thread_pool.h"

#include <errno.h>
#include <getopt.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_FILTERS 16
#define MAX_THREADS ((size_t)256)
#define MAX_QUEUE_CAPACITY ((size_t)65536)
#define MAX_TILE_ROWS ((size_t)1048576)

typedef struct {
    const char *input;
    const char *output;
    FilterKind filters[MAX_FILTERS];
    size_t filter_count;
    size_t threads;
    size_t queue_capacity;
    size_t tile_rows;
    int parallel;
    int stats;
} Options;

typedef struct {
    double load_ms;
    double setup_ms;
    double process_ms;
    double shutdown_ms;
    double save_ms;
    double total_ms;
    double stage_ms[MAX_FILTERS];
} RunStats;

/* Workers inherit blocked termination signals, so only main accesses this. */
static volatile sig_atomic_t cancel_signal = 0;

static void handle_signal(int number)
{
    if (cancel_signal == 0) {
        cancel_signal = number;
    }
}

static void usage(FILE *stream)
{
    (void)fprintf(stream,
        "Usage: imgpipe --input INPUT.ppm --output OUTPUT.ppm [options]\n"
        "\n"
        "  -i, --input PATH          P3/P6 image, maxval 1..255\n"
        "  -o, --output PATH         Atomic P6 output (may equal input)\n"
        "  -f, --filters LIST        Ordered comma-separated stages, at most 16\n"
        "                            grayscale, blur, sharpen, edge\n"
        "                            Default: grayscale,blur\n"
        "  -m, --mode MODE           sequential or parallel (default: parallel)\n"
        "  -t, --threads N           Workers: 1..256 (default: CPUs, capped at 32)\n"
        "  -q, --queue-capacity N    Queued tasks: 1..65536 (default: 64)\n"
        "  -r, --tile-rows N         Rows per task: 1..1048576 (default: 32)\n"
        "      --stats              Print JSON timings after successful output\n"
        "  -h, --help               Show this help\n"
        "\n"
        "Sequential mode ignores threads/queue settings. SIGINT/SIGTERM stop\n"
        "new work cooperatively and drain accepted tasks before cleanup.\n");
}

static int flush_stdout(void)
{
    if (fflush(stdout) != 0 || ferror(stdout)) {
        int error = errno != 0 ? errno : EIO;
        (void)fprintf(stderr, "imgpipe: cannot write stdout: %s\n", strerror(error));
        return 1;
    }
    return 0;
}

static int parse_positive(const char *text, size_t maximum, size_t *result)
{
    if (text == NULL || text[0] == '\0') {
        return EINVAL;
    }
    size_t value = 0;
    for (size_t index = 0; text[index] != '\0'; ++index) {
        if (text[index] < '0' || text[index] > '9') {
            return EINVAL;
        }
        unsigned int digit = (unsigned int)(text[index] - '0');
        if (value > (SIZE_MAX - digit) / 10) {
            return EOVERFLOW;
        }
        value = value * 10 + digit;
    }
    if (value == 0 || value > maximum) {
        return EINVAL;
    }
    *result = value;
    return 0;
}

static int parse_filter_list(const char *text, Options *options)
{
    size_t count = 0;
    const char *cursor = text;
    for (;;) {
        size_t length = strcspn(cursor, ",");
        char name[32];
        if (length == 0 || length >= sizeof(name) || count == MAX_FILTERS) {
            return EINVAL;
        }
        (void)memcpy(name, cursor, length);
        name[length] = '\0';
        if (filter_parse(name, &options->filters[count]) != 0) {
            return EINVAL;
        }
        ++count;
        cursor += length;
        if (*cursor == '\0') {
            break;
        }
        ++cursor;
    }
    options->filter_count = count;
    return 0;
}

/* Return 0 to run, 1 after help, or 2 on a usage error. */
static int parse_options(int argc, char **argv, Options *options)
{
    long cpus = sysconf(_SC_NPROCESSORS_ONLN);
    *options = (Options){
        .filters = {FILTER_GRAYSCALE, FILTER_BLUR},
        .filter_count = 2,
        .threads = cpus < 1 ? 1 : cpus > 32 ? 32 : (size_t)cpus,
        .queue_capacity = 64,
        .tile_rows = 32,
        .parallel = 1
    };
    enum { OPT_STATS = 256 };
    const struct option long_options[] = {
        {"input", required_argument, NULL, 'i'},
        {"output", required_argument, NULL, 'o'},
        {"filters", required_argument, NULL, 'f'},
        {"mode", required_argument, NULL, 'm'},
        {"threads", required_argument, NULL, 't'},
        {"queue-capacity", required_argument, NULL, 'q'},
        {"tile-rows", required_argument, NULL, 'r'},
        {"stats", no_argument, NULL, OPT_STATS},
        {"help", no_argument, NULL, 'h'},
        {NULL, 0, NULL, 0}
    };
    opterr = 0;
    int option;
    while ((option = getopt_long(argc, argv, "i:o:f:m:t:q:r:h", long_options, NULL)) != -1) {
        switch (option) {
        case 'i': options->input = optarg; break;
        case 'o': options->output = optarg; break;
        case 'f':
            if (parse_filter_list(optarg, options) != 0) {
                (void)fprintf(stderr, "imgpipe: invalid filter list; use 1..16 of grayscale,blur,sharpen,edge\n");
                return 2;
            }
            break;
        case 'm':
            if (strcmp(optarg, "parallel") == 0) {
                options->parallel = 1;
            } else if (strcmp(optarg, "sequential") == 0) {
                options->parallel = 0;
            } else {
                (void)fprintf(stderr, "imgpipe: mode must be sequential or parallel\n");
                return 2;
            }
            break;
        case 't':
        case 'q':
        case 'r': {
            size_t *destination = option == 't' ? &options->threads :
                                  option == 'q' ? &options->queue_capacity : &options->tile_rows;
            size_t maximum = option == 't' ? MAX_THREADS :
                             option == 'q' ? MAX_QUEUE_CAPACITY : MAX_TILE_ROWS;
            if (parse_positive(optarg, maximum, destination) != 0) {
                (void)fprintf(stderr, "imgpipe: -%c needs an integer from 1 to %zu\n", option, maximum);
                return 2;
            }
            break;
        }
        case OPT_STATS: options->stats = 1; break;
        case 'h': usage(stdout); return 1;
        default:
            (void)fprintf(stderr, "imgpipe: unknown option or missing argument; use --help\n");
            return 2;
        }
    }
    if (optind != argc || options->input == NULL || options->output == NULL ||
        options->input[0] == '\0' || options->output[0] == '\0') {
        (void)fprintf(stderr, "imgpipe: nonempty --input and --output are required; no positional arguments\n");
        return 2;
    }
    return 0;
}

static double monotonic_seconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        perror("imgpipe: clock_gettime");
        exit(EXIT_FAILURE);
    }
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static int install_signal_handlers(void)
{
    struct sigaction action;
    (void)memset(&action, 0, sizeof(action));
    action.sa_handler = handle_signal;
    action.sa_flags = SA_RESTART;
    if (sigemptyset(&action.sa_mask) != 0 ||
        sigaddset(&action.sa_mask, SIGINT) != 0 ||
        sigaddset(&action.sa_mask, SIGTERM) != 0 ||
        sigaction(SIGINT, &action, NULL) != 0 ||
        sigaction(SIGTERM, &action, NULL) != 0) {
        return errno;
    }
    return 0;
}

static int create_worker_pool(const Options *options, ThreadPool **pool)
{
    sigset_t blocked;
    sigset_t previous;
    if (sigemptyset(&blocked) != 0 || sigaddset(&blocked, SIGINT) != 0 ||
        sigaddset(&blocked, SIGTERM) != 0) {
        return errno;
    }
    int error = pthread_sigmask(SIG_BLOCK, &blocked, &previous);
    if (error != 0) {
        return error;
    }
    error = thread_pool_create(pool, options->threads, options->queue_capacity);
    int restore_error = pthread_sigmask(SIG_SETMASK, &previous, NULL);
    if (error == 0 && restore_error != 0) {
        thread_pool_destroy(*pool);
        *pool = NULL;
        return restore_error;
    }
    return error;
}

static void print_stats(const Options *options, const Image *image, const RunStats *stats)
{
    (void)printf("{\"mode\":\"%s\",\"width\":%zu,\"height\":%zu,\"filters\":[",
                 options->parallel ? "parallel" : "sequential", image->width, image->height);
    for (size_t index = 0; index < options->filter_count; ++index) {
        (void)printf("%s\"%s\"", index == 0 ? "" : ",", filter_name(options->filters[index]));
    }
    (void)printf("],\"threads\":%zu,\"queue_capacity\":%zu,\"tile_rows\":%zu,"
                 "\"load_ms\":%.6f,\"setup_ms\":%.6f,\"process_ms\":%.6f,"
                 "\"shutdown_ms\":%.6f,\"save_ms\":%.6f,\"total_ms\":%.6f,\"stage_ms\":[",
                 options->parallel ? options->threads : (size_t)1,
                 options->parallel ? options->queue_capacity : (size_t)0, options->tile_rows,
                 stats->load_ms, stats->setup_ms, stats->process_ms, stats->shutdown_ms,
                 stats->save_ms, stats->total_ms);
    for (size_t index = 0; index < options->filter_count; ++index) {
        (void)printf("%s%.6f", index == 0 ? "" : ",", stats->stage_ms[index]);
    }
    (void)printf("]}\n");
}

int main(int argc, char **argv)
{
    Options options;
    int parsed = parse_options(argc, argv, &options);
    if (parsed != 0) {
        return parsed == 1 ? flush_stdout() : parsed;
    }
    int error = install_signal_handlers();
    if (error != 0) {
        (void)fprintf(stderr, "imgpipe: cannot install signal handlers: %s\n", strerror(error));
        return 1;
    }

    Image image = {0};
    Image scratch = {0};
    ThreadPool *pool = NULL;
    RunStats stats = {0};
    char message[256] = {0};
    int status = 1;
    double total_start = monotonic_seconds();
    double phase_start = total_start;
    error = image_read_ppm(options.input, &image, message, sizeof(message));
    if (error != 0) {
        if (cancel_signal != 0) {
            goto interrupted;
        }
        (void)fprintf(stderr, "imgpipe: %s\n", message);
        goto cleanup;
    }
    double phase_end = monotonic_seconds();
    stats.load_ms = (phase_end - phase_start) * 1000.0;
    if (cancel_signal != 0) {
        goto interrupted;
    }

    phase_start = phase_end;
    error = image_create(&scratch, image.width, image.height, message, sizeof(message));
    if (error != 0) {
        (void)fprintf(stderr, "imgpipe: %s\n", message);
        goto cleanup;
    }
    if (options.parallel) {
        error = create_worker_pool(&options, &pool);
        if (error != 0) {
            (void)fprintf(stderr, "imgpipe: cannot create worker pool: %s\n", strerror(error));
            goto cleanup;
        }
    }
    phase_end = monotonic_seconds();
    stats.setup_ms = (phase_end - phase_start) * 1000.0;

    phase_start = phase_end;
    for (size_t index = 0; index < options.filter_count; ++index) {
        double stage_start = monotonic_seconds();
        error = filter_apply(options.filters[index], &image, &scratch, pool,
                             options.tile_rows, &cancel_signal);
        stats.stage_ms[index] = (monotonic_seconds() - stage_start) * 1000.0;
        if (error != 0) {
            if (cancel_signal != 0) {
                goto interrupted;
            }
            (void)fprintf(stderr, "imgpipe: %s stage failed: %s\n",
                          filter_name(options.filters[index]), strerror(error));
            goto cleanup;
        }
        Image previous = image;
        image = scratch;
        scratch = previous;
    }
    phase_end = monotonic_seconds();
    stats.process_ms = (phase_end - phase_start) * 1000.0;

    phase_start = phase_end;
    thread_pool_destroy(pool);
    pool = NULL;
    phase_end = monotonic_seconds();
    stats.shutdown_ms = (phase_end - phase_start) * 1000.0;
    if (cancel_signal != 0) {
        goto interrupted;
    }

    phase_start = phase_end;
    error = image_write_ppm_atomic(options.output, &image, &cancel_signal, message, sizeof(message));
    if (error != 0) {
        if (cancel_signal != 0) {
            goto interrupted;
        }
        (void)fprintf(stderr, "imgpipe: %s\n", message);
        goto cleanup;
    }
    phase_end = monotonic_seconds();
    stats.save_ms = (phase_end - phase_start) * 1000.0;
    stats.total_ms = (phase_end - total_start) * 1000.0;
    if (cancel_signal != 0) {
        goto interrupted;
    }
    if (options.stats) {
        print_stats(&options, &image, &stats);
        if (flush_stdout() != 0) {
            goto cleanup;
        }
    }
    status = 0;
    goto cleanup;

interrupted:
    (void)fprintf(stderr, "imgpipe: interrupted by signal %d; accepted tasks drained\n", (int)cancel_signal);
    status = 128 + (int)cancel_signal;
cleanup:
    thread_pool_destroy(pool);
    image_destroy(&scratch);
    image_destroy(&image);
    return status;
}
