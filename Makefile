CC = cc
PYTHON ?= python3
CFLAGS ?= -O3 -g
CPPFLAGS += -D_POSIX_C_SOURCE=200809L -Isrc
WARNINGS = -std=c11 -Wall -Wextra -Wpedantic -Werror -Wshadow -Wstrict-prototypes
LDLIBS += -pthread
BUILD_DIR ?= build
BIN_DIR ?= bin

SOURCES = src/main.c src/image.c src/filters.c src/thread_pool.c
OBJECTS = $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SOURCES))
TARGET = $(BIN_DIR)/imgpipe
POOL_TEST = $(BIN_DIR)/test_thread_pool

.PHONY: all test sanitize tsan benchmark demo clean

all: $(TARGET)

$(TARGET): $(OBJECTS) | $(BIN_DIR)
	$(CC) $(LDFLAGS) $^ $(LDLIBS) -o $@

$(BUILD_DIR)/%.o: src/%.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARNINGS) -pthread -MMD -MP -c $< -o $@

$(POOL_TEST): tests/test_thread_pool.c src/thread_pool.h $(BUILD_DIR)/thread_pool.o | $(BIN_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARNINGS) -pthread $(LDFLAGS) tests/test_thread_pool.c $(BUILD_DIR)/thread_pool.o $(LDLIBS) -o $@

$(BUILD_DIR) $(BIN_DIR):
	mkdir -p "$@"

test: $(TARGET) $(POOL_TEST)
	$(POOL_TEST)
	$(PYTHON) tests/test_cli.py --binary "$(TARGET)"

sanitize:
	$(MAKE) BUILD_DIR=build/asan BIN_DIR=bin/asan CFLAGS="-O1 -g -fno-omit-frame-pointer -fno-pie -fsanitize=address,undefined" LDFLAGS="-no-pie -fsanitize=address,undefined" test

tsan:
	$(MAKE) BUILD_DIR=build/tsan BIN_DIR=bin/tsan CFLAGS="-O1 -g -fno-omit-frame-pointer -fno-pie -fsanitize=thread" LDFLAGS="-no-pie -fsanitize=thread" test

benchmark: $(TARGET)
	$(PYTHON) tools/benchmark.py --binary "$(TARGET)" --output benchmarks/results.csv

demo: $(TARGET)
	mkdir -p examples
	$(PYTHON) tools/generate_ppm.py --width 512 --height 512 --output examples/input.ppm
	$(TARGET) --input examples/input.ppm --output examples/output.ppm --filters grayscale,blur,sharpen --threads 4 --stats

clean:
	rm -rf build bin

-include $(OBJECTS:.o=.d)
