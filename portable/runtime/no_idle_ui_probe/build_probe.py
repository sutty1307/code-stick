#!/usr/bin/env python3
"""Build only this diagnostic APK using existing local SDK/JDK; no downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk', type=Path, default=Path('/workspace/android-drive-kit/build-tools/android-sdk'))
    parser.add_argument('--jdk', type=Path, default=Path('/workspace/android-drive-kit/build-tools/jdk-17.0.16+8'))
    args = parser.parse_args()
    tools = args.sdk / 'build-tools/36.0.0'
    android = args.sdk / 'platforms/android-36/android.jar'
    build = root / 'build'
    build.mkdir(exist_ok=True)
    classes, dex = build / 'classes', build / 'dex'
    classes.mkdir(exist_ok=True)
    dex.mkdir(exist_ok=True)
    signing = root / 'diagnostic-only-signing'
    signing.mkdir(mode=0o700, exist_ok=True)
    keystore = signing / 'no-idle-ui-probe-debug.p12'
    environment = os.environ.copy()
    environment['UI_PROBE_TEST_STORE_PASS'] = 'android'

    def run(command, maximum=45):
        result = subprocess.run([str(item) for item in command], capture_output=True, text=True,
                                env=environment, timeout=maximum)
        if result.returncode:
            raise RuntimeError(Path(str(command[0])).name + ' failed: ' + (result.stdout + result.stderr)[-3000:])
        return result.stdout + result.stderr

    if not keystore.exists():
        run([args.jdk / 'bin/keytool', '-genkeypair', '-alias', 'no-idle-ui-probe-debug',
             '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650', '-storetype', 'PKCS12',
             '-keystore', keystore, '-storepass:env', 'UI_PROBE_TEST_STORE_PASS',
             '-keypass:env', 'UI_PROBE_TEST_STORE_PASS',
             '-dname', 'CN=Local UI Probe Test Key,OU=Diagnostic only,O=Code Stick Test'])
        keystore.chmod(0o600)
    run([args.jdk / 'bin/javac', '--release', '8', '-encoding', 'UTF-8', '-classpath', android,
         '-d', classes, *sorted((root / 'src').rglob('*.java'))])
    run([args.jdk / 'bin/java', '-cp', tools / 'lib/d8.jar', 'com.android.tools.r8.D8',
         '--min-api', '24', '--lib', android, '--output', dex, *sorted(classes.rglob('*.class'))])
    unsigned = build / 'probe-unsigned.apk'
    run([tools / 'aapt2', 'link', '-I', android, '--manifest', root / 'AndroidManifest.xml',
         '--min-sdk-version', '24', '--target-sdk-version', '35', '-o', unsigned])
    with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as apk:
        apk.write(dex / 'classes.dex', 'classes.dex')
    aligned = build / 'probe-aligned.apk'
    run([tools / 'zipalign', '-p', '-f', '4', unsigned, aligned])
    output = root / 'AI-Drive-NoIdle-UI-Probe-v2-test-only.apk'
    run([args.jdk / 'bin/java', '-jar', tools / 'lib/apksigner.jar', 'sign', '--ks', keystore,
         '--ks-key-alias', 'no-idle-ui-probe-debug', '--ks-pass', 'env:UI_PROBE_TEST_STORE_PASS',
         '--key-pass', 'env:UI_PROBE_TEST_STORE_PASS', '--out', output, aligned])
    signature = run([args.jdk / 'bin/java', '-jar', tools / 'lib/apksigner.jar', 'verify', '--verbose', output])
    badging = run([tools / 'aapt2', 'dump', 'badging', output])
    permissions = run([tools / 'aapt2', 'dump', 'permissions', output])
    if 'uses-permission' in permissions:
        raise RuntimeError('Diagnostic APK unexpectedly declares permissions')
    if output.stat().st_size > 100 * 1024:
        raise RuntimeError('Diagnostic APK exceeds the 100 KiB limit')
    with zipfile.ZipFile(output) as apk:
        if apk.testzip() is not None:
            raise RuntimeError('Diagnostic APK failed CRC verification')
        if b'waitForIdle' in apk.read('classes.dex'):
            raise RuntimeError('Diagnostic DEX unexpectedly references waitForIdle')
    metadata = {'artifact': output.name, 'bytes': output.stat().st_size,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'package': 'com.codestick.uiprobe', 'instrumentation_targets_self': True,
                'read_target_package': 'com.codestick.aidrive', 'declared_permissions': [],
                'wait_for_idle_referenced_in_dex': False, 'runtime_tested': False,
                'version_code': 2, 'root_retry_budget_ms': 5000, 'root_poll_interval_ms': 150,
                'signing_key': 'separate diagnostic-only test key; never the customer app key'}
    (root / 'BUILD-VALIDATION.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (build / 'apksigner-verify.txt').write_text(signature)
    (build / 'aapt-badging.txt').write_text(badging)
    (build / 'aapt-permissions.txt').write_text(permissions)
    print(json.dumps(metadata, sort_keys=True))


if __name__ == '__main__':
    main()
