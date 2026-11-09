#ifndef IMAGE_H
#define IMAGE_H

#include <signal.h>
#include <stddef.h>

#define IMAGE_MAX_PIXELS ((size_t)100000000)

typedef struct {
    size_t width;
    size_t height;
    unsigned char *pixels;
} Image;

/* Images passed to create/read must be zero-initialized and not own pixels. */
int image_create(Image *image, size_t width, size_t height,
                 char *error, size_t error_size);
int image_read_ppm(const char *path, Image *image, char *error, size_t error_size);
int image_write_ppm_atomic(const char *path, const Image *image,
                          const volatile sig_atomic_t *cancel_signal,
                          char *error, size_t error_size);
void image_destroy(Image *image);

#endif
