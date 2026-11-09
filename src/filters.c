#include "filters.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    FilterKind filter;
    const Image *source;
    Image *destination;
    size_t first_row;
    size_t end_row;
} FilterTask;

static const int blur_kernel[9] = {1, 1, 1, 1, 1, 1, 1, 1, 1};
static const int sharpen_kernel[9] = {0, -1, 0, -1, 5, -1, 0, -1, 0};
static const int edge_kernel[9] = {-1, -1, -1, -1, 8, -1, -1, -1, -1};

const char *filter_name(FilterKind filter)
{
    switch (filter) {
    case FILTER_GRAYSCALE: return "grayscale";
    case FILTER_BLUR: return "blur";
    case FILTER_SHARPEN: return "sharpen";
    case FILTER_EDGE: return "edge";
    }
    return "unknown";
}

int filter_parse(const char *name, FilterKind *filter)
{
    if (name == NULL || filter == NULL) {
        return EINVAL;
    }
    for (int value = FILTER_GRAYSCALE; value <= FILTER_EDGE; ++value) {
        if (strcmp(name, filter_name((FilterKind)value)) == 0) {
            *filter = (FilterKind)value;
            return 0;
        }
    }
    return EINVAL;
}

static size_t neighbor(size_t coordinate, int offset, size_t limit)
{
    if (offset < 0) {
        return coordinate == 0 ? 0 : coordinate - 1;
    }
    if (offset > 0) {
        return coordinate + 1 < limit ? coordinate + 1 : coordinate;
    }
    return coordinate;
}

static unsigned char clamp_channel(int value)
{
    if (value < 0) {
        return 0;
    }
    if (value > 255) {
        return 255;
    }
    return (unsigned char)value;
}

static void process_rows(const FilterTask *task)
{
    const Image *source = task->source;
    Image *destination = task->destination;
    size_t width = source->width;

    if (task->filter == FILTER_GRAYSCALE) {
        for (size_t row = task->first_row; row < task->end_row; ++row) {
            for (size_t column = 0; column < width; ++column) {
                size_t offset = (row * width + column) * 3;
                int gray = (299 * source->pixels[offset] +
                            587 * source->pixels[offset + 1] +
                            114 * source->pixels[offset + 2] + 500) / 1000;
                destination->pixels[offset] = (unsigned char)gray;
                destination->pixels[offset + 1] = (unsigned char)gray;
                destination->pixels[offset + 2] = (unsigned char)gray;
            }
        }
        return;
    }

    const int *kernel = task->filter == FILTER_BLUR ? blur_kernel :
                        task->filter == FILTER_SHARPEN ? sharpen_kernel : edge_kernel;
    int divisor = task->filter == FILTER_BLUR ? 9 : 1;
    for (size_t row = task->first_row; row < task->end_row; ++row) {
        for (size_t column = 0; column < width; ++column) {
            int red = 0;
            int green = 0;
            int blue = 0;
            for (int dy = -1; dy <= 1; ++dy) {
                size_t sample_row = neighbor(row, dy, source->height);
                for (int dx = -1; dx <= 1; ++dx) {
                    int weight = kernel[(dy + 1) * 3 + dx + 1];
                    if (weight == 0) {
                        continue;
                    }
                    size_t sample_column = neighbor(column, dx, width);
                    size_t sample = (sample_row * width + sample_column) * 3;
                    red += weight * source->pixels[sample];
                    green += weight * source->pixels[sample + 1];
                    blue += weight * source->pixels[sample + 2];
                }
            }
            size_t output = (row * width + column) * 3;
            destination->pixels[output] = clamp_channel((red + divisor / 2) / divisor);
            destination->pixels[output + 1] = clamp_channel((green + divisor / 2) / divisor);
            destination->pixels[output + 2] = clamp_channel((blue + divisor / 2) / divisor);
        }
    }
}

static void run_filter_task(void *argument)
{
    FilterTask *task = argument;
    process_rows(task);
    free(task);
}

int filter_apply(FilterKind filter, const Image *source, Image *destination,
                 ThreadPool *pool, size_t tile_rows,
                 const volatile sig_atomic_t *cancel_signal)
{
    if (source == NULL || destination == NULL || source->pixels == NULL ||
        destination->pixels == NULL || source->pixels == destination->pixels ||
        source->width != destination->width || source->height != destination->height ||
        source->width == 0 || source->height == 0 || tile_rows == 0 ||
        filter < FILTER_GRAYSCALE || filter > FILTER_EDGE) {
        return EINVAL;
    }

    int error = 0;
    for (size_t row = 0; row < source->height;) {
        if (cancel_signal != NULL && *cancel_signal != 0) {
            error = ECANCELED;
            break;
        }
        size_t end = tile_rows > source->height - row ? source->height : row + tile_rows;
        FilterTask task = {filter, source, destination, row, end};
        if (pool == NULL) {
            process_rows(&task);
        } else {
            FilterTask *owned_task = malloc(sizeof(*owned_task));
            if (owned_task == NULL) {
                error = ENOMEM;
                break;
            }
            *owned_task = task;
            error = thread_pool_submit(pool, run_filter_task, owned_task);
            if (error != 0) {
                free(owned_task);
                break;
            }
        }
        row = end;
    }

    /* Drain before the caller swaps/frees either image or returns on an error. */
    thread_pool_wait(pool);
    if (error == 0 && cancel_signal != NULL && *cancel_signal != 0) {
        error = ECANCELED;
    }
    return error;
}
