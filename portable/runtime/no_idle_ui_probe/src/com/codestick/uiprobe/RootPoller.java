package com.codestick.uiprobe;

import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

/** Pure Java bounded root acquisition policy; no Android mutation or UI idle API. */
final class RootPoller<T> {
    interface Reader<T> { T read() throws Exception; String packageName(T value) throws Exception; void release(T value); }
    interface Clock { long now(); }
    interface Sleeper { void sleep(long ms) throws InterruptedException; }
    static final class Result<T> {
        final String status, packageName;
        final T root;
        final int attempts, nullReads;
        final long elapsedMs;
        Result(String status, T root, String packageName, int attempts, int nullReads, long elapsedMs) {
            this.status = status; this.root = root; this.packageName = packageName;
            this.attempts = attempts; this.nullReads = nullReads; this.elapsedMs = elapsedMs;
        }
    }
    private final Reader<T> reader;
    private final String target;
    private final long budgetMs, intervalMs;
    private final Clock clock;
    private final Sleeper sleeper;
    private final AtomicBoolean cancelled = new AtomicBoolean(false);
    private volatile int attempts, nullReads;
    private volatile long started = Long.MIN_VALUE;

    RootPoller(Reader<T> reader, String target, long budgetMs, long intervalMs, Clock clock, Sleeper sleeper) {
        this.reader = reader; this.target = target; this.budgetMs = budgetMs; this.intervalMs = intervalMs;
        this.clock = clock; this.sleeper = sleeper;
    }
    private long elapsed() { return Math.max(0, clock.now() - started); }
    private Result<T> result(String status, T root, String packageName) {
        return new Result<T>(status, root, packageName, attempts, nullReads, elapsed());
    }
    Result<T> poll() throws Exception {
        if (started == Long.MIN_VALUE) started = clock.now();
        while (!cancelled.get() && elapsed() < budgetMs && attempts < 35) {
            attempts++;
            T root = reader.read();
            if (root != null) {
                String observed;
                try { observed = reader.packageName(root); }
                catch (Exception error) { reader.release(root); throw error; }
                // A wrong package never gets another chance to become AI Drive.
                if (!target.equals(observed)) {
                    reader.release(root);
                    return result("wrong_active_package", null, observed);
                }
                if (cancelled.get() || elapsed() > budgetMs) {
                    reader.release(root);
                    return result("root_query_timeout", null, observed);
                }
                return result("ok", root, observed);
            }
            nullReads++;
            long remaining = budgetMs - elapsed();
            if (remaining <= 0 || cancelled.get()) break;
            sleeper.sleep(Math.min(intervalMs, remaining));
        }
        return result(cancelled.get() ? "root_query_timeout" : "root_null", null, "");
    }

    static <T> Result<T> acquire(Reader<T> reader, String target, long budgetMs, long intervalMs) throws Exception {
        Clock clock = () -> System.nanoTime() / 1000000L;
        RootPoller<T> poller = new RootPoller<T>(reader, target, budgetMs, intervalMs, clock, Thread::sleep);
        poller.started = clock.now();
        ExecutorService executor = Executors.newSingleThreadExecutor(task -> {
            Thread thread = new Thread(task, "UiProbe-root-read"); thread.setDaemon(true); return thread;
        });
        Future<Result<T>> future = executor.submit((Callable<Result<T>>) poller::poll);
        try {
            Result<T> result = future.get(budgetMs, TimeUnit.MILLISECONDS);
            if (result.root != null && clock.now() - poller.started > budgetMs) {
                reader.release(result.root);
                return poller.result("root_query_timeout", null, result.packageName);
            }
            return result;
        } catch (TimeoutException error) {
            poller.cancelled.set(true);
            future.cancel(true);
            // A non-interruptible Binder read may finish later. Its poll worker
            // checks cancelled and releases that root; it is never accepted.
            return poller.result("root_query_timeout", null, "");
        } finally {
            executor.shutdownNow();
        }
    }
}
