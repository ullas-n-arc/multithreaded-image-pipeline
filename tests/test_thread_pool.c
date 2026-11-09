#include "thread_pool.h"

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHECK(expression)                                                     \
    do {                                                                      \
        if (!(expression)) {                                                  \
            fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__,  \
                    #expression);                                             \
            exit(EXIT_FAILURE);                                               \
        }                                                                     \
    } while (0)

#define CHECK_PTHREAD(expression)                                             \
    do {                                                                      \
        int check_status = (expression);                                      \
        if (check_status != 0) {                                              \
            fprintf(stderr, "%s:%d: %s: %s (%d)\n", __FILE__, __LINE__,       \
                    #expression, strerror(check_status), check_status);       \
            exit(EXIT_FAILURE);                                               \
        }                                                                     \
    } while (0)

typedef struct {
    pthread_mutex_t mutex;
    unsigned int *hits;
} Record;

typedef struct {
    Record *record;
    size_t index;
} RecordJob;

typedef struct {
    ThreadPool *pool;
    RecordJob *jobs;
    size_t count;
    int result;
} SubmitBatch;

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    bool entered;
    bool released;
} Gate;

typedef struct {
    ThreadPool *pool;
    RecordJob *job;
    pthread_mutex_t mutex;
    pthread_cond_t condition;
    bool started;
    bool finished;
    int result;
} SubmitProbe;

static void record_task(void *argument)
{
    RecordJob *job = argument;
    CHECK_PTHREAD(pthread_mutex_lock(&job->record->mutex));
    ++job->record->hits[job->index];
    CHECK_PTHREAD(pthread_mutex_unlock(&job->record->mutex));
}

static void *submit_batch(void *argument)
{
    SubmitBatch *batch = argument;
    for (size_t index = 0; index < batch->count; ++index) {
        batch->result = thread_pool_submit(batch->pool, record_task,
                                           &batch->jobs[index]);
        if (batch->result != 0) {
            break;
        }
    }
    return NULL;
}

static void gated_task(void *argument)
{
    Gate *gate = argument;
    CHECK_PTHREAD(pthread_mutex_lock(&gate->mutex));
    gate->entered = true;
    CHECK_PTHREAD(pthread_cond_broadcast(&gate->condition));
    while (!gate->released) {
        CHECK_PTHREAD(pthread_cond_wait(&gate->condition, &gate->mutex));
    }
    CHECK_PTHREAD(pthread_mutex_unlock(&gate->mutex));
}

static void wait_for_gate(Gate *gate)
{
    CHECK_PTHREAD(pthread_mutex_lock(&gate->mutex));
    while (!gate->entered) {
        CHECK_PTHREAD(pthread_cond_wait(&gate->condition, &gate->mutex));
    }
    CHECK_PTHREAD(pthread_mutex_unlock(&gate->mutex));
}

static void release_gate(Gate *gate)
{
    CHECK_PTHREAD(pthread_mutex_lock(&gate->mutex));
    gate->released = true;
    CHECK_PTHREAD(pthread_cond_broadcast(&gate->condition));
    CHECK_PTHREAD(pthread_mutex_unlock(&gate->mutex));
}

static void destroy_gate(Gate *gate)
{
    CHECK_PTHREAD(pthread_cond_destroy(&gate->condition));
    CHECK_PTHREAD(pthread_mutex_destroy(&gate->mutex));
}

static void init_probe(SubmitProbe *probe, ThreadPool *pool, RecordJob *job)
{
    probe->pool = pool;
    probe->job = job;
    probe->started = false;
    probe->finished = false;
    probe->result = 0;
    CHECK_PTHREAD(pthread_mutex_init(&probe->mutex, NULL));
    CHECK_PTHREAD(pthread_cond_init(&probe->condition, NULL));
}

static void *submit_probe(void *argument)
{
    SubmitProbe *probe = argument;
    CHECK_PTHREAD(pthread_mutex_lock(&probe->mutex));
    probe->started = true;
    CHECK_PTHREAD(pthread_cond_broadcast(&probe->condition));
    CHECK_PTHREAD(pthread_mutex_unlock(&probe->mutex));

    int result = thread_pool_submit(probe->pool, record_task, probe->job);

    CHECK_PTHREAD(pthread_mutex_lock(&probe->mutex));
    probe->result = result;
    probe->finished = true;
    CHECK_PTHREAD(pthread_cond_broadcast(&probe->condition));
    CHECK_PTHREAD(pthread_mutex_unlock(&probe->mutex));
    return NULL;
}

static void check_probe_started(SubmitProbe *probe)
{
    CHECK_PTHREAD(pthread_mutex_lock(&probe->mutex));
    while (!probe->started) {
        CHECK_PTHREAD(pthread_cond_wait(&probe->condition, &probe->mutex));
    }
    CHECK(!probe->finished);
    CHECK_PTHREAD(pthread_mutex_unlock(&probe->mutex));
}

static void destroy_probe(SubmitProbe *probe)
{
    CHECK_PTHREAD(pthread_cond_destroy(&probe->condition));
    CHECK_PTHREAD(pthread_mutex_destroy(&probe->mutex));
}

static void test_invalid_create(void)
{
    ThreadPool *pool = NULL;
    CHECK(thread_pool_create(NULL, 1, 1) == EINVAL);
    CHECK(thread_pool_create(&pool, 0, 1) == EINVAL);
    CHECK(thread_pool_create(&pool, 1, 0) == EINVAL);
    CHECK(thread_pool_create(&pool, 0, 0) == EINVAL);
}

static void test_execution_once(void)
{
    enum { PRODUCERS = 4, JOBS_PER_PRODUCER = 1000,
           JOB_COUNT = PRODUCERS * JOBS_PER_PRODUCER };
    unsigned int hits[JOB_COUNT] = {0};
    Record record = {PTHREAD_MUTEX_INITIALIZER, hits};
    RecordJob jobs[JOB_COUNT];
    SubmitBatch batches[PRODUCERS];
    pthread_t producers[PRODUCERS];
    ThreadPool *pool = NULL;

    CHECK(thread_pool_create(&pool, 4, 7) == 0);
    for (size_t index = 0; index < JOB_COUNT; ++index) {
        jobs[index] = (RecordJob){&record, index};
    }
    for (size_t index = 0; index < PRODUCERS; ++index) {
        batches[index] = (SubmitBatch){pool, &jobs[index * JOBS_PER_PRODUCER],
                                      JOBS_PER_PRODUCER, 0};
        CHECK_PTHREAD(pthread_create(&producers[index], NULL, submit_batch,
                                     &batches[index]));
    }
    for (size_t index = 0; index < PRODUCERS; ++index) {
        CHECK_PTHREAD(pthread_join(producers[index], NULL));
        CHECK(batches[index].result == 0);
    }
    thread_pool_wait(pool);
    for (size_t index = 0; index < JOB_COUNT; ++index) {
        CHECK(hits[index] == 1);
    }
    thread_pool_destroy(pool);
    CHECK_PTHREAD(pthread_mutex_destroy(&record.mutex));
}

static void test_wait_and_reuse(void)
{
    enum { JOB_COUNT = 97, ROUNDS = 5 };
    unsigned int hits[JOB_COUNT] = {0};
    Record record = {PTHREAD_MUTEX_INITIALIZER, hits};
    RecordJob jobs[JOB_COUNT];
    ThreadPool *pool = NULL;

    CHECK(thread_pool_create(&pool, 3, 1) == 0);
    for (size_t index = 0; index < JOB_COUNT; ++index) {
        jobs[index] = (RecordJob){&record, index};
    }
    thread_pool_wait(pool);
    for (unsigned int round = 1; round <= ROUNDS; ++round) {
        for (size_t index = 0; index < JOB_COUNT; ++index) {
            CHECK(thread_pool_submit(pool, record_task, &jobs[index]) == 0);
        }
        thread_pool_wait(pool);
        thread_pool_wait(pool);
        for (size_t index = 0; index < JOB_COUNT; ++index) {
            CHECK(hits[index] == round);
        }
    }
    thread_pool_destroy(pool);
    CHECK_PTHREAD(pthread_mutex_destroy(&record.mutex));
}

static void test_capacity_one_backpressure(void)
{
    unsigned int hits[2] = {0};
    Record record = {PTHREAD_MUTEX_INITIALIZER, hits};
    RecordJob jobs[2] = {{&record, 0}, {&record, 1}};
    Gate gate = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER,
                 false, false};
    SubmitProbe probe;
    pthread_t producer;
    ThreadPool *pool = NULL;

    CHECK(thread_pool_create(&pool, 1, 1) == 0);
    CHECK(thread_pool_submit(pool, gated_task, &gate) == 0);
    wait_for_gate(&gate);
    CHECK(thread_pool_submit(pool, record_task, &jobs[0]) == 0);
    init_probe(&probe, pool, &jobs[1]);
    CHECK_PTHREAD(pthread_create(&producer, NULL, submit_probe, &probe));
    check_probe_started(&probe);

    release_gate(&gate);
    CHECK_PTHREAD(pthread_join(producer, NULL));
    CHECK(probe.result == 0);
    thread_pool_wait(pool);
    CHECK(hits[0] == 1);
    CHECK(hits[1] == 1);
    thread_pool_destroy(pool);
    destroy_probe(&probe);
    destroy_gate(&gate);
    CHECK_PTHREAD(pthread_mutex_destroy(&record.mutex));
}

static void test_close_drain_and_producer_wakeup(void)
{
    enum { PRODUCERS = 8, ROUNDS = 12 };
    for (unsigned int round = 0; round < ROUNDS; ++round) {
        unsigned int hits[PRODUCERS + 1] = {0};
        Record record = {PTHREAD_MUTEX_INITIALIZER, hits};
        RecordJob jobs[PRODUCERS + 1];
        Gate gate = {PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER,
                     false, false};
        SubmitProbe probes[PRODUCERS];
        pthread_t producers[PRODUCERS];
        ThreadPool *pool = NULL;

        CHECK(thread_pool_create(&pool, 1, 1) == 0);
        for (size_t index = 0; index <= PRODUCERS; ++index) {
            jobs[index] = (RecordJob){&record, index};
        }
        CHECK(thread_pool_submit(pool, gated_task, &gate) == 0);
        wait_for_gate(&gate);
        CHECK(thread_pool_submit(pool, record_task, &jobs[0]) == 0);
        for (size_t index = 0; index < PRODUCERS; ++index) {
            init_probe(&probes[index], pool, &jobs[index + 1]);
            CHECK_PTHREAD(pthread_create(&producers[index], NULL, submit_probe,
                                         &probes[index]));
            check_probe_started(&probes[index]);
        }

        /* The public API cannot expose the exact instant a producer sleeps.
         * Keep the worker and full queue gated; joins before release catch a
         * missing close broadcast without relying on scheduling delays. */
        thread_pool_close(pool);
        CHECK(thread_pool_submit(pool, record_task, &jobs[0]) == ECANCELED);
        for (size_t index = 0; index < PRODUCERS; ++index) {
            CHECK_PTHREAD(pthread_join(producers[index], NULL));
            CHECK(probes[index].result == ECANCELED);
            destroy_probe(&probes[index]);
        }
        release_gate(&gate);
        thread_pool_wait(pool);
        CHECK(hits[0] == 1);
        for (size_t index = 1; index <= PRODUCERS; ++index) {
            CHECK(hits[index] == 0);
        }
        thread_pool_destroy(pool);
        destroy_gate(&gate);
        CHECK_PTHREAD(pthread_mutex_destroy(&record.mutex));
    }
}

static void test_destroy_drains(void)
{
    enum { JOB_COUNT = 1237 };
    unsigned int hits[JOB_COUNT] = {0};
    Record record = {PTHREAD_MUTEX_INITIALIZER, hits};
    RecordJob jobs[JOB_COUNT];
    ThreadPool *pool = NULL;

    CHECK(thread_pool_create(&pool, 4, 23) == 0);
    for (size_t index = 0; index < JOB_COUNT; ++index) {
        jobs[index] = (RecordJob){&record, index};
        CHECK(thread_pool_submit(pool, record_task, &jobs[index]) == 0);
    }
    thread_pool_destroy(pool);
    for (size_t index = 0; index < JOB_COUNT; ++index) {
        CHECK(hits[index] == 1);
    }
    CHECK_PTHREAD(pthread_mutex_destroy(&record.mutex));
}

static void deadlock_alarm(int signal_number)
{
    static const char message[] = "thread_pool tests: deadlock/time limit\n";
    (void)signal_number;
    ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
    (void)written;
    _exit(EXIT_FAILURE);
}

int main(void)
{
    struct sigaction action = {0};
    action.sa_handler = deadlock_alarm;
    CHECK(sigemptyset(&action.sa_mask) == 0);
    CHECK(sigaction(SIGALRM, &action, NULL) == 0);
    alarm(60);

    test_invalid_create();
    puts("PASS invalid create");
    test_execution_once();
    puts("PASS concurrent producers execute every task once");
    test_wait_and_reuse();
    puts("PASS wait, empty wait, and reuse");
    test_capacity_one_backpressure();
    puts("PASS capacity-one backpressure");
    test_close_drain_and_producer_wakeup();
    puts("PASS close, drain, and blocked-producer wakeup");
    test_destroy_drains();
    puts("PASS destroy drains accepted tasks");
    alarm(0);
    return EXIT_SUCCESS;
}
