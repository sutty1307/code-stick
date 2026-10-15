#!/usr/bin/env python3
"""LOCAL PROPOSAL: optional exact-APK test on a disposable debug Android VM.

Import run_usb_memory and pass the existing runtime_smoke.Smoke instance after
the candidate startup phase passes. This file does not run anything on import.
It creates one NEW virtual removable disk, uses real DocumentsUI consent, and
checks only explicit fact/note forms, snapshots, restart restoration and Models.
It never supplies a tree URI, grants permissions, writes app data, clears app
data, loads a model, changes the APK, or contacts GitHub/network services.
"""

import hashlib
import json
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


PACKAGE = 'com.codestick.aidrive'
COMPONENT = PACKAGE + '/com.Vali98.ChatterUI.MainActivity'
UUID_PATTERN = r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}'
SNAPSHOT_NAMES = ('drive-memory-a.json', 'drive-memory-b.json')


class InvariantError(Exception):
    """A deliberate safety/ambiguity failure; never swallowed by retry loops."""


def _bounds(node):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return (left, top, right, bottom) if right > left and bottom > top else None


def _nodes(xml, label, node_class=None):
    wanted = label.casefold()
    return [node for node in ET.fromstring(xml).iter('node')
            if node.get('enabled') != 'false' and _bounds(node)
            and (node_class is None or node.get('class') == node_class)
            and any(node.get(key, '').casefold() == wanted for key in ('text', 'content-desc'))]


def _root_rows(output):
    """Parse only three named Android content-query fields; do not read files."""
    rows = []
    for line in output.splitlines():
        if not line.startswith('Row: '):
            continue
        row = {key: value.strip() for key, value in
               re.findall(r'(root_id|title|document_id)=([^,]*)(?:,|$)', line)}
        if set(row) == {'root_id', 'title', 'document_id'}:
            rows.append(row)
    return rows


class _USBTest:
    def __init__(self, smoke):
        self.s = smoke
        token = uuid.uuid4().hex[:10]
        self.fact, self.note, self.title = 'DriveFact' + token, 'DriveNote' + token, 'NoteTitle' + token
        self.folder = 'AI-Drive-Test'
        self.volume, self.disk, self.base, self.current_revision = None, None, None, None
        self.last_ui = None
        self.continuity_pid = None

    def _visible_labels(self):
        if not self.last_ui:
            return ''
        labels = [node.get('text', '') for node in ET.fromstring(self.last_ui).iter('node')
                  if node.get('text') and _bounds(node)]
        return ' | '.join(dict.fromkeys(label[:160] for label in labels))[:1600]

    def _wait(self, label, operation, maximum=60):
        deadline, last_error = time.monotonic() + maximum, None
        while time.monotonic() < deadline:
            try:
                result = operation()
                if result:
                    return result
            except (RuntimeError, ValueError, ET.ParseError) as error:
                last_error = error
            time.sleep(1)
        raise RuntimeError(f'{label} did not complete in {maximum}s: {last_error or "condition not met"}. '
                           f'Actual visible controls: {self._visible_labels()}')

    def _screen(self):
        if self.continuity_pid and self.s.shell('pidof', PACKAGE, check=False) != self.continuity_pid:
            raise InvariantError('App died or restarted during the restored UI checks')
        xml = self.s.xml()
        self.last_ui = xml
        # Never keep issuing UI input after a real app failure.
        if _nodes(xml, 'Unable to open your drive session') or _nodes(xml, 'Drive unavailable'):
            raise InvariantError('The actual app showed a drive/session error: ' + self._visible_labels())
        return xml

    def _tap_xml(self, xml, label):
        matches = _nodes(xml, label)
        # Identically named nested accessible nodes at the same bounds are OK;
        # ambiguous controls at distinct positions are not.
        positions = {_bounds(node) for node in matches}
        if len(positions) > 1:
            raise InvariantError(f'Ambiguous enabled UI targets for {label!r}')
        if len(positions) != 1:
            raise RuntimeError(f'Expected one enabled visible UI target for {label!r}; found {len(positions)}')
        left, top, right, bottom = positions.pop()
        self.s.shell('input', 'tap', str((left + right) // 2), str((top + bottom) // 2))

    def _tap(self, label, maximum=30):
        def attempt():
            xml = self._screen()
            if not _nodes(xml, label):
                return False
            self._tap_xml(xml, label)
            return True
        self._wait('Tap ' + label, attempt, maximum)

    def _size(self, xml):
        bounds = [_bounds(node) for node in ET.fromstring(xml).iter('node')]
        bounds = [item for item in bounds if item]
        if not bounds:
            raise RuntimeError('Actual UI supplied no screen bounds')
        return max(item[2] for item in bounds), max(item[3] for item in bounds)

    def _swipe(self, direction):
        width, height = self._size(self._screen())
        if direction == 'right':
            coordinates = (width // 8, height // 2, width * 7 // 8, height // 2, 180)
        else:
            start, end = (0.76, 0.30) if direction == 'up' else (0.30, 0.76)
            coordinates = (width * 3 // 4, int(height * start), width * 3 // 4, int(height * end), 350)
        self.s.shell('input', 'swipe', *map(str, coordinates))
        time.sleep(0.4)

    def _find(self, label, direction='up', swipes=8, node_class=None):
        for index in range(swipes + 1):
            xml = self._screen()
            if _nodes(xml, label, node_class):
                return xml
            if index < swipes:
                self._swipe(direction)
        raise RuntimeError(f'Could not find {label!r} after {swipes} actual UI scrolls. '
                           f'Actual visible controls: {self._visible_labels()}')

    def _top(self):
        return self._find('Drive connected', 'down', 10)

    def _hide_keyboard(self):
        state = self.s.shell('dumpsys', 'input_method', timeout=20)
        # BACK only if Android explicitly says an IME is shown. With a hardware
        # keyboard, unconditional BACK would navigate away instead of hiding it.
        if re.search(r'\b(?:mInputShown|isInputViewShown)=true\b', state):
            self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
            time.sleep(0.3)

    def _fill(self, label, value):
        if not re.fullmatch(r'[A-Za-z0-9]+', value):
            raise RuntimeError('Only locally generated ASCII fixture markers may be typed')
        xml = self._find(label)
        root = ET.fromstring(xml)
        labels = _nodes(xml, label)
        if len(labels) != 1:
            raise RuntimeError(f'Input label is ambiguous: {label}')
        label_bounds = _bounds(labels[0])
        candidates = []
        for node in root.iter('node'):
            bounds = _bounds(node)
            if (node.get('class') == 'android.widget.EditText' and bounds
                    and node.get('enabled') != 'false' and bounds[1] >= label_bounds[3]
                    and bounds[0] < label_bounds[2] and bounds[2] > label_bounds[0]):
                candidates.append((bounds[1] - label_bounds[3], node, bounds))
        candidates.sort(key=lambda item: item[0])
        if not candidates or candidates[0][0] > 400:
            raise RuntimeError(f'No EditText directly below the actual {label!r} label')
        _, node, (left, top, right, bottom) = candidates[0]
        if node.get('text'):
            raise RuntimeError(f'{label} is not blank; refusing to overwrite unexpected state')
        self.s.shell('input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
        self.s.shell('input', 'text', value)
        self._wait('Actual input text ' + label,
                   lambda: any(node.get('class') == 'android.widget.EditText' and node.get('text') == value
                               for node in ET.fromstring(self._screen()).iter('node')), 15)
        self._hide_keyboard()

    def fixture(self):
        """Partition only the single disk that THIS operation just created."""
        self.s.wait_boot(maximum=120, stable_seconds=10)
        if not any(self.s.shell('getprop', key) == '1' for key in ('ro.kernel.qemu', 'ro.boot.qemu')):
            raise InvariantError('Virtual fixture requires a disposable Android emulator; qemu property is absent')
        rooted = self.s.adb('root', timeout=30, check=False)
        self.s.write('usb-adb-root.txt', rooted.stdout + rooted.stderr)
        self.s.adb('wait-for-device', timeout=30)
        if self.s.shell('id', '-u') != '0':
            raise RuntimeError('Virtual removable fixture unsupported: this disposable emulator is not debuggable/rootable')
        old_disks = set(re.findall(r'^disk:\d+,\d+$', self.s.shell('sm', 'list-disks'), re.MULTILINE))
        old_volumes = set(self.s.shell('sm', 'list-volumes', 'public').splitlines())
        self.s.write('usb-before-disks.txt', '\n'.join(sorted(old_disks)))
        self.s.write('usb-before-volumes.txt', '\n'.join(sorted(old_volumes)))
        if old_disks or old_volumes:
            raise InvariantError('Fresh disposable VM gate failed: pre-existing disks/public volumes; no virtual disk created')
        self.s.shell('sm', 'set-virtual-disk', 'true', timeout=30)

        def new_disk():
            current = set(re.findall(r'^disk:\d+,\d+$', self.s.shell('sm', 'list-disks'), re.MULTILINE))
            added = current - old_disks
            if len(added) > 1:
                raise InvariantError('More than one disk appeared; refusing to choose or partition')
            return next(iter(added)) if added else None

        self.disk = self._wait('One newly created virtual disk', new_disk)
        if self.disk in old_disks or not re.fullmatch(r'disk:\d+,\d+', self.disk):
            raise InvariantError('Refusing to partition a pre-existing or invalid disk')
        self.s.shell('sm', 'partition', self.disk, 'public', timeout=90)

        def mounted_volume():
            added = set(self.s.shell('sm', 'list-volumes', 'public').splitlines()) - old_volumes
            mounted = [match.groups() for line in added
                       if (match := re.fullmatch(r'(public:\d+,\d+) mounted (' + UUID_PATTERN + ')', line))]
            if len(mounted) > 1:
                raise InvariantError('Multiple new mounted volumes; refusing ambiguous removable storage')
            return mounted[0] if mounted else None

        volume_id, self.volume = self._wait('New mounted removable volume', mounted_volume, 90)
        self.base = f'/storage/{self.volume}/{self.folder}'
        # OS fixture creates an EMPTY folder only, before any app permission.
        self.s.shell('mkdir', self.base)
        self.s.write('usb-fixture.json', json.dumps({'simulated_removable_storage': True,
                     'physical_usb_tested': False, 'new_disk': self.disk,
                     'new_volume_id': volume_id, 'new_uuid': self.volume,
                     'empty_fixture_folder': self.base}, indent=2) + '\n')
        return {'newly_created_disk_only': True, 'simulated_removable_storage': True,
                'physical_usb_tested': False}

    def open_memory(self):
        # Fresh startup or previous calculator phase both have real UI routes.
        xml = self._screen()
        if _nodes(xml, 'Drive connected') or _nodes(xml, 'Choose USB memory folder'):
            return
        if _nodes(xml, 'Offline Toolkit'):
            # Calculator output can leave the toolkit scrolled below its link.
            self._tap_xml(self._find('Drive Memory', 'down'), 'Drive Memory')
        elif _nodes(xml, 'Drive Memory'):
            self._tap_xml(xml, 'Drive Memory')  # Offline Toolkit's visible button.
        else:
            # Home uses a documented rightward gesture; its menu icon has no
            # accessible label. Do not navigate with deep links or injected JS.
            self._swipe('right')
            self._tap('Drive Memory')
        self._wait('Drive Memory screen', lambda: _nodes(self._screen(), 'Choose USB memory folder')
                   or _nodes(self._screen(), 'Drive connected'), 45)

    def grant(self):
        self.open_memory()
        self._tap('Choose USB memory folder')

        def matching_root():
            output = self.s.shell('content', 'query', '--uri',
                                  'content://com.android.externalstorage.documents/root',
                                  '--projection', 'root_id:title:document_id', timeout=20)
            all_roots = _root_rows(output)
            roots = [row for row in all_roots
                     if row['root_id'].casefold() == self.volume.casefold()
                     and row['document_id'].casefold() == (self.volume + ':').casefold()]
            if len(roots) > 1:
                raise InvariantError('Provider reports multiple roots for the new UUID; refusing selection')
            if len(roots) == 1 and roots[0]['title']:
                if sum(row['title'].casefold() == roots[0]['title'].casefold() for row in all_roots) != 1:
                    raise InvariantError('New removable root title is ambiguous; refusing UI selection')
                self.s.write('usb-provider-new-root.json', json.dumps(roots[0], indent=2) + '\n')
                return roots[0]
            return None

        root = self._wait('ExternalStorageProvider root for the new UUID', matching_root, 40)
        # Metadata chooses the visible drawer label only. Folder permission is
        # exclusively obtained through genuine DocumentsUI taps below.
        self._tap('Show roots')
        self._tap(root['title'])
        self._tap(self.folder)
        # Let the actual folder-navigation transaction settle. This is an
        # intentionally empty new folder; the previous root listing must close.
        time.sleep(1)
        before = self._screen()
        if not _nodes(before, self.folder):
            raise RuntimeError('Selected fixture folder is not shown in the real folder picker')
        self.s.write('usb-saf-before-consent.xml', before)
        self._tap('Use this folder')
        self._tap('Allow')
        self._wait('Real app drive connection', lambda: _nodes(self._screen(), 'Drive connected'), 90)
        self.s.capture('usb-saf-connected')
        snapshot = self._wait_snapshot({})
        self.current_revision = snapshot['revision']
        return {'genuine_saf_ui_grant': True, 'initial_revision': self.current_revision,
                'simulated_removable_storage': True, 'physical_usb_tested': False}

    def _snapshots(self):
        snapshots = []
        for name in SNAPSHOT_NAMES:
            path = self.base + '/' + name
            if self.s.adb('shell', 'test', '-f', path, check=False).returncode:
                continue
            raw = self.s.shell('cat', path)
            if len(raw) > 32 * 1024 * 1024:
                raise RuntimeError('Unexpectedly large snapshot in the fresh test folder')
            envelope = json.loads(raw)
            if (envelope.get('format') != 'chatterui-usb-memory-v1'
                    or not isinstance(envelope.get('body'), str)
                    or hashlib.sha256(envelope['body'].encode()).hexdigest() != envelope.get('sha256')):
                raise RuntimeError('App-created snapshot failed its real on-volume format/checksum check')
            data = json.loads(envelope['body'])
            if (data.get('version') != 1 or type(data.get('revision')) is not int
                    or data['revision'] < 1 or not isinstance(data.get('entries'), list)):
                raise RuntimeError('App-created snapshot has an invalid revision or entries')
            snapshots.append((data['revision'], envelope['body'], data))
        if not snapshots:
            return None
        snapshots.sort(key=lambda item: item[0], reverse=True)
        if len(snapshots) == 2 and snapshots[0][0] == snapshots[1][0] and snapshots[0][1] != snapshots[1][1]:
            raise RuntimeError('App wrote conflicting snapshots with the same revision')
        return snapshots[0][2]

    def _wait_snapshot(self, expected, after=None):
        def ready():
            data = self._snapshots()
            if not data or (after is not None and data['revision'] <= after):
                return None
            for text, (kind, title) in expected.items():
                matches = [item for item in data['entries'] if item.get('text') == text]
                if not matches:
                    return None
                if (len(matches) != 1 or matches[0].get('kind') != kind
                        or matches[0].get('source', {}).get('type') != 'manual'
                        or (title is not None and matches[0].get('title') != title)):
                    raise RuntimeError('Saved marker record has incorrect kind/title/manual provenance')
            return data
        return self._wait('Verified real on-volume snapshot markers/revision', ready, 60)

    def _save_form(self, kind, marker, expected, title=None):
        self._top()
        self._tap_xml(self._find(kind), kind)
        if title is not None:
            self._fill('Note title (optional)', title)
        self._fill('Memory text', marker)
        self._tap_xml(self._find('Save to drive'), 'Save to drive')
        data = self._wait_snapshot(expected, after=self.current_revision)
        self.current_revision = data['revision']
        # The cleared editor cannot satisfy this assertion: verify the rendered
        # saved card plus the app's own verified-save counter.
        self._find(marker, node_class='android.widget.TextView')
        self._top()
        def verified_counter():
            counters = [int(match[1]) for node in ET.fromstring(self._screen()).iter('node')
                        if (match := re.search(r'Verified save #(\d+)', node.get('text', '')))]
            return counters and max(counters) >= data['revision']
        self._wait('Actual UI verified-save counter', verified_counter, 20)
        return {'manual_form_saved': kind.lower(), 'snapshot_sha256_verified': True,
                'revision': self.current_revision, 'saved_card_visible': True}

    def fact_form(self):
        return self._save_form('Fact', self.fact, {self.fact: ('fact', None)})

    def note_form(self):
        return self._save_form('Note', self.note,
                               {self.fact: ('fact', None), self.note: ('note', self.title)}, self.title)

    def restart(self):
        saved_revision = self.current_revision
        old_pid = self.s.shell('pidof', PACKAGE, check=False)
        if not old_pid:
            raise RuntimeError('App was not running before the restart test')
        self.s.shell('am', 'force-stop', PACKAGE)
        self._wait('Original app process stopped', lambda: not self.s.shell('pidof', PACKAGE, check=False), 15)
        try:
            started = self.s.shell('am', 'start', '-W', '-n', COMPONENT, timeout=45)
        except subprocess.TimeoutExpired:
            # AMS -W may time out while its activity keeps starting normally.
            # This is pending, never a pass: process continuity and real restored
            # UI below remain mandatory for the following bounded readiness wait.
            started = 'Harness timeout waiting for am start -W; pending actual process/UI readiness'
        self.s.write('usb-relaunch-activity.txt', started)
        if 'Error' in started or ('Status:' in started and not re.search(r'Status:\s*(?:ok|timeout)\b', started)):
            raise InvariantError('Android rejected relaunch after force-stop: ' + started)
        initial_pid = self._wait('App relaunch process', lambda: self.s.shell('pidof', PACKAGE, check=False), 20)
        self.continuity_pid = initial_pid
        def restored_home():
            if self.s.shell('pidof', PACKAGE, check=False) != initial_pid:
                raise InvariantError('App died or restarted while waiting for restored Home')
            xml = self._screen()
            return any(_nodes(xml, label) for label in ('Drive connected', 'Characters',
                       'Recent', 'No Characters Found. Try Importing Some!'))
        # Let restoration actually finish before opening the existing UI drawer.
        self._wait('Restored Home or Drive Memory', restored_home, 90)
        self.open_memory()
        self._wait('Restored drive connection', lambda: _nodes(self._screen(), 'Drive connected'), 45)
        for marker in (self.fact, self.note, 'Note · ' + self.title):
            self._top()
            self._find(marker, node_class='android.widget.TextView')
        restored = self._wait_snapshot({self.fact: ('fact', None), self.note: ('note', self.title)})
        if restored['revision'] < saved_revision:
            raise RuntimeError('Restart rolled back the verified saved revision')
        self.current_revision = restored['revision']
        self.s.results['usb_memory_persistence_tested'] = True
        self.s.results['usb_memory_test_storage'] = 'Android debug virtual removable disk; not physical USB/SD'
        return {'force_stop_restart_restored_fact_and_note': True, 'app_data_cleared': False,
                'physical_usb_tested': False, 'restored_revision': self.current_revision}

    def models(self):
        self._top()
        self._tap_xml(self._find('Choose a model'), 'Choose a model')
        def models_ready():
            xml = self._screen()
            return _nodes(xml, 'Models') and _nodes(xml, 'Settings')
        self._wait('Real Models header and Settings control', models_ready, 40)
        self._tap('Settings')
        def settings_ready():
            xml = self._screen()
            return _nodes(xml, 'Model Settings') and _nodes(xml, 'CPU Settings')
        self._wait('Real Model Settings header and CPU Settings', settings_ready, 40)
        return {'actual_ui_controls_verified': ['Choose a model', 'Models', 'Settings', 'Model Settings', 'CPU Settings'],
                'model_inference_tested': False, 'model_route_verified': True}


def run_usb_memory(smoke):
    """Return a bounded real-test result; stop immediately after any failed step.

    Caller must gate on candidate startup and use a fresh disposable VM. Each
    phase's native CI notice is the actual pass/failure, never evidence content.
    The existing Smoke saves screenshots/XML/logcat/results after each phase.
    """
    test = _USBTest(smoke)
    phases = [('virtual-removable-fixture', test.fixture),
              ('virtual-removable-saf-grant', test.grant),
              ('virtual-removable-fact-form', test.fact_form),
              ('virtual-removable-note-form', test.note_form),
              ('virtual-removable-force-stop-restore', test.restart),
              ('virtual-removable-model-route', test.models)]
    for label, operation in phases:
        if not smoke.phase(label, operation):
            return False
    return True
