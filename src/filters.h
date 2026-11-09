#ifndef FILTERS_H
#define FILTERS_H

#include "image.h"
#include "thread_pool.h"

typedef enum {
    FILTER_GRAYSCALE,
    FILTER_BLUR,
    FILTER_SHARPEN,
    FILTER_EDGE
} FilterKind;

const char *filter_name(FilterKind filter);
int filter_parse(const char *name, FilterKind *filter);

/* NULL pool selects sequential execution. Source and destination cannot alias.
 * Only the producer reads cancel_signal; accepted jobs finish before return.
 * Return zero on success or a positive errno code on failure/interruption. */
int filter_apply(FilterKind filter, const Image *source, Image *destination,
                 ThreadPool *pool, size_t tile_rows,
                 const volatile sig_atomic_t *cancel_signal);

#endif
