#include "thread_pool.h"

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>

typedef struct {
    ThreadTaskFunction function;
    void *argument;
} ThreadTask;

struct ThreadPool {
    pthread_t *workers;
    size_t worker_count;
    ThreadTask *queue;
    size_t capacity;
    size_t head;
    size_t tail;
    size_t queued;
    size_t active;
    int closing;
    pthread_mutex_t mutex;
    pthread_cond_t not_empty;
    pthread_cond_t not_full;
    pthread_cond_t idle;
};

static void *worker_main(void *argument)
{
    ThreadPool *pool = argument;

    for (;;) {
        (void)pthread_mutex_lock(&pool->mutex);
        while (pool->queued == 0 && !pool->closing) {
            (void)pthread_cond_wait(&pool->not_empty, &pool->mutex);
        }
        if (pool->queued == 0 && pool->closing) {
            (void)pthread_mutex_unlock(&pool->mutex);
            break;
        }

        ThreadTask task = pool->queue[pool->head];
        pool->head = (pool->head + 1) % pool->capacity;
        --pool->queued;
        ++pool->active;
        (void)pthread_cond_signal(&pool->not_full);
        (void)pthread_mutex_unlock(&pool->mutex);

        /* Pixel work must not serialize on the queue mutex. */
        task.function(task.argument);

        (void)pthread_mutex_lock(&pool->mutex);
        --pool->active;
        if (pool->queued == 0 && pool->active == 0) {
            (void)pthread_cond_broadcast(&pool->idle);
        }
        (void)pthread_mutex_unlock(&pool->mutex);
    }
    return NULL;
}

int thread_pool_create(ThreadPool **result, size_t workers, size_t queue_capacity)
{
    if (result == NULL) {
        return EINVAL;
    }
    *result = NULL;
    if (workers == 0 || queue_capacity == 0) {
        return EINVAL;
    }
    if (workers > SIZE_MAX / sizeof(pthread_t) ||
        queue_capacity > SIZE_MAX / sizeof(ThreadTask)) {
        return EOVERFLOW;
    }

    ThreadPool *pool = calloc(1, sizeof(*pool));
    if (pool == NULL) {
        return ENOMEM;
    }
    pool->workers = calloc(workers, sizeof(*pool->workers));
    pool->queue = calloc(queue_capacity, sizeof(*pool->queue));
    pool->capacity = queue_capacity;
    if (pool->workers == NULL || pool->queue == NULL) {
        free(pool->workers);
        free(pool->queue);
        free(pool);
        return ENOMEM;
    }

    int error = pthread_mutex_init(&pool->mutex, NULL);
    if (error != 0) {
        goto fail_allocations;
    }
    error = pthread_cond_init(&pool->not_empty, NULL);
    if (error != 0) {
        goto fail_mutex;
    }
    error = pthread_cond_init(&pool->not_full, NULL);
    if (error != 0) {
        goto fail_not_empty;
    }
    error = pthread_cond_init(&pool->idle, NULL);
    if (error != 0) {
        goto fail_not_full;
    }

    for (size_t index = 0; index < workers; ++index) {
        error = pthread_create(&pool->workers[index], NULL, worker_main, pool);
        if (error != 0) {
            /* Only initialized threads and synchronization objects are joined. */
            thread_pool_destroy(pool);
            return error;
        }
        ++pool->worker_count;
    }
    *result = pool;
    return 0;

fail_not_full:
    (void)pthread_cond_destroy(&pool->not_full);
fail_not_empty:
    (void)pthread_cond_destroy(&pool->not_empty);
fail_mutex:
    (void)pthread_mutex_destroy(&pool->mutex);
fail_allocations:
    free(pool->queue);
    free(pool->workers);
    free(pool);
    return error;
}

int thread_pool_submit(ThreadPool *pool, ThreadTaskFunction function, void *argument)
{
    if (pool == NULL || function == NULL) {
        return EINVAL;
    }
    (void)pthread_mutex_lock(&pool->mutex);
    while (pool->queued == pool->capacity && !pool->closing) {
        (void)pthread_cond_wait(&pool->not_full, &pool->mutex);
    }
    if (pool->closing) {
        (void)pthread_mutex_unlock(&pool->mutex);
        return ECANCELED;
    }

    pool->queue[pool->tail] = (ThreadTask){function, argument};
    pool->tail = (pool->tail + 1) % pool->capacity;
    ++pool->queued;
    (void)pthread_cond_signal(&pool->not_empty);
    (void)pthread_mutex_unlock(&pool->mutex);
    return 0;
}

void thread_pool_wait(ThreadPool *pool)
{
    if (pool == NULL) {
        return;
    }
    (void)pthread_mutex_lock(&pool->mutex);
    while (pool->queued != 0 || pool->active != 0) {
        (void)pthread_cond_wait(&pool->idle, &pool->mutex);
    }
    (void)pthread_mutex_unlock(&pool->mutex);
}

void thread_pool_close(ThreadPool *pool)
{
    if (pool == NULL) {
        return;
    }
    (void)pthread_mutex_lock(&pool->mutex);
    pool->closing = 1;
    (void)pthread_cond_broadcast(&pool->not_empty);
    (void)pthread_cond_broadcast(&pool->not_full);
    (void)pthread_mutex_unlock(&pool->mutex);
}

void thread_pool_destroy(ThreadPool *pool)
{
    if (pool == NULL) {
        return;
    }
    thread_pool_close(pool);
    for (size_t index = 0; index < pool->worker_count; ++index) {
        (void)pthread_join(pool->workers[index], NULL);
    }
    (void)pthread_cond_destroy(&pool->idle);
    (void)pthread_cond_destroy(&pool->not_full);
    (void)pthread_cond_destroy(&pool->not_empty);
    (void)pthread_mutex_destroy(&pool->mutex);
    free(pool->queue);
    free(pool->workers);
    free(pool);
}
