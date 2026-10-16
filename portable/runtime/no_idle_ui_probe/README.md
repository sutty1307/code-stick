# No-idle Android accessibility probe — diagnostic only

This separate 13 KiB instrumentation APK is a test diagnostic for a fresh
disposable Android emulator. It is **not part of the customer kit**. Local
compilation, APK signature/CRC/manifest checks and host protocol fixtures pass;
Android execution has not been tested yet.

The instrumentation targets its own `com.codestick.uiprobe` package. It has no
activities and declares **no permissions**, including no internet permission.
Its only UI operation is `Instrumentation.getUiAutomation` with
`FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES`, then `getRootInActiveWindow` and
read-only traversal of actual accessibility nodes. It never calls
`waitForIdle`, `executeAndWaitForEvent`, target app APIs, input injection,
shell commands, app-data access or URI/permission-grant APIs. It does not alter
the AI Drive APK, model, snapshots, inference state or strict reply proof.

The active root must report exactly `com.codestick.aidrive`. Wrong-package,
null-root, query-error and timeout results fail honestly. Traversal is bounded
by 256 emitted nodes, depth 32, 512 child queries, 2,048 characters per
attribute, 65,536 aggregate attribute characters and 128 KiB final XML. A
30-second watchdog bounds the device operation; the host also imposes a
45-second timeout. Truncation never passes as a complete hierarchy.

The finish Bundle carries a fresh host nonce, explicit protocol/status fields,
SHA-256 and base64 XML. This avoids untrusted app text becoming stdout protocol
lines. The host validates all of these plus the XML root package/node count
before saving a standard `<hierarchy><node .../></hierarchy>` document. It
prints only a small metadata summary, never the UI text/base64.

Build using already installed public SDK/JDK tools, with no Gradle or downloads:

```bash
python build_probe.py --sdk /path/to/android-sdk --jdk /path/to/jdk
python -m unittest -v test_protocol.py
```

The defaults point to the existing cloud SDK 36/JDK 17. A **separate new
diagnostic-only debug keystore** is generated under `diagnostic-only-signing/`.
It is never the customer app's signing key. Keep that directory private and
out of distribution; it is ignored alongside intermediate build output.

After review, on the disposable emulator only, install the independent probe
while retaining the running AI Drive application:

```bash
adb -s emulator-5554 install --no-streaming -t AI-Drive-NoIdle-UI-Probe-test-only.apk
python run_probe.py --adb /path/to/adb --serial emulator-5554 --output /new/evidence/busy-ui.xml
```

The APK is marked `testOnly`, so installation requires the explicit `-t` test
flag. `run_probe.py` does not install, launch or interact with the target app.
It runs the already installed self-targeted instrumentation only. It refuses
to overwrite evidence files. Root must integrate installation and any fallback
reading explicitly into the fresh test workflow after review.

If the UI thread cannot answer accessibility queries, this may still return a
null root or time out. It does not prove why UiAutomator failed, guarantee an
atomic hierarchy, or make a blocked inference pass. No fallback text or cached
hierarchy is returned. Strict native timings, exact prompt/reply, active swipe,
archive, checksum and durable-volume assertions remain unchanged.
