#include "image.h"

#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int report_error(char *error, size_t error_size, int code,
                        const char *format, ...)
{
    if (error != NULL && error_size != 0) {
        va_list arguments;
        va_start(arguments, format);
        (void)vsnprintf(error, error_size, format, arguments);
        va_end(arguments);
    }
    return code;
}

/* Consume a token's delimiter, but never skip ahead into a P6 raster. */
static int read_token(FILE *file, char *token, size_t capacity, int *delimiter)
{
    int character = fgetc(file);
    for (;;) {
        while (character != EOF && isspace((unsigned char)character)) {
            character = fgetc(file);
        }
        if (character != '#') {
            break;
        }
        while (character != EOF && character != '\n' && character != '\r') {
            character = fgetc(file);
        }
    }
    if (character == EOF) {
        return ferror(file) ? -1 : 0;
    }

    size_t length = 0;
    while (character != EOF && !isspace((unsigned char)character) && character != '#') {
        if (character == '\0' || length + 1 >= capacity) {
            return -1;
        }
        token[length++] = (char)character;
        character = fgetc(file);
    }
    if (character == '#') {
        while (character != EOF && character != '\n' && character != '\r') {
            character = fgetc(file);
        }
    }
    if (ferror(file)) {
        return -1;
    }
    token[length] = '\0';
    *delimiter = character;
    return 1;
}

static int read_unsigned(FILE *file, size_t *value, int *delimiter,
                         const char *field, char *error, size_t error_size)
{
    char token[64];
    int status = read_token(file, token, sizeof(token), delimiter);
    if (status <= 0) {
        return report_error(error, error_size, ferror(file) ? EIO : EINVAL,
                            "%s %s in PPM", status == 0 ? "missing" : "invalid", field);
    }
    size_t number = 0;
    for (size_t index = 0; token[index] != '\0'; ++index) {
        if (token[index] < '0' || token[index] > '9') {
            return report_error(error, error_size, EINVAL,
                                "invalid %s: expected an unsigned integer", field);
        }
        unsigned int digit = (unsigned int)(token[index] - '0');
        if (number > (SIZE_MAX - digit) / 10) {
            return report_error(error, error_size, EOVERFLOW, "%s overflows size_t", field);
        }
        number = number * 10 + digit;
    }
    *value = number;
    return 0;
}

int image_create(Image *image, size_t width, size_t height,
                 char *error, size_t error_size)
{
    if (image == NULL || image->pixels != NULL || width == 0 || height == 0) {
        return report_error(error, error_size, EINVAL,
                            "image needs positive dimensions and an empty destination");
    }
    if (width > SIZE_MAX / height || width * height > SIZE_MAX / 3) {
        return report_error(error, error_size, EOVERFLOW, "image dimensions overflow storage");
    }
    size_t pixels = width * height;
    if (pixels > IMAGE_MAX_PIXELS) {
        return report_error(error, error_size, EFBIG,
                            "image exceeds the %zu-pixel safety limit", IMAGE_MAX_PIXELS);
    }
    unsigned char *storage = malloc(pixels * 3);
    if (storage == NULL) {
        return report_error(error, error_size, ENOMEM, "cannot allocate image pixels");
    }
    *image = (Image){width, height, storage};
    return 0;
}

void image_destroy(Image *image)
{
    if (image != NULL) {
        free(image->pixels);
        *image = (Image){0};
    }
}

int image_read_ppm(const char *path, Image *image, char *error, size_t error_size)
{
    if (path == NULL || image == NULL || image->pixels != NULL) {
        return report_error(error, error_size, EINVAL, "invalid PPM input arguments");
    }
    FILE *file = fopen(path, "rb");
    if (file == NULL) {
        int code = errno;
        return report_error(error, error_size, code, "cannot open input '%s': %s",
                            path, strerror(code));
    }

    Image loaded = {0};
    int result = 0;
    int delimiter = EOF;
    char token[64];
    if (read_token(file, token, sizeof(token), &delimiter) != 1 ||
        (strcmp(token, "P3") != 0 && strcmp(token, "P6") != 0)) {
        result = report_error(error, error_size, EINVAL, "input must be a P3 or P6 PPM image");
        goto fail;
    }
    int binary = strcmp(token, "P6") == 0;
    size_t width = 0;
    size_t height = 0;
    size_t maximum = 0;
    result = read_unsigned(file, &width, &delimiter, "width", error, error_size);
    if (result != 0) {
        goto fail;
    }
    result = read_unsigned(file, &height, &delimiter, "height", error, error_size);
    if (result != 0) {
        goto fail;
    }
    result = read_unsigned(file, &maximum, &delimiter, "maxval", error, error_size);
    if (result != 0) {
        goto fail;
    }
    if (maximum == 0 || maximum > 255) {
        result = report_error(error, error_size, EINVAL,
                              "only 8-bit PPM maxval values from 1 to 255 are supported");
        goto fail;
    }
    if (binary && (delimiter == EOF || !isspace((unsigned char)delimiter))) {
        result = report_error(error, error_size, EINVAL, "P6 maxval needs a whitespace delimiter");
        goto fail;
    }
    result = image_create(&loaded, width, height, error, error_size);
    if (result != 0) {
        goto fail;
    }
    size_t bytes = width * height * 3;
    if (binary) {
        if (fread(loaded.pixels, 1, bytes, file) != bytes) {
            result = report_error(error, error_size, ferror(file) ? EIO : EINVAL,
                                  "truncated or unreadable P6 pixel data");
            goto fail;
        }
        int trailing = fgetc(file);
        /* Only interpret LF as part of CRLF when a complete raster follows it.
           With exactly 'bytes' remaining, LF is the first sample after bare CR. */
        if (delimiter == '\r' && loaded.pixels[0] == '\n' && trailing != EOF) {
            (void)memmove(loaded.pixels, loaded.pixels + 1, bytes - 1);
            loaded.pixels[bytes - 1] = (unsigned char)trailing;
            trailing = fgetc(file);
        }
        if (trailing != EOF) {
            result = report_error(error, error_size, EINVAL,
                                  "unexpected data after the single P6 image");
            goto fail;
        }
        if (ferror(file)) {
            result = report_error(error, error_size, EIO, "error reading P6 input");
            goto fail;
        }
        if (maximum != 255) {
            for (size_t index = 0; index < bytes; ++index) {
                unsigned int sample = loaded.pixels[index];
                if (sample > maximum) {
                    result = report_error(error, error_size, EINVAL, "P6 sample exceeds maxval");
                    goto fail;
                }
                loaded.pixels[index] = (unsigned char)((sample * 255 + maximum / 2) / maximum);
            }
        }
    } else {
        for (size_t index = 0; index < bytes; ++index) {
            size_t sample = 0;
            result = read_unsigned(file, &sample, &delimiter, "sample", error, error_size);
            if (result != 0) {
                goto fail;
            }
            if (sample > maximum) {
                result = report_error(error, error_size, EINVAL, "P3 sample exceeds maxval");
                goto fail;
            }
            loaded.pixels[index] = (unsigned char)((sample * 255 + maximum / 2) / maximum);
        }
        int status = read_token(file, token, sizeof(token), &delimiter);
        if (status != 0) {
            result = report_error(error, error_size, ferror(file) ? EIO : EINVAL,
                                  "unexpected data after the single P3 image");
            goto fail;
        }
    }

    if (fclose(file) != 0) {
        int code = errno != 0 ? errno : EIO;
        image_destroy(&loaded);
        return report_error(error, error_size, code, "cannot close input: %s", strerror(code));
    }
    *image = loaded;
    return 0;

fail:
    (void)fclose(file);
    image_destroy(&loaded);
    return result;
}

int image_write_ppm_atomic(const char *path, const Image *image,
                          const volatile sig_atomic_t *cancel_signal,
                          char *error, size_t error_size)
{
    if (path == NULL || image == NULL || image->pixels == NULL ||
        image->width == 0 || image->height == 0 ||
        image->width > IMAGE_MAX_PIXELS / image->height) {
        return report_error(error, error_size, EINVAL, "invalid PPM output arguments");
    }
    if (cancel_signal != NULL && *cancel_signal != 0) {
        return report_error(error, error_size, ECANCELED, "output cancelled");
    }

    char temporary_name[] = ".imgpipe.tmp.XXXXXX";
    const char *separator = strrchr(path, '/');
    size_t directory_length = separator == NULL ? 0 : (size_t)(separator - path) + 1;
    /* A distinct first character prevents mkstemp from creating the destination
       itself if its name happens to match the randomly generated basename. */
    if (path[directory_length] == '.') {
        temporary_name[0] = '_';
    }
    if (directory_length > SIZE_MAX - sizeof(temporary_name)) {
        return report_error(error, error_size, ENAMETOOLONG, "output path is too long");
    }
    char *temporary = malloc(directory_length + sizeof(temporary_name));
    if (temporary == NULL) {
        return report_error(error, error_size, ENOMEM, "cannot allocate temporary output path");
    }
    (void)memcpy(temporary, path, directory_length);
    (void)memcpy(temporary + directory_length, temporary_name, sizeof(temporary_name));
    int descriptor = mkstemp(temporary);
    if (descriptor < 0) {
        int code = errno;
        free(temporary);
        return report_error(error, error_size, code, "cannot create output near '%s': %s",
                            path, strerror(code));
    }
    FILE *file = fdopen(descriptor, "wb");
    if (file == NULL) {
        int code = errno;
        (void)close(descriptor);
        (void)unlink(temporary);
        free(temporary);
        return report_error(error, error_size, code, "cannot open temporary output: %s", strerror(code));
    }

    int result = 0;
    size_t bytes = image->width * image->height * 3;
    if (fprintf(file, "P6\n%zu %zu\n255\n", image->width, image->height) < 0 ||
        fwrite(image->pixels, 1, bytes, file) != bytes || fflush(file) != 0) {
        int code = errno != 0 ? errno : EIO;
        result = report_error(error, error_size, code, "cannot write output: %s", strerror(code));
    }
    if (fclose(file) != 0 && result == 0) {
        int code = errno != 0 ? errno : EIO;
        result = report_error(error, error_size, code, "cannot close output: %s", strerror(code));
    }
    if (result == 0 && cancel_signal != NULL && *cancel_signal != 0) {
        result = report_error(error, error_size, ECANCELED, "output cancelled before publication");
    }
    if (result == 0 && rename(temporary, path) != 0) {
        int code = errno;
        result = report_error(error, error_size, code, "cannot publish output '%s': %s",
                              path, strerror(code));
    }
    if (result != 0) {
        (void)unlink(temporary);
    }
    free(temporary);
    return result;
}
