package com.codestick.uiprobe;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class RootPollerFixtures {
    private static final String TARGET = "com.codestick.aidrive";
    private static final class Root { final String pkg; Root(String pkg) { this.pkg = pkg; } }
    private static final class Clock implements RootPoller.Clock, RootPoller.Sleeper {
        long time; public long now() { return time; } public void sleep(long ms) { time += ms; }
    }
    private static final class Reader implements RootPoller.Reader<Root> {
        final Root[] values; int reads, releases; Reader(Root... values) { this.values = values; }
        public Root read() { int index = reads++; return index < values.length ? values[index] : null; }
        public String packageName(Root root) { return root.pkg; }
        public void release(Root root) { releases++; }
    }
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    public static void main(String[] args) throws Exception {
        Clock clock = new Clock(); Root target = new Root(TARGET);
        Reader reader = new Reader(null, null, target);
        RootPoller.Result<Root> result = new RootPoller<Root>(reader, TARGET, 5000, 150, clock, clock).poll();
        check(result.root == target && result.attempts == 3 && result.nullReads == 2 && result.elapsedMs == 300, "delayed target failed");
        check(reader.releases == 0, "accepted root was prematurely recycled");

        clock = new Clock(); reader = new Reader(new Root("com.android.launcher3"), target);
        result = new RootPoller<Root>(reader, TARGET, 5000, 150, clock, clock).poll();
        check(result.status.equals("wrong_active_package") && result.root == null && reader.reads == 1 && reader.releases == 1, "wrong package retried/fell back");

        clock = new Clock(); reader = new Reader();
        result = new RootPoller<Root>(reader, TARGET, 5000, 150, clock, clock).poll();
        check(result.status.equals("root_null") && result.root == null && result.attempts == 34 && result.elapsedMs == 5000, "all-null budget failed");

        final Clock lateClock = new Clock(); final AtomicInteger lateReleases = new AtomicInteger();
        RootPoller.Reader<Root> lateReader = new RootPoller.Reader<Root>() {
            public Root read() { lateClock.time = 5001; return target; }
            public String packageName(Root root) { return root.pkg; }
            public void release(Root root) { lateReleases.incrementAndGet(); }
        };
        result = new RootPoller<Root>(lateReader, TARGET, 5000, 150, lateClock, lateClock).poll();
        check(result.status.equals("root_query_timeout") && result.root == null && lateReleases.get() == 1, "late root accepted");

        clock = new Clock(); reader = new Reader(null, null, new Root("com.android.systemui"), target);
        result = new RootPoller<Root>(reader, TARGET, 5000, 150, clock, clock).poll();
        check(result.status.equals("wrong_active_package") && result.attempts == 3 && reader.reads == 3, "later wrong package retried");

        final CountDownLatch released = new CountDownLatch(1);
        RootPoller.Reader<Root> blockedReader = new RootPoller.Reader<Root>() {
            public Root read() {
                long stop = System.nanoTime() + 300000000L;
                while (System.nanoTime() < stop) { try { Thread.sleep(10); } catch (InterruptedException ignored) { } }
                return target;
            }
            public String packageName(Root root) { return root.pkg; }
            public void release(Root root) { released.countDown(); }
        };
        long began = System.nanoTime();
        result = RootPoller.acquire(blockedReader, TARGET, 40, 10);
        long elapsed = (System.nanoTime() - began) / 1000000L;
        check(result.status.equals("root_query_timeout") && result.root == null && elapsed < 200, "blocking read extended caller deadline");
        check(released.await(1, TimeUnit.SECONDS), "late blocked result was not discarded/recycled");
        System.out.println("RootPoller fixtures: 6 passed; no Android calls executed");
    }
}
