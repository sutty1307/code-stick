# Diagnostic no-idle probe v2 — bounded root retries

This is a separate, local-only test APK. The original v1 APK/source remains
unchanged with SHA-256 `f1cfdfaa4370aef933e9005449b9c635a84bf022a9950a910c8cd86fd39ab046`.
The customer app, its signing key, runtime worktree and remote files were not
modified. This probe must never be shipped in the customer kit.

The v2 change retries the **actual** `UiAutomation.getRootInActiveWindow()` call
for at most 5 seconds, sleeping 150 ms only between null results. It uses a
timed daemon worker so a single blocking Android query cannot extend the
caller's root-acquisition budget. Late roots are discarded/recycled, never
accepted. A non-null wrong-package root stops immediately without retrying.
There is no stored previous root, fallback XML, window substitution or idle
method. After acceptance, the exact AI Drive package is checked again before
walking the actual node hierarchy.

The result includes version, attempt count, null-result count, actual root
acquisition time, 5,000 ms budget and 150 ms interval. Host validation rejects
a claimed successful root outside those bounds. Root acquisition time excludes
initial UiAutomation connection and later traversal; the unchanged 30-second
device watchdog and 40-second host timeout bound the overall operation. Any
timeout, null root, wrong package or truncated hierarchy fails honestly.

Existing limits remain: 256 nodes, depth 32, 512 child queries, 2,048 characters
per attribute, 65,536 aggregate attribute characters and 128 KiB XML. The same
fresh nonce/base64/SHA-256 protocol validates actual XML without printing app
text. No activities, app API calls, app-data reads, input injection, permissions,
SAF operations or internet permission are added. The instrumentation targets
its own package and is marked test-only, signed with a newly generated separate
diagnostic key under this directory's ignored `diagnostic-only-signing/`.

Validation: six fixtures execute the actual pure Java root-acquisition policy
with null→target, immediate/later wrong packages, all-null budget, late target
and a blocking reader. Ten host protocol tests pass. Local SDK compilation,
signature/CRC/permission checks and DEX absence of `waitForIdle` pass. **Android
runtime execution is not tested.** Build/fixture commands:

```bash
python test_root_poller.py
python -m unittest -v test_protocol.py
python build_probe.py
```

After review, use this APK/decoder in a fresh disposable emulator only. Its
package is still `com.codestick.uiprobe`, now versionCode 2. A fresh install uses
`adb install --no-streaming -t AI-Drive-NoIdle-UI-Probe-v2-test-only.apk`.
Because v2 deliberately uses a new independent debug key, it cannot update the
old installed v1 in place; root's fresh test VM avoids that signing conflict.
No automatic uninstall or replacement is attempted.

Interpretation: Android permits a null active root when no root is available
to the accessibility connection at that instant. Connecting UiAutomation does
not establish that active-window/root information has arrived. Focus/window
transitions, accessibility connection/event timing, Android's own accessibility
cache/state and an unresponsive app are possible explanations; one immediate
null does not distinguish them. The exact API 35 framework implementation was
not locally available for source verification. This change makes repeated fresh
API calls and does not clear or substitute Android's internal cache; it cannot
guarantee that the framework supplies an atomic or uncached hierarchy.

A retry success would demonstrate a transiently missing root. Repeated nulls
or a query timeout still do **not**, by themselves, prove the UI thread is
blocked. Native backtraces/CPU and the original strict native/USB reply proof
remain separate evidence. This probe cannot make a blocked generation pass.
