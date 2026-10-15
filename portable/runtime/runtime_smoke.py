#!/usr/bin/env python3
"""Android CI smoke checks, with optional debug removable storage; no model load."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

PACKAGE = 'com.codestick.aidrive'
COMPONENT = PACKAGE + '/com.Vali98.ChatterUI.MainActivity'
BASELINE_SHA256 = '937b9d098130525d72dbbbf428868c4a66794927a801fe55d6a8899278cb28e2'
# Exact reviewed usb.4 APK; v1 remains the immutable upgrade baseline.
CANDIDATE_SHA256 = 'c833e55ff089903f3bc0e084f10b91516590fa5d9945138723e4ba466508d549'
CANDIDATE_VERSION_CODE = 4
API = 35
IMAGE = f'system-images;android-{API};google_apis;x86_64'
AVD = f'ai_drive_runtime_usb4_api{API}'
SERIAL = 'emulator-5554'


def report_annotation(kind, message):
    """Emit a bounded native CI diagnostic directly from this execution."""
    # A single bounded annotation is a failure summary, not an artifact/file relay.
    # Escape workflow-command data so exception text cannot create new commands.
    message = message[:6000]
    message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f'::{kind}::{message}', flush=True)


def report_error(label, error):
    """Report a newly raised test error through the runner's ordinary diagnostics."""
    report_annotation('error', f'{label}: {type(error).__name__}: {error}')


def report_pass(label, details=None):
    # Publish only the explicit outcome fields from this operation, never files
    # or the full evidence/results object.
    allowed = {'version_code', 'install_kind', 'uid', 'process_stable_seconds',
               'foreground_activity_verified', 'actual_ui_controls_verified',
               'real_ui_result_verified', 'expected_visible_result',
               'baseline_uid', 'candidate_uid', 'am_launch_status',
               'am_launch_elapsed_seconds', 'ready_elapsed_seconds',
               'newly_created_disk_only', 'simulated_removable_storage',
               'genuine_saf_ui_grant', 'initial_revision', 'manual_form_saved',
               'snapshot_sha256_verified', 'revision', 'saved_card_visible',
               'force_stop_restart_restored_fact_and_note', 'app_data_cleared',
               'restored_revision', 'model_route_verified'}
    summary = {key: value for key, value in (details or {}).items() if key in allowed}
    suffix = ' ' + json.dumps(summary, sort_keys=True) if summary else ''
    report_annotation('notice', f'{label}: PASS{suffix}')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def text_bounds(xml, label):
    root = ET.fromstring(xml)
    for node in root.iter('node'):
        if label not in (node.get('text'), node.get('content-desc')):
            continue
        match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
        if match:
            left, top, right, bottom = map(int, match.groups())
            if right > left and bottom > top and node.get('enabled') != 'false':
                return ((left + right) // 2, (top + bottom) // 2)
    raise RuntimeError(f'Visible UI control not found: {label}')


def boot_ready(properties):
    return (properties['boot'] == '1' and properties['user'] == 'RUNNING_UNLOCKED'
            and 'found' in properties['mount'] and 'not found' not in properties['mount']
            and 'found' in properties['package'] and 'not found' not in properties['package']
            and bool(re.fullmatch(r'\d+', properties['system_server'])))


class Smoke:
    def __init__(self, sdk, evidence, env):
        self.sdk, self.evidence, self.env = Path(sdk), Path(evidence), env
        self.adb_path = self.sdk / 'platform-tools/adb'
        self.results = {'test': f'API{API} KVM usb.4 startup and v1 upgrade smoke', 'physical_phone_tested': False,
                        'physical_usb_tested': False, 'model_inference_tested': False,
                        'usb_memory_persistence_tested': False, 'phases': {}}

    def command(self, args, timeout=30, check=True, input_text=None):
        result = subprocess.run([str(arg) for arg in args], env=self.env, input=input_text,
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'{Path(str(args[0])).name} failed ({result.returncode}): {result.stdout[-3000:]} {result.stderr[-3000:]}')
        return result

    def adb(self, *args, timeout=20, check=True):
        return self.command([self.adb_path, '-s', SERIAL, *args], timeout, check)

    def write(self, name, text):
        (self.evidence / name).write_text(text, encoding='utf-8')

    def shell(self, *args, timeout=15, check=True):
        return self.adb('shell', *args, timeout=timeout, check=check).stdout.strip()

    def wait_boot(self, maximum=360, stable_seconds=20):
        deadline, stable_since, previous_pid = time.monotonic() + maximum, None, None
        observations = self.evidence / 'boot-observations.jsonl'
        with observations.open('a') as log:
            while time.monotonic() < deadline:
                try:
                    values = {'boot': self.shell('getprop', 'sys.boot_completed'),
                              'mount': self.shell('service', 'check', 'mount'),
                              'package': self.shell('service', 'check', 'package'),
                              'user': self.shell('am', 'get-started-user-state', '0'),
                              'system_server': self.shell('pidof', 'system_server')}
                    pid = values['system_server']
                    if boot_ready(values):
                        if pid != previous_pid or stable_since is None:
                            stable_since = time.monotonic()
                        if time.monotonic() - stable_since >= stable_seconds:
                            self.results['stable_system_server_pid'] = pid
                            self.results['boot_stable_seconds'] = stable_seconds
                            log.write(json.dumps(values | {'stable': True}) + '\n')
                            return
                    else:
                        stable_since = None
                    previous_pid = pid
                    log.write(json.dumps(values) + '\n')
                except (subprocess.TimeoutExpired, RuntimeError) as error:
                    stable_since, previous_pid = None, None
                    log.write(json.dumps({'waiting': str(error)[:1000]}) + '\n')
                log.flush()
                time.sleep(5)
        raise RuntimeError('Android never reached stable boot/unlocked storage+package services; no installer was invoked')

    def xml(self, maximum=50):
        deadline = time.monotonic() + maximum

        def remaining(limit):
            value = min(limit, deadline - time.monotonic())
            if value <= 0:
                raise RuntimeError('UI hierarchy capture exceeded its time budget')
            return value

        self.shell('rm', '-f', '/sdcard/ai-drive-runtime-ui.xml', timeout=remaining(15))
        self.shell('uiautomator', 'dump', '/sdcard/ai-drive-runtime-ui.xml', timeout=remaining(20))
        content = self.shell('cat', '/sdcard/ai-drive-runtime-ui.xml', timeout=remaining(15))
        ET.fromstring(content)
        return content

    def capture(self, name):
        failures = []
        try:
            image = subprocess.run([str(self.adb_path), '-s', SERIAL, 'exec-out', 'screencap', '-p'],
                                   env=self.env, capture_output=True, timeout=20, check=True).stdout
            if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('Android did not return a PNG screenshot')
            (self.evidence / f'{name}.png').write_bytes(image)
        except Exception as error:
            failures.append(f'screenshot: {error}')
        try:
            self.write(f'{name}.xml', self.xml())
        except Exception as error:
            failures.append(f'UI XML: {error}')
        for suffix, args in [('activity', ('dumpsys', 'activity', 'activities')),
                             ('package', ('dumpsys', 'package', PACKAGE)),
                             ('crash-log', ('logcat', '-d', '-b', 'crash')),
                             ('logcat', ('logcat', '-d', '-v', 'threadtime', '-b', 'main', '-b', 'system', '-b', 'crash'))]:
            try:
                self.write(f'{name}-{suffix}.txt', self.shell(*args, timeout=20, check=False))
            except Exception as error:
                failures.append(f'{suffix}: {error}')
        if failures:
            self.write(f'{name}-capture-errors.txt', '\n'.join(failures))

    def phase(self, label, operation):
        started = time.monotonic()
        try:
            self.shell('logcat', '-c', check=False)
            details = operation() or {}
            self.results['phases'][label] = {'passed': True, 'elapsed_seconds': round(time.monotonic() - started, 2), **details}
            report_pass(label, details)
            return True
        except Exception as error:
            self.results['phases'][label] = {'passed': False, 'elapsed_seconds': round(time.monotonic() - started, 2), 'error': str(error)}
            report_error(label, error)
            return False
        finally:
            self.capture(label)
            self.save()

    def install(self, apk, version_code, replace=False):
        # Never call pm/install until the full stable boot gate has passed.
        self.wait_boot(maximum=120, stable_seconds=10)
        command = ['install', '--no-streaming'] + (['-r'] if replace else []) + [str(apk)]
        result = self.adb(*command, timeout=120, check=False)
        self.write(f'install-v{version_code}.txt', result.stdout + result.stderr)
        if result.returncode or not re.search(r'^Success\s*$', result.stdout, re.MULTILINE):
            raise RuntimeError(f'Android did not confirm successful APK installation: {result.stdout} {result.stderr}')
        info = self.shell('dumpsys', 'package', PACKAGE)
        if not re.search(rf'\bversionCode={version_code}\b', info):
            raise RuntimeError('Installed package versionCode does not match the tested APK')
        uid_report = self.shell('cmd', 'package', 'list', 'packages', '-U', '--user', '0', PACKAGE)
        self.write(f'package-uid-v{version_code}.txt', uid_report)
        uid = re.search(rf'^package:{re.escape(PACKAGE)}\s+uid:\s*(\d+)\s*$',
                        uid_report, re.MULTILINE)
        if not uid or int(uid[1]) <= 0:
            raise RuntimeError(f'Installed application UID was missing or invalid: {uid_report}')
        return {'version_code': version_code, 'install_kind': 'upgrade' if replace else 'fresh',
                'uid': uid[1]}

    def launch(self, label):
        self.shell('am', 'force-stop', PACKAGE)
        began = time.monotonic()
        deadline = began + 90
        started = self.shell('am', 'start', '-W', '-n', COMPONENT, timeout=45)
        launch_elapsed = time.monotonic() - began
        self.write(f'{label}-activity-launch.txt', started)
        statuses = re.findall(r'^Status:\s*(\S+)\s*$', started, re.MULTILINE)
        launch_record = {'am_launch_status': statuses[0] if len(statuses) == 1 else 'unexpected',
                         'am_launch_elapsed_seconds': round(launch_elapsed, 2)}
        self.results.setdefault('activity_launches', {})[label] = launch_record
        if len(statuses) != 1 or statuses[0] not in {'ok', 'timeout'} or 'Error' in started:
            raise RuntimeError(f'Activity launch returned an unexpected status: {started}')
        launch_status = statuses[0]
        initial, ready_since, last_pending = None, None, 'Real app UI has not appeared'

        def remaining(limit):
            value = min(limit, deadline - time.monotonic())
            if value <= 0:
                raise RuntimeError('Application startup exceeded its 90-second deadline')
            return value

        # am start -W can time out while a cold translated app is still starting.
        # Neither that timeout nor Status: ok proves real application readiness.
        while time.monotonic() < deadline:
            pid = self.shell('pidof', PACKAGE, timeout=remaining(15), check=False)
            if not pid or (initial is not None and initial != pid):
                crash = self.shell('logcat', '-d', '-b', 'crash', '-v', 'brief', check=False)
                lines = crash.splitlines()
                anchor = next((i for i, line in enumerate(lines) if PACKAGE in line), None)
                crash_detail = ''
                if anchor is not None:
                    crash_detail = '\n'.join(lines[max(0, anchor - 3):anchor + 45])[:4500]
                if not crash_detail.strip():
                    crash_detail = self.shell('logcat', '-d', '-v', 'brief', '-s',
                                              'AndroidRuntime:E', 'ReactNativeJS:E', 'libc:F',
                                              check=False)[-4500:]
                raise RuntimeError('Application process exited or restarted during startup. '
                                   f'Android crash diagnostic: {crash_detail or "No crash message returned"}')
            initial = pid
            if self.shell('pidof', 'system_server', timeout=remaining(15)) != self.results['stable_system_server_pid']:
                raise RuntimeError('Android framework restarted during application startup')
            try:
                activity = self.shell('dumpsys', 'activity', 'activities', timeout=remaining(15))
                resumed = [line for line in activity.splitlines() if 'mResumedActivity' in line or 'topResumedActivity' in line]
                if not any(PACKAGE in line for line in resumed):
                    raise RuntimeError('Application is not yet the foreground resumed activity')
                content = self.xml(maximum=remaining(20))
                text_bounds(content, 'Choose USB memory folder')
                text_bounds(content, 'Offline Toolkit')
            except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as error:
                ready_since = None
                last_pending = str(error)
            else:
                now = time.monotonic()
                if ready_since is None:
                    ready_since = now
                last_pending = 'Real UI appeared; waiting for 20 seconds of stable readiness'
                if now <= deadline and now - ready_since >= 20:
                    self.write(f'{label}-startup-visible.xml', content)
                    return {'process_stable_seconds': 20, 'pid': initial,
                            'foreground_activity_verified': True,
                            'actual_ui_controls_verified': ['Choose USB memory folder', 'Offline Toolkit'],
                            'am_launch_status': launch_status,
                            'am_launch_elapsed_seconds': round(launch_elapsed, 2),
                            'ready_elapsed_seconds': round(now - began, 2)}
            time.sleep(max(0, min(2, deadline - time.monotonic())))
        raise RuntimeError(f'Application did not show the real USB controls in the foreground '
                           f'with an unchanged PID for 20 seconds within 90 seconds '
                           f'(am status: {launch_status}): {last_pending}')

    def tap(self, label, maximum=45):
        deadline = time.monotonic() + maximum
        while time.monotonic() < deadline:
            try:
                x, y = text_bounds(self.xml(), label)
                self.shell('input', 'tap', str(x), str(y))
                return
            except RuntimeError:
                time.sleep(1)
        raise RuntimeError(f'Could not find and tap the real UI control: {label}')

    def calculator(self):
        self.tap('Offline Toolkit')
        self.tap('Calculator')
        self.tap('Run tool')
        deadline = time.monotonic() + 30
        expected = '(12 + 8) * 3 = 60'
        while time.monotonic() < deadline:
            content = self.xml()
            nodes = ET.fromstring(content).iter('node')
            if any(node.get('text') == expected for node in nodes):
                self.write('calculator-result.xml', content)
                return {'command': '/calc (12 + 8) * 3', 'expected_visible_result': expected,
                        'real_ui_result_verified': True, 'configured_usb_required': False}
            time.sleep(1)
        raise RuntimeError('The actual UI did not show the exact calculator result 60')

    def save(self):
        self.write('RESULTS.json', json.dumps(self.results, indent=2) + '\n')


def run(args):
    evidence = args.evidence.resolve()
    if evidence.exists():
        raise RuntimeError('Use a new evidence directory')
    evidence.mkdir(parents=True)
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix='ai-drive-runtime-ci-') as temporary:
        env.update({'ANDROID_AVD_HOME': str(Path(temporary) / 'avd'), 'ANDROID_USER_HOME': str(Path(temporary) / 'android'),
                    'ANDROID_HOME': str(args.sdk.resolve()), 'ANDROID_SDK_ROOT': str(args.sdk.resolve())})
        Path(env['ANDROID_AVD_HOME']).mkdir()
        Path(env['ANDROID_USER_HOME']).mkdir()
        smoke, emulator, logcat, handles = Smoke(args.sdk, evidence, env), None, None, []
        smoke.results['test_mode'] = 'fresh-candidate' if args.fresh_candidate else 'baseline-upgrade'
        if args.fresh_candidate:
            smoke.results['test'] = f'API{API} KVM fresh usb.4 candidate startup smoke; no baseline or upgrade test'
        setup_stage = 'KVM prerequisites'
        try:
            if platform.machine() != 'x86_64' or not os.access('/dev/kvm', os.R_OK | os.W_OK):
                raise RuntimeError('This test requires an x86_64 hosted VM with usable /dev/kvm; no TCG fallback')
            setup_stage = 'APK SHA-256 verification'
            if not re.fullmatch(r'[0-9a-f]{64}', CANDIDATE_SHA256):
                raise RuntimeError('The reviewed usb.4 candidate APK SHA-256 pin is invalid')
            apks = [('candidate', args.candidate, CANDIDATE_SHA256)]
            if not args.fresh_candidate:
                apks.insert(0, ('baseline', args.baseline, BASELINE_SHA256))
            for name, file, expected in apks:
                if file.is_symlink() or digest(file) != expected:
                    raise RuntimeError(f'{name} APK SHA-256 does not match the exact reviewed binary')
            smoke.results['apk_sha256'] = {name: expected for name, _, expected in apks}
            setup_stage = 'SDK installation'
            sdkmanager = args.sdk / 'cmdline-tools/latest/bin/sdkmanager'
            installed = smoke.command([sdkmanager, 'platform-tools', 'emulator', IMAGE], timeout=900, check=False, input_text='y\n' * 100)
            smoke.write('sdk-install.txt', installed.stdout + installed.stderr)
            if installed.returncode:
                raise RuntimeError(f'sdkmanager exited {installed.returncode}; stderr: {installed.stderr[-1200:]}; stdout: {installed.stdout[-400:]}')
            setup_stage = 'installed SDK image properties'
            smoke.write('system-image-source.properties', (args.sdk / f'system-images/android-{API}/google_apis/x86_64/source.properties').read_text())
            setup_stage = 'emulator version'
            smoke.write('emulator-version.txt', smoke.command([args.sdk / 'emulator/emulator', '-version']).stdout)
            setup_stage = 'emulator KVM acceleration check'
            acceleration = smoke.command([args.sdk / 'emulator/emulator', '-accel-check'])
            smoke.write('acceleration.txt', acceleration.stdout + acceleration.stderr)
            setup_stage = 'AVD creation'
            created = smoke.command([args.sdk / 'cmdline-tools/latest/bin/avdmanager', 'create', 'avd', '--name', AVD,
                                     '--package', IMAGE, '--device', 'pixel_2', '--force'], timeout=90, input_text='no\n')
            smoke.write('avd-create.txt', created.stdout + created.stderr)
            setup_stage = 'emulator process startup'
            emu_log = (evidence / 'emulator.log').open('w')
            handles.append(emu_log)
            emulator = subprocess.Popen([str(args.sdk / 'emulator/emulator'), '-avd', AVD, '-no-window', '-no-metrics', '-no-audio',
                                         '-no-snapshot', '-no-boot-anim', '-accel', 'on', '-gpu', 'swiftshader', '-memory', '3072', '-cores', '2',
                                         '-camera-back', 'none', '-camera-front', 'none', '-feature', '-Vulkan',
                                         '-feature', '-HostComposition', '-feature', '-PlayStoreImage', '-port', '5554'],
                                        env=env, stdout=emu_log, stderr=subprocess.STDOUT)
            setup_stage = 'ADB server startup'
            smoke.command([smoke.adb_path, 'start-server'])
            setup_stage = 'stable unlocked Android boot'
            smoke.wait_boot()
            smoke.adb('wait-for-device', timeout=30)
            setup_stage = 'guest logcat capture'
            adb_log = (evidence / 'logcat.txt').open('w')
            handles.append(adb_log)
            logcat = subprocess.Popen([str(smoke.adb_path), '-s', SERIAL, 'logcat', '-T', '1', '-v', 'threadtime', '-b', 'main', '-b', 'system', '-b', 'crash'],
                                      env=env, stdout=adb_log, stderr=subprocess.STDOUT)
            setup_stage = 'guest ARM64 translation verification'
            smoke.shell('input', 'keyevent', '82')
            props = {key: smoke.shell('getprop', key) for key in ['ro.product.cpu.abilist', 'ro.dalvik.vm.native.bridge', 'ro.dalvik.vm.isa.arm64', 'ro.build.version.sdk']}
            smoke.write('guest-runtime-properties.json', json.dumps(props, indent=2))
            if ('arm64-v8a' not in props['ro.product.cpu.abilist'] or props['ro.dalvik.vm.native.bridge'] != 'libndk_translation.so'
                    or props['ro.dalvik.vm.isa.arm64'] != 'x86_64' or props['ro.build.version.sdk'] != str(API)):
                raise RuntimeError(f'The Google API {API} image does not advertise the required ARM64 native translation: {props}')
            setup_stage = 'app runtime phases'
            baseline = False
            if args.fresh_candidate:
                setup_stage = 'fresh candidate package absence on new isolated AVD'
                installed = smoke.shell('cmd', 'package', 'list', 'packages', '--user', '0', PACKAGE)
                smoke.write('fresh-candidate-before-install.txt', installed + '\n')
                if re.search(rf'^package:{re.escape(PACKAGE)}\s*$', installed, re.MULTILINE):
                    raise RuntimeError('Candidate package already exists; this is not a fresh installation test')
                smoke.results['fresh_candidate_clean_avd_verified'] = True
                setup_stage = 'fresh candidate runtime phases'
            else:
                baseline = smoke.phase('baseline-install', lambda: smoke.install(args.baseline, 1))
                if baseline:
                    baseline_launched = smoke.phase('baseline-startup', lambda: smoke.launch('baseline'))
                    if baseline_launched:
                        smoke.phase('baseline-calculator', smoke.calculator)
                else:
                    smoke.results['phases']['baseline-startup'] = {'passed': False, 'skipped': True, 'reason': 'Baseline install failed'}
            # Keep the installed v1 and its UID; never uninstall/clear before attempting -r.
            candidate = smoke.phase('candidate-upgrade' if baseline else 'candidate-fresh-install', lambda: smoke.install(args.candidate, CANDIDATE_VERSION_CODE, replace=baseline))
            if candidate:
                if baseline:
                    smoke.results['upgrade_same_uid'] = smoke.results['phases']['baseline-install']['uid'] == smoke.results['phases']['candidate-upgrade']['uid']
                    smoke.results['phases']['upgrade-identity'] = {'passed': smoke.results['upgrade_same_uid'],
                        'baseline_uid': smoke.results['phases']['baseline-install']['uid'],
                        'candidate_uid': smoke.results['phases']['candidate-upgrade']['uid']}
                    if smoke.results['upgrade_same_uid']:
                        report_pass('upgrade-identity', smoke.results['phases']['upgrade-identity'])
                    else:
                        report_error('upgrade-identity', RuntimeError('Android UID changed during the in-place upgrade'))
                launched = smoke.phase('candidate-startup', lambda: smoke.launch('candidate'))
                if launched:
                    smoke.phase('candidate-calculator', smoke.calculator)
                    if args.usb_memory:
                        from usb_memory_smoke import run_usb_memory
                        run_usb_memory(smoke)
            smoke.results['passed'] = all(phase['passed'] for phase in smoke.results['phases'].values())
            smoke.results['candidate_install_kind'] = ('fresh on a new isolated AVD; baseline never installed'
                                                       if args.fresh_candidate else
                                                       'upgrade' if baseline else 'fresh; NOT an upgrade test')
            smoke.save()
            return 0 if smoke.results['passed'] else 1
        except Exception as error:
            smoke.results.update({'passed': False, 'setup_failure_stage': setup_stage,
                                  'infrastructure_or_setup_error': str(error)})
            report_error(f'Android runtime setup [{setup_stage}]', error)
            smoke.capture('failure')
            smoke.save()
            return 1
        finally:
            for process in [logcat, emulator]:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for handle in handles:
                handle.close()
            summary = os.getenv('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a') as stream:
                    stream.write('## Android runtime smoke evidence\n\n')
                    for name, phase in smoke.results['phases'].items():
                        stream.write(f"- {name}: {'PASS' if phase['passed'] else 'FAIL / SKIPPED'}\n")
                    stream.write('\nOpen the runtime evidence artifact for actual screenshots, UI XML, logcat and RESULTS.json. No physical USB or model inference was tested.\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--fresh-candidate', action='store_true',
                        help='Test only the exact candidate on the new isolated AVD; not an upgrade test')
    parser.add_argument('--usb-memory', action='store_true',
                        help='Exercise real SAF consent and memory on a new debug virtual removable disk')
    args = parser.parse_args()
    if not args.fresh_candidate and args.baseline is None:
        parser.error('--baseline is required unless --fresh-candidate is selected')
    raise SystemExit(run(args))
