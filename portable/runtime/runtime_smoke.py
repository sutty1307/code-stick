#!/usr/bin/env python3
"""Runtime-only Android CI smoke test. No GitHub writes, USB simulation or model load."""
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
CANDIDATE_SHA256 = 'aab31bcdf39f623cd21c30e02db0b1c16674e4ec27c77fb28f9cef7f5d422c1b'
IMAGE = 'system-images;android-30;google_apis;x86_64'
AVD = 'ai_drive_runtime_api30'
SERIAL = 'emulator-5554'


def report_annotation(kind, message):
    """Emit a bounded native CI diagnostic directly from this execution."""
    # A single bounded annotation is a failure summary, not an artifact/file relay.
    # Escape workflow-command data so exception text cannot create new commands.
    message = message[:2000]
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
               'baseline_uid', 'candidate_uid'}
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
        self.results = {'test': 'API30 KVM startup and upgrade smoke', 'physical_phone_tested': False,
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

    def xml(self):
        self.shell('rm', '-f', '/sdcard/ai-drive-runtime-ui.xml')
        self.shell('uiautomator', 'dump', '/sdcard/ai-drive-runtime-ui.xml', timeout=20)
        content = self.shell('cat', '/sdcard/ai-drive-runtime-ui.xml')
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
        uid = re.search(r'\buserId=(\d+)', info)
        if not uid or int(uid[1]) <= 0:
            raise RuntimeError('Installed application UID was missing or invalid')
        return {'version_code': version_code, 'install_kind': 'upgrade' if replace else 'fresh',
                'uid': uid[1]}

    def launch(self, label):
        self.shell('am', 'force-stop', PACKAGE)
        started = self.shell('am', 'start', '-W', '-n', COMPONENT, timeout=45)
        if 'Status: ok' not in started or 'Error' in started:
            raise RuntimeError(f'Activity launch was not successful: {started}')
        self.write(f'{label}-activity-launch.txt', started)
        initial, until = None, time.monotonic() + 20
        while time.monotonic() < until:
            pid = self.shell('pidof', PACKAGE, check=False)
            if not pid or (initial is not None and initial != pid):
                raise RuntimeError('Application process exited or restarted during the startup observation')
            initial = pid
            if self.shell('pidof', 'system_server') != self.results['stable_system_server_pid']:
                raise RuntimeError('Android framework restarted during application startup')
            time.sleep(2)
        activity = self.shell('dumpsys', 'activity', 'activities')
        resumed = [line for line in activity.splitlines() if 'mResumedActivity' in line or 'topResumedActivity' in line]
        if not any(PACKAGE in line for line in resumed):
            raise RuntimeError('Application is not the foreground resumed activity')
        content = self.xml()
        self.write(f'{label}-startup-visible.xml', content)
        text_bounds(content, 'Choose USB memory folder')
        text_bounds(content, 'Offline Toolkit')
        return {'process_stable_seconds': 20, 'pid': initial, 'foreground_activity_verified': True,
                'actual_ui_controls_verified': ['Choose USB memory folder', 'Offline Toolkit']}

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
        setup_stage = 'KVM prerequisites'
        try:
            if platform.machine() != 'x86_64' or not os.access('/dev/kvm', os.R_OK | os.W_OK):
                raise RuntimeError('This test requires an x86_64 hosted VM with usable /dev/kvm; no TCG fallback')
            setup_stage = 'APK SHA-256 verification'
            for name, file, expected in [('baseline', args.baseline, BASELINE_SHA256), ('candidate', args.candidate, CANDIDATE_SHA256)]:
                if file.is_symlink() or digest(file) != expected:
                    raise RuntimeError(f'{name} APK SHA-256 does not match the exact reviewed binary')
            smoke.results['apk_sha256'] = {'baseline': BASELINE_SHA256, 'candidate': CANDIDATE_SHA256}
            setup_stage = 'SDK installation'
            sdkmanager = args.sdk / 'cmdline-tools/latest/bin/sdkmanager'
            installed = smoke.command([sdkmanager, 'platform-tools', 'emulator', IMAGE], timeout=900, check=False, input_text='y\n' * 100)
            smoke.write('sdk-install.txt', installed.stdout + installed.stderr)
            if installed.returncode:
                raise RuntimeError(f'sdkmanager exited {installed.returncode}; stderr: {installed.stderr[-1200:]}; stdout: {installed.stdout[-400:]}')
            setup_stage = 'installed SDK image properties'
            smoke.write('system-image-source.properties', (args.sdk / 'system-images/android-30/google_apis/x86_64/source.properties').read_text())
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
                    or props['ro.dalvik.vm.isa.arm64'] != 'x86_64' or props['ro.build.version.sdk'] != '30'):
                raise RuntimeError('The Google API image does not advertise the required ARM64 native translation')
            setup_stage = 'app runtime phases'
            baseline = smoke.phase('baseline-install', lambda: smoke.install(args.baseline, 1))
            if baseline:
                smoke.phase('baseline-startup', lambda: smoke.launch('baseline'))
            else:
                smoke.results['phases']['baseline-startup'] = {'passed': False, 'skipped': True, 'reason': 'Baseline install failed'}
            # Keep the installed v1 and its UID; never uninstall/clear before attempting -r.
            candidate = smoke.phase('candidate-upgrade' if baseline else 'candidate-fresh-install', lambda: smoke.install(args.candidate, 2, replace=baseline))
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
            smoke.results['passed'] = all(phase['passed'] for phase in smoke.results['phases'].values())
            smoke.results['candidate_install_kind'] = 'upgrade' if baseline else 'fresh; NOT an upgrade test'
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
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    raise SystemExit(run(parser.parse_args()))
