#ifndef THREAD_POOL_H
#define THREAD_POOL_H

#include <stddef.h>

typedef struct ThreadPool ThreadPool;
typedef void (*ThreadTaskFunction)(void *argument);

/* Return zero on success or a positive errno/pthread error code on failure. */
int thread_pool_create(ThreadPool **pool, size_t workers, size_t queue_capacity);
int thread_pool_submit(ThreadPool *pool, ThreadTaskFunction function, void *argument);

/* Wait until both queued and running tasks finish. The pool remains reusable. */
void thread_pool_wait(ThreadPool *pool);

/* Reject new submissions, wake blocked producers, and drain accepted tasks. */
void thread_pool_close(ThreadPool *pool);

/* Call only after other API callers have stopped; closes, drains, and joins. */
void thread_pool_destroy(ThreadPool *pool);

/* Callbacks must not submit to, wait on, or destroy their own pool. */

#endif
