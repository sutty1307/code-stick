#!/usr/bin/env python3
"""Local proposal: exact Android APK, real SAF model selection and real inference.

Import the existing reviewed runtime_smoke and usb_memory_smoke helpers beside
this file. A fresh disposable VM is mandatory. Model bytes live in public phone
Download; app snapshots live on Android's small debug virtual removable volume.
This test never injects application data, grants SAF permission, changes an APK,
or claims physical-phone/USB compatibility. Nothing runs merely on import.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from runtime_smoke import Smoke, PACKAGE, SERIAL, digest, report_annotation
from usb_memory_smoke import _USBTest, _bounds, _nodes, _root_rows, InvariantError
from no_idle_ui_probe.run_probe import decode_result, COMPONENT as PROBE_COMPONENT
from generated_chat_restart import run_generated_chat_restart

API = 35
IMAGE = 'system-images;android-35;google_apis;x86_64'
AVD = 'ai_drive_real_inference_api35'
APK_SHA256 = 'c833e55ff089903f3bc0e084f10b91516590fa5d9945138723e4ba466508d549'
MODEL_FILE = 'Llama-3.2-3B-Instruct-abliterated.Q4_K_M.gguf'
MODEL_BYTES = 2019377472
MODEL_SHA256 = 'e1f2b428e316d8bd1cb5ca13e9649329e299d951fbcee6b3cd0e9f1d646c9fa7'
MODEL_URL = ('https://huggingface.co/QuantFactory/Llama-3.2-3B-Instruct-abliterated-GGUF/'
             'resolve/120d74373f679f9d9e0463b435dfd3fb802d9eb0/' + MODEL_FILE + '?download=true')
MODEL_PHONE = '/sdcard/Download/' + MODEL_FILE
MODEL_PROFILE, CPU_THREADS = '3b', 2
MODEL_PROFILES = {
    '3b': (MODEL_FILE, MODEL_BYTES, MODEL_SHA256, MODEL_URL),
    '1b-comparison': (
        'Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf', 955445792,
        '68336c25367576f49b29f01ee9034658b51bbded6a7f215e934d997de96301e0',
        'https://huggingface.co/mradermacher/Llama-3.2-1B-Instruct-abliterated-GGUF/'
        'resolve/becf36858832feddfea0ace2fabf791797571999/'
        'Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf?download=true'),
}


def select_test_profile(model, threads):
    """Choose only an explicit pinned comparison; never repin the production kit."""
    global MODEL_FILE, MODEL_BYTES, MODEL_SHA256, MODEL_URL, MODEL_PHONE, MODEL_PROFILE, CPU_THREADS
    if model not in MODEL_PROFILES or type(threads) is not int or threads not in (1, 2):
        raise InvariantError('Unknown pinned model/thread diagnostic profile')
    MODEL_FILE, MODEL_BYTES, MODEL_SHA256, MODEL_URL = MODEL_PROFILES[model]
    MODEL_PHONE = '/sdcard/Download/' + MODEL_FILE
    MODEL_PROFILE, CPU_THREADS = model, threads


PROBE_APK = Path(__file__).resolve().parent / 'no_idle_ui_probe/AI-Drive-NoIdle-UI-Probe-v2-test-only.apk'
PROBE_SHA256 = '9798f76df6fdea290a7b4670c7b01a74949041d24615162689469fdaad5f1efa'
ENGINE_KEY = 'enginedata-storage'
SAMPLER_KEY = 'samplerstate-storage'
FILE_ADD, PLAY, CLOSE, SEND, STOP = '\uf473', '\uf4d9', '\uf431', '\ue163', '\ue047'


class BudgetExceeded(Exception):
    pass


@contextmanager
def budget(seconds):
    """Bound blocking SDK/ADB/UI calls as well as retry loops on the Linux runner."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()

    def expired(signum, frame):
        raise BudgetExceeded(f'Operation exceeded its {seconds}-second budget')

    signal.signal(signal.SIGALRM, expired)
    timeout = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.001, previous_timer[0] - (time.monotonic() - began)), previous_timer[1])


def rows(data, table):
    app = data.get('applicationState', {})
    if app.get('format') != 'chatterui-drive-application-v1':
        raise RuntimeError('Actual snapshot lacks the supported full application state')
    matches = [item for item in app.get('tables', []) if item.get('name') == table]
    if len(matches) != 1 or not isinstance(matches[0].get('rows'), list):
        raise RuntimeError(f'Actual snapshot lacks a unique {table} table')
    return matches[0]['rows']


def preference(data, key):
    raw = data.get('applicationState', {}).get('preferences', {}).get(key)
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'Preference JSON parse failed for {key}: {error.msg}; '
                           f'pos={error.pos}, line={error.lineno}, column={error.colno}') from error
    return value.get('state') if isinstance(value, dict) else None


def default_character_state(data):
    """Inspect the fresh app-created rows; never create or inject a test character."""
    all_rows = rows(data, 'characters')
    characters = [row for row in all_rows if row.get('type') == 'character']
    summary = {'all_character_table_rows': len(all_rows), 'character_count': len(characters),
               'rows': [{key: row.get(key) for key in ('id', 'name', 'type')} for row in all_rows[:8]],
               'rows_omitted': max(0, len(all_rows) - 8), 'snapshot_revision': data['revision']}
    matches = [row for row in characters if row.get('name') == 'AI Bot']
    return (matches[0] if len(characters) == 1 and len(matches) == 1 else None), summary


def examine_slot(name, raw):
    """Decode the same core envelope/body contract, returning metadata without payload text."""
    meta = {'name': name, 'present': True, 'raw_characters': len(raw),
            'raw_utf8_bytes': len(raw.encode('utf-8')), 'valid': False,
            'envelope_valid': False, 'checksum_valid': False, 'body_valid': False}
    layer = 'envelope'
    try:
        if len(raw.encode('utf-16-le')) // 2 > 32 * 1024 * 1024 + 1024:
            raise ValueError('snapshot_exceeds_core_character_limit')
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or envelope.get('format') != 'chatterui-usb-memory-v1'
                or not isinstance(envelope.get('body'), str) or not isinstance(envelope.get('sha256'), str)):
            raise ValueError('unsupported_envelope_format_or_fields')
        meta['envelope_valid'] = True
        meta['body_characters'] = len(envelope['body'])
        layer = 'checksum'
        if hashlib.sha256(envelope['body'].encode('utf-8')).hexdigest() != envelope['sha256']:
            raise ValueError('body_sha256_mismatch')
        meta['checksum_valid'] = True
        layer = 'body'
        data = json.loads(envelope['body'])
        if not isinstance(data, dict):
            raise ValueError('body_is_not_an_object')
        if ('processedUserMessages' in data and
                (not isinstance(data['processedUserMessages'], list)
                 or any(not isinstance(key, str) for key in data['processedUserMessages']))):
            raise ValueError('invalid_message_provenance')
        revision = data.get('revision')
        if (data.get('version') != 1 or type(revision) is not int or not 1 <= revision <= 2**53 - 1
                or not isinstance(data.get('entries'), list) or not isinstance(data.get('conversations'), list)):
            raise ValueError('unsupported_body_version_revision_or_collections')
        for entry in data['entries']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                    or entry.get('kind') not in ('fact', 'preference', 'note')
                    or not all(isinstance(entry.get(key), str) for key in ('text', 'createdAt', 'updatedAt'))
                    or not isinstance(entry.get('source'), dict)
                    or entry['source'].get('type') not in ('manual', 'user-message')):
                raise ValueError('invalid_memory_entry_record')
        for conversation in data['conversations']:
            if (not isinstance(conversation, dict) or not isinstance(conversation.get('id'), str)
                    or not isinstance(conversation.get('messages'), list)):
                raise ValueError('invalid_conversation_record')
            for message in conversation['messages']:
                if (not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'system')
                        or not isinstance(message.get('text'), str)):
                    raise ValueError('invalid_conversation_message_record')
        meta.update({'valid': True, 'body_valid': True, 'revision': revision})
        return data, meta
    except json.JSONDecodeError as error:
        meta.update({'error_layer': layer, 'error_type': 'JSONDecodeError', 'error': error.msg,
                     'error_position': error.pos, 'error_line': error.lineno, 'error_column': error.colno})
    except ValueError as error:
        meta.update({'error_layer': layer, 'error_type': 'ValueError', 'error': str(error)})
    return None, meta


def select_verified_snapshot(raw_slots):
    """Follow core.readDrive: recover one invalid slot; reject conflicting equal revisions."""
    valid, metadata = [], []
    for name, raw in raw_slots:
        if raw is None:
            metadata.append({'name': name, 'present': False, 'valid': False})
            continue
        data, meta = examine_slot(name, raw)
        metadata.append(meta)
        if data is not None:
            valid.append((data['revision'], name, data))
    valid.sort(key=lambda item: item[0], reverse=True)
    summary = {'slots': metadata, 'recovered_invalid_other': any(
        item.get('present') and not item['valid'] for item in metadata)}
    if valid:
        if (len(valid) > 1 and valid[0][0] == valid[1][0]
                and json.dumps(valid[0][2], ensure_ascii=False, separators=(',', ':')) !=
                json.dumps(valid[1][2], ensure_ascii=False, separators=(',', ':'))):
            raise InvariantError('Conflicting verified equal-revision snapshots; metadata=' + json.dumps(summary))
        summary.update({'selected_slot': valid[0][1], 'selected_revision': valid[0][0]})
        return valid[0][2], summary
    return None, summary


def completed_reply(data, prompt, baseline_ids):
    """Reject greetings, echoes, unfinished buffers, old replies and fake timing-free text."""
    entries, swipes = rows(data, 'chat_entries'), rows(data, 'chat_swipes')
    users = [entry for entry in entries if entry.get('is_user') == 1
             and any(swipe.get('entry_id') == entry['id'] and swipe.get('active') == 1
                     and swipe.get('swipe') == prompt for swipe in swipes)]
    if not users:
        return None
    if len(users) != 1:
        raise InvariantError('The fixture prompt appears more than once in durable history')
    user = users[0]
    candidates = []
    for entry in entries:
        if (entry.get('is_user') != 0 or entry.get('chat_id') != user['chat_id']
                or entry.get('id', 0) <= user['id'] or str(entry['id']) in baseline_ids):
            continue
        for swipe in swipes:
            text = swipe.get('swipe')
            if (swipe.get('entry_id') != entry['id'] or swipe.get('active') != 1
                    or not isinstance(text, str) or not text.strip() or text == prompt):
                continue
            timing = swipe.get('timings')
            if isinstance(timing, str):
                timing = json.loads(timing)
            if (not isinstance(timing, dict) or type(timing.get('predicted_n')) is not int
                    or not 1 <= timing['predicted_n'] <= 16
                    or not isinstance(timing.get('predicted_ms'), (float, int))
                    or timing['predicted_ms'] < 0
                    or not isinstance(swipe.get('gen_finished'), int)
                    or not isinstance(swipe.get('gen_started'), int)
                    or swipe['gen_finished'] < swipe['gen_started']):
                continue
            archives = [conversation for conversation in data.get('conversations', [])
                        if conversation.get('id') == str(user['chat_id'])]
            if len(archives) != 1:
                continue
            messages = archives[0].get('messages', [])
            prompt_positions = [index for index, message in enumerate(messages)
                                if message.get('id') == str(user['id'])
                                and message.get('role') == 'user' and message.get('text') == prompt]
            if len(prompt_positions) != 1:
                continue
            if not any(message.get('id') == str(entry['id']) and message.get('role') == 'assistant'
                       and message.get('text') == text for message in messages[prompt_positions[0] + 1:]):
                continue
            candidates.append({'chat_id': user['chat_id'], 'entry_id': entry['id'],
                               'text': text, 'timings': timing, 'revision': data['revision']})
    if len(candidates) > 1:
        raise InvariantError('Multiple new generated assistant replies found for the one fixture prompt')
    return candidates[0] if candidates else None


def visible_reply_words(text):
    """Remove only paired inline Markdown markers that the real renderer hides.

    Native timings, exact durable text and archive matching remain mandatory.
    Preserve words, case and punctuation; unsupported markup still fails closed.
    """
    patterns = (r'\*\*([^*\n]+)\*\*', r'__([^_\n]+)__', r'~~([^~\n]+)~~',
                r'`([^`\n]+)`', r'(?<!\*)\*([^*\n]+)\*(?!\*)',
                r'(?<!\w)_([^_\n]+)_(?!\w)')
    for _ in range(2):
        for pattern in patterns:
            text = re.sub(pattern, r'\1', text)
    return text.split()


def response_snapshot_diagnostic(data, prompt, baseline_ids):
    """Explain the unchanged strict reply gate using only the fresh fixture rows."""
    result = {'snapshot_revision': data.get('revision')}
    entries, swipes = rows(data, 'chat_entries'), rows(data, 'chat_swipes')
    users = [entry for entry in entries if entry.get('is_user') == 1 and any(
        swipe.get('entry_id') == entry.get('id') and swipe.get('active') == 1
        and swipe.get('swipe') == prompt for swipe in swipes)]
    result['exact_prompt_user_count'] = len(users)
    result['baseline_entry_count'] = len(baseline_ids)
    result['new_entries'] = []
    user = users[0] if len(users) == 1 else None
    for entry in entries:
        if str(entry.get('id')) in baseline_ids:
            continue
        item = {key: entry.get(key) for key in ('id', 'chat_id', 'is_user', 'swipe_id')}
        item['after_unique_prompt_same_chat'] = bool(user and entry.get('chat_id') == user.get('chat_id')
            and type(entry.get('id')) is int and entry['id'] > user['id'])
        item['swipes'] = []
        for swipe in swipes:
            if swipe.get('entry_id') != entry.get('id'):
                continue
            text = swipe.get('swipe')
            timing = swipe.get('timings')
            detail = {key: swipe.get(key) for key in ('id', 'active', 'gen_started', 'gen_finished')}
            detail.update({'text_type': type(text).__name__,
                           'text_characters': len(text) if isinstance(text, str) else None,
                           'text_excerpt': text[:240] if isinstance(text, str) else None,
                           'timings_raw_type': type(timing).__name__})
            if isinstance(timing, str):
                try:
                    timing = json.loads(timing)
                except (ValueError, TypeError) as error:
                    detail['timings_parse_error'] = str(error)[:180]
            if isinstance(timing, dict):
                detail['timings'] = {key: timing.get(key) for key in (
                    'predicted_n', 'predicted_ms', 'prompt_n', 'prompt_ms',
                    'predicted_per_second', 'prompt_per_second')}
            archive = [conversation for conversation in data.get('conversations', [])
                       if conversation.get('id') == str(entry.get('chat_id'))]
            detail['archive_count'] = len(archive)
            messages = archive[0].get('messages', []) if len(archive) == 1 else []
            detail['exact_entry_archived'] = any(message.get('id') == str(entry.get('id'))
                and message.get('text') == text
                and message.get('role') == ('user' if entry.get('is_user') == 1 else 'assistant')
                for message in messages)
            item['swipes'].append(detail)
        item['swipes_omitted'] = max(0, len(item['swipes']) - 2)
        item['swipes'] = item['swipes'][:2]
        result['new_entries'].append(item)
    result['new_entries_omitted'] = max(0, len(result['new_entries']) - 4)
    result['new_entries'] = result['new_entries'][:4]
    try:
        reply = completed_reply(data, prompt, baseline_ids)
        result['strict_native_and_durable_gate_satisfied'] = bool(reply)
        if reply:
            result['verified_reply_excerpt'] = reply['text'][:240]
    except Exception as error:
        result['strict_gate_error'] = type(error).__name__ + ': ' + str(error)[:240]
    for key, name in ((ENGINE_KEY, 'engine'), (SAMPLER_KEY, 'sampler')):
        try:
            state = preference(data, key)
            if name == 'engine':
                config = (state or {}).get('config', {})
                model = (state or {}).get('lastModel') or {}
                result['engine'] = {'config': {key: config.get(key) for key in (
                    'context_length', 'threads', 'batch', 'gpu_layers', 'devices')},
                    'lastModel': {key: model.get(key) for key in ('id', 'name', 'file')}}
            elif isinstance(state, dict):
                values, index = state.get('configList', []), state.get('currentConfigIndex')
                result['sampler'] = {'currentConfigIndex': index,
                    'genamt': values[index].get('data', {}).get('genamt')
                    if type(index) is int and 0 <= index < len(values) else None}
        except Exception as error:
            result[name + '_preference_error'] = type(error).__name__ + ': ' + str(error)[:180]
    return result


def parse_proc_stat(raw):
    """Parse Linux proc stat without splitting spaces/parentheses in comm."""
    opening, closing = raw.find('('), raw.rfind(')')
    if opening < 1 or closing <= opening:
        raise ValueError('Malformed proc stat comm')
    fields = raw[closing + 1:].split()
    return {'pid': int(raw[:opening].strip()), 'comm': raw[opening + 1:closing][:60],
        'state': fields[0], 'user_ticks': int(fields[11]), 'system_ticks': int(fields[12]),
        'threads': int(fields[17]), 'start_ticks': int(fields[19]), 'rss_pages': int(fields[21]),
        'minflt': int(fields[7]), 'majflt': int(fields[9]), 'virtual_bytes': int(fields[20])}


def summarize_backtrace(text, preferred_tids):
    """Bound newly collected debuggerd evidence to actual busy native threads."""
    threads, current = [], None
    for line in text.splitlines():
        match = re.search(r'^"([^"\n]+)"\s+sysTid\s*=\s*(\d+)', line)
        if match:
            current = {'tid': int(match.group(2)), 'comm': match.group(1)[:60], 'frames': []}
            threads.append(current)
        elif current is not None and re.match(r'^\s*#\d+\s+pc\b', line):
            current['frames'].append(line.strip())
    preferred = {tid: index for index, tid in enumerate(preferred_tids)}
    ordered = sorted(threads, key=lambda item: (preferred.get(item['tid'], 999),
        0 if re.search(r'mqt_v_native|llama|ggml', item['comm'], re.I) else 1))
    selected = []
    for item in ordered[:3]:
        frames = item['frames'][:4]
        for frame in item['frames'][4:]:
            if re.search(r'llama|ggml', frame, re.I) and len(frames) < 8:
                frames.append(frame)
        selected.append({'tid': item['tid'], 'comm': item['comm'], 'frame_count': len(item['frames']),
                         'frames': [frame[:180] for frame in frames]})
    return {'thread_count': len(threads), 'selected_threads': selected,
        'translation_frame_count': sum('libndk_translation' in frame
            for item in threads for frame in item['frames']),
        'engine_frame_count': sum(bool(re.search(r'llama|ggml', frame, re.I))
            for item in threads for frame in item['frames'])}


def grouped_native_log(log):
    """Keep engine/error evidence independently of noisy provider and GC logs."""
    lines = log.splitlines()
    provider = [line for line in lines if 'DocumentFile' in line and 'Failed query' in line]
    gc = [line for line in lines if re.search(r'\bGC\b|concurrent.*(?:mark|copy)', line, re.I)]
    noise = set(provider + gc)
    remaining = [line for line in lines if line not in noise]
    return {'provider_query_warning_count': len(provider), 'gc_line_count': len(gc),
        'provider_warning_example': provider[-1][:220] if provider else None,
        'engine_perf_lines': [line[:260] for line in remaining
            if re.search(r'RNLLAMA|llama|ggml|prompt.eval|eval.time|tokens.per.second|completion', line, re.I)][-8:],
        'java_js_error_lines': [line[:260] for line in remaining
            if re.search(r'^[EF]/|AndroidRuntime|ReactNativeJS|exception|failed|'
                         r'HeadlessJsTask|ForegroundService|Cannot start headless task|'
                         r'CatalystInstance|ReactHost', line, re.I)][-8:]}


def icon_target(xml, glyph, header=False):
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    extent = [_bounds(node) for node in root.iter('node') if _bounds(node)]
    width, height = max(item[2] for item in extent), max(item[3] for item in extent)
    positions = set()
    for node in root.iter('node'):
        if glyph not in (node.get('text'), node.get('content-desc')) or not _bounds(node):
            continue
        bounds = _bounds(node)
        if header and (bounds[0] < width * 0.60 or bounds[3] > height * 0.20):
            continue  # The empty-model hint repeats file-add; only the real header is actionable.
        ancestor = node
        while ancestor is not None and ancestor.get('clickable') != 'true':
            ancestor = parents.get(ancestor)
        if ancestor is not None and ancestor.get('enabled') != 'false' and _bounds(ancestor):
            positions.add(_bounds(ancestor))
    if len(positions) > 1:
        raise InvariantError('Ambiguous real UI icon; refusing coordinate guesses')
    return next(iter(positions)) if positions else None


def home_drawer_visible(xml):
    # Drawer.Body only renders these source-defined route controls while open.
    # The underlying AI Bot label can remain in Accessibility XML under its overlay.
    return bool(_nodes(xml, 'Drive Memory') and _nodes(xml, 'Sampler'))


class InferenceSmoke(Smoke):
    """Keep every result/capture, reserving native notices for the final proof.

    GitHub limits notice annotations per step. Four fixture notices plus the
    explicit onboarding/settings/load/reply notices and restart proof total at most nine.
    The shared runtime helper and its fresh/upgrade behavior are unchanged.
    """
    notice_phases = {'candidate-install', 'candidate-startup', 'verified-full-model-fixture',
                     'genuine-external-model-selection', 'real-generated-chat-restart'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.probe_enabled = False
        self.probe_preferred = False
        self.probe_count = 0

    def install_probe(self):
        if PROBE_APK.is_symlink() or digest(PROBE_APK) != PROBE_SHA256:
            raise InvariantError('Read-only diagnostic probe does not match its reviewed hash')
        result = self.adb('install', '--no-streaming', '-t', PROBE_APK, timeout=45)
        if not re.search(r'^Success\s*$', result.stdout, re.MULTILINE):
            raise RuntimeError('Diagnostic-only probe installation did not report success')
        return {'test_only': True, 'package': 'com.codestick.uiprobe',
                'apk_sha256': PROBE_SHA256, 'customer_apk_unchanged': True}

    def probe_xml(self):
        if not self.probe_enabled:
            raise InvariantError('Read-only no-idle probe is limited to the sent-response phase')
        def identity():
            pid = self.shell('pidof', PACKAGE, timeout=3, check=False)
            foreground = self.shell('dumpsys', 'activity', 'activities', timeout=5)
            resumed = [line.strip()[:240] for line in foreground.splitlines()
                       if re.search(r'topResumedActivity|mResumedActivity|ResumedActivity', line)]
            if pid != self.probe_expected_pid or not any(PACKAGE + '/' in line for line in resumed):
                raise InvariantError('Read-only UI probe lost the original app process/foreground')
            return {'pid': pid, 'resumed': resumed[-3:]}
        before = identity()
        nonce = uuid.uuid4().hex
        result = self.adb('shell', 'am', 'instrument', '-w', '-r', '-e', 'probe_nonce',
                          nonce, PROBE_COMPONENT, timeout=40)
        xml, metadata = decode_result(result.stdout, nonce)
        metadata['identity_before'], metadata['identity_after'] = before, identity()
        self.probe_count += 1
        prefix = f'no-idle-ui-{self.probe_count:03d}'
        self.write(prefix + '.xml', xml)
        self.write(prefix + '.json', json.dumps(metadata, indent=2) + '\n')
        self.results['no_idle_ui_probe'] = {'reads': self.probe_count,
            'actual_active_package': metadata['probe_active_package'],
            'actual_xml_sha256': metadata['probe_xml_sha256'], 'method': 'accessibility-no-idle',
            'customer_app_apk_unchanged': True, 'grants_or_appdata_injected': False}
        return xml

    def xml(self, maximum=50):
        if not self.probe_enabled:
            return super().xml(maximum)
        if self.probe_preferred:
            return self.probe_xml()
        try:
            return super().xml(min(maximum, 20))
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            self.results['normal_dump_before_no_idle_fallback'] = {
                'error': type(error).__name__ + ': ' + str(error)[:300]}
            self.probe_preferred = True
            return self.probe_xml()

    def phase(self, label, operation):
        started = time.monotonic()
        try:
            self.shell('logcat', '-c', check=False)
            details = operation() or {}
            self.results['phases'][label] = {'passed': True,
                'elapsed_seconds': round(time.monotonic() - started, 2), **details}
            if label in self.notice_phases:
                report_annotation('notice', f'{label}: PASS ' + json.dumps(details, sort_keys=True))
            return True
        except Exception as error:
            self.results['phases'][label] = {'passed': False,
                'elapsed_seconds': round(time.monotonic() - started, 2), 'error': str(error)}
            report_annotation('error', f'{label}: {type(error).__name__}: {error}')
            # The operation's 300s alarm has exited. This separate failure-only
            # budget observes fresh state; it cannot turn the failed gate green.
            owner = getattr(operation, '__self__', None)
            if label == 'real-short-model-response' and isinstance(owner, InferenceTest):
                diagnostic = {}
                try:
                    with budget(90):
                        owner.response_diagnostic(diagnostic)
                except Exception as diagnostic_error:
                    diagnostic['diagnostic_error'] = (type(diagnostic_error).__name__ + ': '
                                                       + str(diagnostic_error)[:240])
                self.results['response_failure_diagnostic'] = diagnostic
                self.write('response-failure-diagnostic.json', json.dumps(diagnostic, indent=2) + '\n')
                for name, keys in (
                    ('state', ('expected_pid', 'current_pid', 'same_pid', 'foreground', 'ui',
                               'ui_dump', 'services', 'slot_metadata', 'snapshot_read_error')),
                    ('snapshot', ('snapshot',)),
                    ('cpu', ('cpu_samples', 'cpu_sample_error')),
                    ('native-backtrace', ('native_backtrace',)),
                    ('logs-native', ('fresh_native_log_groups', 'native_log_error')),
                    ('logs-ui', ('actual_app_log_ui_lines', 'app_log_ui_dump',
                                 'app_log_ui_error', 'diagnostic_error'))):
                    part = {key: diagnostic[key] for key in keys if key in diagnostic}
                    report_annotation('error', 'Fresh response failure ' + name + ': '
                                      + json.dumps(part, sort_keys=True, ensure_ascii=False))
            return False
        finally:
            self.capture(label)
            self.save()


class InferenceTest(_USBTest):
    def __init__(self, smoke):
        super().__init__(smoke)
        self.prompt = 'Say hello in three words. Session Inference' + uuid.uuid4().hex[:10]
        self.model_name, self.baseline_ids = None, set()
        self.last_slot_diagnostics, self._last_diagnostic_text = {}, None
        self._reported_invalid_slot = False
        self.onboarding_character, self.onboarding_revision = None, None

    def _diagnostic_xml(self, diagnostic, key, name):
        """Preserve real dump output even when CLI exits0 without creating XML."""
        path = '/sdcard/ai-drive-response-diagnostic.xml'
        metadata = diagnostic.setdefault(key, {})
        began = time.monotonic()
        self.s.shell('rm', '-f', path, timeout=3)
        try:
            result = self.s.adb('shell', 'uiautomator', 'dump', path, timeout=15, check=False)
            metadata.update({'exit_code': result.returncode, 'stdout': result.stdout[:600],
                             'stderr': result.stderr[:400]})
        except subprocess.TimeoutExpired as error:
            def excerpt(value):
                return (value.decode(errors='replace') if isinstance(value, bytes) else value or '')[:600]
            metadata.update({'timed_out': True, 'stdout': excerpt(error.stdout), 'stderr': excerpt(error.stderr)})
        finally:
            metadata['elapsed_seconds'] = round(time.monotonic() - began, 2)
        metadata['xml_exists'] = not self.s.adb('shell', 'test', '-f', path,
                                               timeout=3, check=False).returncode
        if not metadata['xml_exists'] or metadata.get('exit_code') or metadata.get('timed_out'):
            if not getattr(self.s, 'probe_enabled', False):
                raise RuntimeError('Actual UI dump did not create XML; metadata=' + json.dumps(metadata))
            self.s.probe_preferred = True
            xml = self.s.probe_xml()
            metadata['fallback'] = self.s.results.get('no_idle_ui_probe')
        else:
            xml = self.s.shell('cat', path, timeout=4)
        ET.fromstring(xml)
        self.s.write(name, xml)
        return xml

    def _cpu_diagnostic(self):
        pid = self.continuity_pid
        if not re.fullmatch(r'\d+', pid or ''):
            return {'unavailable': 'No valid original app PID'}

        def sample():
            current = self.s.shell('pidof', PACKAGE, timeout=3, check=False)
            if current != pid:
                return {'pid': current, 'same_pid': False}
            raw = self.s.shell('cat', '/proc/' + pid + '/stat', timeout=3, check=False)
            process = parse_proc_stat(raw)
            io_raw = self.s.shell('cat', '/proc/' + pid + '/io', timeout=3, check=False)
            io = {name: int(value) for name, value in re.findall(r'^(\w+):\s*(\d+)\s*$', io_raw, re.M)
                  if name in ('rchar', 'wchar', 'syscr', 'syscw', 'read_bytes', 'write_bytes', 'cancelled_write_bytes')}
            task_raw = self.s.shell('cat', '/proc/' + pid + '/task/[0-9]*/stat', timeout=5, check=False)
            tasks = []
            for line in task_raw.splitlines():
                try:
                    tasks.append(parse_proc_stat(line))
                except (ValueError, IndexError):
                    continue
            return {'same_pid': True, 'observed_monotonic': time.monotonic(),
                    'process': process, 'io': io, 'tasks': tasks}

        first = sample()
        time.sleep(2)
        second = sample()
        result = {'requested_interval_seconds': 2, 'same_original_pid': bool(
            first.get('same_pid') and second.get('same_pid')), 'samples': [
                {key: item[key] for key in ('same_pid', 'process', 'io') if key in item}
                for item in (first, second)]}
        if not result['same_original_pid']:
            return result
        before, after = first['process'], second['process']
        result['same_process_start_time'] = before['start_ticks'] == after['start_ticks']
        if not result['same_process_start_time']:
            return result
        result['elapsed_seconds'] = round(second['observed_monotonic'] - first['observed_monotonic'], 3)
        result['process_cpu_delta_ticks'] = (after['user_ticks'] + after['system_ticks']
                                             - before['user_ticks'] - before['system_ticks'])
        result['minor_fault_delta'] = after['minflt'] - before['minflt']
        result['major_fault_delta'] = after['majflt'] - before['majflt']
        result['rss_delta_pages'] = after['rss_pages'] - before['rss_pages']
        result['io_deltas'] = {key: second['io'][key] - value for key, value in first['io'].items()
                               if key in second['io']}
        previous = {item['pid']: item for item in first['tasks']}
        top = []
        for item in second['tasks']:
            old = previous.get(item['pid'])
            if old and old['start_ticks'] == item['start_ticks']:
                top.append({key: item[key] for key in ('pid', 'comm', 'state')} | {'cpu_delta_ticks':
                    item['user_ticks'] + item['system_ticks'] - old['user_ticks'] - old['system_ticks']})
        top.sort(key=lambda item: item['cpu_delta_ticks'], reverse=True)
        result['thread_count_observed'] = len(second['tasks'])
        result['top_threads'] = top[:5]
        for item in result['top_threads']:
            item['wchan'] = self.s.shell('cat', f'/proc/{pid}/task/{item["pid"]}/wchan',
                                         timeout=2, check=False)[:100]
        return result

    def _backtrace_diagnostic(self, diagnostic):
        """After the failed gate only, collect one bounded original-process stack."""
        metadata = diagnostic.setdefault('native_backtrace', {})
        pid = self.continuity_pid
        if not re.fullmatch(r'\d+', pid or ''):
            metadata['unavailable'] = 'No valid original PID'
            return
        metadata['same_original_pid_before'] = self.s.shell('pidof', PACKAGE, timeout=3, check=False) == pid
        if not metadata['same_original_pid_before']:
            return
        began = time.monotonic()
        output = ''
        try:
            # Device timeout also bounds a remote debuggerd after host adb exits.
            result = self.s.adb('shell', 'timeout', '25', 'debuggerd', '-b', pid,
                                timeout=30, check=False)
            metadata.update({'exit_code': result.returncode, 'stderr_excerpt': result.stderr[:180]})
            output = result.stdout + '\n' + result.stderr
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
            output = decoded(error.stdout) + '\n' + decoded(error.stderr)
            metadata['host_timeout_seconds'] = 30
        except BudgetExceeded:
            raise
        except Exception as error:
            metadata['read_error'] = type(error).__name__ + ': ' + str(error)[:180]
        finally:
            metadata['elapsed_seconds'] = round(time.monotonic() - began, 2)
        metadata['raw_output_characters'] = len(output)
        self.s.write('response-failure-native-backtrace.txt', output)
        preferred = [item['pid'] for item in diagnostic.get('cpu_samples', {}).get('top_threads', [])]
        metadata.update(summarize_backtrace(output, preferred))
        metadata['same_original_pid_after'] = self.s.shell('pidof', PACKAGE, timeout=3, check=False) == pid

    def response_diagnostic(self, diagnostic):
        """Observe the current disposable app after failure, never replay old evidence."""
        xml = None
        diagnostic['expected_pid'] = self.continuity_pid
        pid = self.s.shell('pidof', PACKAGE, timeout=5, check=False)
        diagnostic.update({'current_pid': pid, 'same_pid': pid == self.continuity_pid})
        try:
            diagnostic['cpu_samples'] = self._cpu_diagnostic()
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic['cpu_sample_error'] = type(error).__name__ + ': ' + str(error)[:200]
        try:
            self._backtrace_diagnostic(diagnostic)
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic.setdefault('native_backtrace', {})['read_error'] = (
                type(error).__name__ + ': ' + str(error)[:180])
        foreground = self.s.shell('dumpsys', 'activity', 'activities', timeout=8, check=False)
        diagnostic['foreground'] = [line.strip()[:220] for line in foreground.splitlines()
            if re.search(r'topResumedActivity|mResumedActivity|ResumedActivity', line)][-3:]
        try:
            services = self.s.shell('dumpsys', 'activity', 'services', PACKAGE, timeout=5, check=False)
            diagnostic['services'] = [line.strip()[:180] for line in services.splitlines()
                if re.search(r'ServiceRecord|isForeground|foregroundId|background|headless', line, re.I)][-5:]
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic['services'] = {'read_error': type(error).__name__}
        try:
            # Bypass _screen's continuity/session guards to record an actual crash UI too.
            xml = self._diagnostic_xml(diagnostic, 'ui_dump', 'response-failure-before-navigation.xml')
            nodes = list(ET.fromstring(xml).iter('node'))
            labels = list(dict.fromkeys(node.get('text', '') for node in nodes
                          if node.get('text') and _bounds(node)))
            diagnostic['ui'] = {'labels': [value[:90] for value in labels[:16]],
                'exact_prompt_in_edittext': any(node.get('class') == 'android.widget.EditText'
                    and node.get('text') == self.prompt for node in nodes),
                'drafts': [node.get('text', '')[:100] for node in nodes
                           if node.get('class') == 'android.widget.EditText'][:2],
                'send_visible': bool(icon_target(xml, SEND)),
                'stop_visible': bool(icon_target(xml, STOP))}
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic['ui'] = {'read_error': type(error).__name__ + ': ' + str(error)[:180]}
        try:
            data = self._snapshots()
            diagnostic['snapshot'] = response_snapshot_diagnostic(data, self.prompt, self.baseline_ids)
            diagnostic['snapshot']['revision_before_send'] = self.current_revision
            if xml and diagnostic['snapshot'].get('strict_native_and_durable_gate_satisfied'):
                words = visible_reply_words(completed_reply(data, self.prompt, self.baseline_ids)['text'])
                rendered = ' '.join(node.get('text', '') for node in ET.fromstring(xml).iter('node'))
                diagnostic['snapshot']['rendered_words_gate_satisfied'] = bool(words) and all(
                    word in rendered for word in words)
                diagnostic['snapshot']['missing_rendered_words'] = [word[:60] for word in words
                                                                   if word not in rendered][:8]
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic['snapshot_read_error'] = type(error).__name__ + ': ' + str(error)[:240]
        diagnostic['slot_metadata'] = self.last_slot_diagnostics
        try:
            # Same original PID only; never mix a replacement process into the evidence.
            if re.fullmatch(r'\d+', self.continuity_pid or ''):
                log = self.s.shell('logcat', '-d', '--pid', self.continuity_pid,
                                   '-v', 'brief', timeout=8, check=False)
                diagnostic['fresh_native_log_groups'] = grouped_native_log(log)
        except BudgetExceeded:
            raise
        except Exception as error:
            diagnostic['native_log_error'] = type(error).__name__ + ': ' + str(error)[:180]
        # Production removes Logger.console.log. Read its actual current Logs UI;
        # the original chat screen and snapshots are recorded before navigation.
        if pid == self.continuity_pid:
            try:
                self._swipe('right')
                self._tap('Logs', maximum=8)
                xml = self._diagnostic_xml(diagnostic, 'app_log_ui_dump', 'response-failure-app-logs.xml')
                diagnostic['actual_app_log_ui_lines'] = [node.get('text', '')[:350]
                    for node in ET.fromstring(xml).iter('node')
                    if node.get('class') == 'android.widget.TextView' and _bounds(node)
                    and re.match(r'^\[(?:INFO|WARN|ERROR|DEBUG)\]', node.get('text', ''))][-8:]
            except BudgetExceeded:
                raise
            except Exception as error:
                diagnostic['app_log_ui_error'] = type(error).__name__ + ': ' + str(error)[:180]

    def onboarding(self):
        """Cheap real Home/default-card gate before model download or inference."""
        self._top()
        self._tap_xml(self._find('Chats'), 'Chats')
        diagnostic = {'character_count': None, 'rows': [], 'home_visible_labels': []}

        def ready():
            data = self._snapshots()
            character, summary = default_character_state(data)
            diagnostic.update(summary)
            xml = self._screen()
            diagnostic['home_visible_labels'] = list(dict.fromkeys(
                node.get('text', '')[:160] for node in ET.fromstring(xml).iter('node')
                if node.get('text') and _bounds(node)))[:25]
            positions = {_bounds(node) for node in _nodes(xml, 'AI Bot')}
            if character is None or len(positions) != 1:
                return None
            self.onboarding_character = {key: character[key] for key in ('id', 'name', 'type')}
            self.onboarding_revision = data['revision']
            return data

        try:
            with budget(30):
                data = self._wait('Actual unique AI Bot Home card and verified snapshot identity', ready, 30)
        except Exception as error:
            diagnostic['slot_metadata'] = self.last_slot_diagnostics
            # This reads only the current disposable app's fresh native diagnostics.
            # Production removes Logger.console.log, so its real Logs UI may be
            # needed to see caught default-card/asset failures; no export occurs.
            try:
                pid = self.s.shell('pidof', PACKAGE, check=False)
                log = self.s.shell('logcat', '-d', '--pid', pid, '-v', 'brief', timeout=15, check=False) if re.fullmatch(r'\d+', pid) else ''
                diagnostic['fresh_native_error_lines'] = [line[:350] for line in log.splitlines()
                    if re.search(r'error|exception|failed|aibot|asset|default.character', line, re.I)][-12:]
            except Exception as log_error:
                diagnostic['fresh_native_log_read_error'] = type(log_error).__name__
            try:
                self.s.capture('onboarding-home-failure')
                self._swipe('right')
                self._tap('Logs', maximum=10)
                self._wait('Actual app Logs screen', lambda: _nodes(self._screen(), 'Logs'), 10)
                xml = self._screen()
                diagnostic['actual_app_log_ui_lines'] = [node.get('text', '')[:400]
                    for node in ET.fromstring(xml).iter('node')
                    if re.search(r'\[ERROR\]|\[WARN\]|aibot|asset|default.character', node.get('text', ''), re.I)][-12:]
            except Exception as log_error:
                diagnostic['actual_app_log_ui_read_error'] = type(log_error).__name__
            self.s.write('onboarding-diagnostic.json', json.dumps(diagnostic, indent=2) + '\n')
            raise InvariantError('Default AI Bot onboarding did not become ready in30s; current operation '
                                 'diagnostic=' + json.dumps(diagnostic, sort_keys=True) +
                                 '; wait_error=' + type(error).__name__) from error
        result = {'actual_ai_bot_card_visible': True, 'verified_snapshot_ai_bot_identity': self.onboarding_character,
                  'verified_revision': data['revision'], 'no_character_created_by_harness': True}
        report_annotation('notice', 'actual-default-ai-bot-onboarding: PASS ' + json.dumps(result, sort_keys=True))
        # Real Home drawer route returns to the connected Memory screen.
        self.open_memory()
        self._wait('Connected Drive Memory after verified Home onboarding',
                   lambda: _nodes(self._screen(), 'Drive connected'), 30)
        self.current_revision = data['revision']
        return result

    def _snapshots(self):
        samples, read_errors = [], {}
        for name in ('drive-memory-a.json', 'drive-memory-b.json'):
            path = self.base + '/' + name
            if self.s.adb('shell', 'test', '-f', path, check=False).returncode:
                samples.append((name, None))
            else:
                try:
                    samples.append((name, self.s.shell('cat', path)))
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    # usb.3 replaces the inactive document. It may disappear
                    # between test -f and cat; the verified partner remains valid.
                    missing = self.s.adb('shell', 'test', '-f', path, check=False).returncode != 0
                    samples.append((name, None))
                    read_errors[name] = {'name': name, 'present': not missing, 'valid': False,
                                         'error_layer': 'read', 'error_type': type(error).__name__,
                                         'missing_after_existence_check': missing}
        data, metadata = select_verified_snapshot(samples)
        for index, item in enumerate(metadata['slots']):
            if item['name'] in read_errors:
                metadata['slots'][index] = read_errors[item['name']]
        metadata['recovered_invalid_other'] = any(
            item.get('present') and not item['valid'] for item in metadata['slots'])
        self.last_slot_diagnostics = metadata
        text = json.dumps(metadata, sort_keys=True)
        self.s.write('inference-snapshot-read-metadata.json', json.dumps(metadata, indent=2) + '\n')
        if text != self._last_diagnostic_text:
            with (self.s.evidence / 'inference-snapshot-read-metadata.jsonl').open('a') as stream:
                stream.write(text + '\n')
            self._last_diagnostic_text = text
        if metadata['recovered_invalid_other'] and not self._reported_invalid_slot:
            report_annotation('warning', 'Actual snapshot read encountered an invalid slot; '
                              'using only highest verified valid snapshot under core recovery contract. '
                              'Current read metadata: ' + text)
            self._reported_invalid_slot = True
        if data is None:
            raise RuntimeError('No verified snapshot in current operation; metadata=' + text)
        return data

    def _icon(self, glyph, header=False):
        target = self._wait('Visible enabled unique native icon',
                            lambda: icon_target(self._screen(), glyph, header), 30)
        left, top, right, bottom = target
        self.s.shell('input', 'tap', str((left + right) // 2), str((top + bottom) // 2))

    def _numeric(self, label, value):
        # A label can enter the bottom of a viewport before its actual field.
        # Scroll that real label upward before selecting; never guess a missing input.
        fields = []
        for attempt in range(3):
            xml = self._find(label)
            labels = _nodes(xml, label)
            if len(labels) != 1:
                raise InvariantError('Numeric setting label is ambiguous: ' + label)
            bottom = _bounds(labels[0])[3]
            fields = [(bounds[1] - bottom, node, bounds)
                      for node in ET.fromstring(xml).iter('node')
                      if node.get('class') == 'android.widget.EditText' and node.get('enabled') != 'false'
                      and (bounds := _bounds(node)) and 0 <= bounds[1] - bottom < 100]
            fields.sort(key=lambda item: item[0])
            if fields:
                break
            self._swipe('up')
        if not fields or (len(fields) > 1 and fields[0][0] == fields[1][0]):
            raise InvariantError('No unique adjacent numeric input for ' + label)
        _, field, (left, top, right, low) = fields[0]
        old = field.get('text', '')
        if not re.fullmatch(r'\d{1,6}', old):
            raise InvariantError('Refusing to clear unexpected numeric input content')
        self.s.shell('input', 'tap', str((left + right) // 2), str((top + low) // 2))
        self.s.shell('input', 'keyevent', 'KEYCODE_MOVE_END')
        self.s.shell('input', 'keyevent', *(['KEYCODE_DEL'] * len(old)))
        self.s.shell('input', 'text', str(value))
        self._hide_keyboard()
        # Blur explicitly through the actual setting label, even with a hardware keyboard.
        current = self._find(label)
        self._tap_xml(current, label)
        def exact():
            return any(node.get('class') == 'android.widget.EditText' and node.get('text') == str(value)
                       for node in ET.fromstring(self._screen()).iter('node'))
        self._wait('Actual numeric value ' + label, exact, 15)

    def _configuration(self):
        data = self._snapshots()
        state = preference(data, ENGINE_KEY) if data else None
        config = state.get('config', {}) if isinstance(state, dict) else {}
        return data if (config.get('context_length') == 1024 and config.get('threads') == CPU_THREADS
                        and config.get('batch') == 16 and config.get('gpu_layers') == 0
                        and config.get('devices') in ([], ['CPU'])) else None

    def configure(self):
        previous_revision = self.current_revision
        self._top()
        self._tap_xml(self._find('Choose a model'), 'Choose a model')
        self._tap('Settings')
        self._find('CPU Settings', direction='down')
        for label, value in [('Max Context', 1024), ('Threads', CPU_THREADS), ('Batch', 16)]:
            self._numeric(label, value)
        xml = self._screen()
        if _nodes(xml, 'Backend Device'):
            self._tap('CPU')
        def newer_configuration():
            result = self._configuration()
            return result if result and result['revision'] > previous_revision else None
        try:
            data = self._wait('Exact real UI engine settings in a subsequent verified snapshot',
                              newer_configuration, 45)
        except Exception as error:
            raise RuntimeError(str(error) + '; current slot metadata=' +
                               json.dumps(self.last_slot_diagnostics, sort_keys=True)) from error
        self.current_revision = data['revision']
        self._tap('Models')  # Header is Model Settings here, so the footer label is unique.
        self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
        self._top()

        def connected_with_verified_save():
            xml = self._screen()
            counters = [int(match[1]) for node in ET.fromstring(xml).iter('node')
                        if (match := re.search(r'Verified save #(\d+)', node.get('text', '')))]
            return _nodes(xml, 'Drive connected') and counters and max(counters) >= data['revision']
        self._wait('Actual Drive connected and current verified-save counter after shrinking settings',
                   connected_with_verified_save, 30)
        self.s.capture('model-settings-verified-drive-status')
        report_annotation('notice', 'model-settings-durable-save: PASS ' + json.dumps({
            'context_tokens': 1024, 'cpu_threads': CPU_THREADS, 'batch_tokens': 16,
            'previous_revision': previous_revision, 'verified_revision': data['revision'],
            'actual_drive_connected': True, 'actual_verified_save_counter': True,
            'current_slot_metadata': self.last_slot_diagnostics}, sort_keys=True))
        self._tap_xml(self._find('Choose a model'), 'Choose a model')
        return {'context_tokens': 1024, 'cpu_threads': CPU_THREADS, 'batch_tokens': 16,
                'gpu_layers': 0, 'settings_entered_through_real_ui': True,
                'subsequent_verified_revision': data['revision'], 'drive_status_ui_verified': True}

    def select_model(self):
        self._icon(FILE_ADD, header=True)
        self._tap('Use External Model')
        roots = _root_rows(self.s.shell('content', 'query', '--uri',
                           'content://com.android.externalstorage.documents/root',
                           '--projection', 'root_id:title:document_id'))
        primary = [row for row in roots if row['root_id'] == 'primary' and row['document_id'] == 'primary:']
        if len(primary) != 1 or not primary[0]['title']:
            raise InvariantError('The real file picker lacks a unique primary-storage root')
        self._tap('Show roots')
        if not _nodes(self._screen(), primary[0]['title']):
            # Some DocumentsUI file-picker configurations hide internal storage.
            # Enable it only through the actual menu, then reopen the real drawer.
            self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
            self._tap('More options')
            self._tap('Show internal storage')
            self._tap('Show roots')
        self._tap(primary[0]['title'])
        self._tap_xml(self._find('Download'), 'Download')
        self._tap(MODEL_FILE)

        def selected():
            data = self._snapshots()
            if not data:
                return None
            models = rows(data, 'model_data')
            if len(models) > 1:
                raise InvariantError('Fresh one-model fixture has unexpected model entries')
            if len(models) != 1:
                return None
            model = models[0]
            decoded = urllib.parse.unquote(model.get('file_path', ''))
            if model.get('file') != MODEL_FILE or not decoded.startswith('content://com.android.externalstorage.documents/document/primary:Download/'):
                raise InvariantError('Actual model entry is not the selected public Download document')
            if not decoded.endswith('/' + MODEL_FILE):
                raise InvariantError('Selected model document has an unexpected filename')
            if model.get('architecture') != 'llama' or not model.get('name') or model['name'] == 'N/A':
                return None
            if model.get('file_size') != MODEL_BYTES:
                raise InvariantError('Actual selected model file size differs from the verified full GGUF')
            self.model_name = model['name']
            return data

        data = self._wait('Real external GGUF metadata selection', selected, 90)
        self.current_revision = data['revision']
        self._wait('Actual unique model play icon', lambda: icon_target(self._screen(), PLAY), 30)
        return {'selected_through_genuine_documents_ui': True, 'model_bytes': MODEL_BYTES,
                'model_location': 'public phone Download; NOT removable USB',
                'model_name_from_actual_metadata': self.model_name}

    def load_model(self):
        with budget(300):
            began = time.monotonic()
            if not self._configuration():
                raise InvariantError('Low-memory settings changed before model loading')
            self._icon(PLAY)

            def loaded():
                xml = self._screen()
                text = [node.get('text', '') for node in ET.fromstring(xml).iter('node')]
                if not any('Model Loaded' in item and self.model_name in item for item in text):
                    return False
                if not icon_target(xml, CLOSE) or icon_target(xml, PLAY):
                    return False
                data = self._snapshots()
                state = preference(data, ENGINE_KEY) if data else None
                model = state.get('lastModel') if isinstance(state, dict) else None
                return isinstance(model, dict) and model.get('file') == MODEL_FILE

            self._wait('Full GGUF model loaded through real native engine', loaded, 295)
            result = {'actual_full_model_loaded': True, 'model_sha256': MODEL_SHA256,
                      'load_elapsed_seconds': round(time.monotonic() - began, 2),
                      'model_location': 'public phone Download; NOT physical USB'}
            report_annotation('notice', 'real-model-load: PASS ' + json.dumps(result, sort_keys=True))
            return result

    def configure_chat(self):
        self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
        self._top()
        self._tap_xml(self._find('Chats'), 'Chats')
        if self.onboarding_character is None:
            raise InvariantError('No earlier verified real AI Bot onboarding identity')
        def proven_home():
            data = self._snapshots()
            matches = [row for row in rows(data, 'characters')
                       if row.get('id') == self.onboarding_character['id']
                       and row.get('type') == 'character' and row.get('name') == 'AI Bot']
            return data if (data['revision'] >= self.onboarding_revision and len(matches) == 1
                            and _nodes(self._screen(), 'AI Bot')) else None
        data = self._wait('Previously proven AI Bot Home identity in a current verified snapshot', proven_home, 30)
        self._swipe('right')
        self._tap('Sampler')
        self._numeric('Generated Tokens', 16)

        def sample_config():
            data = self._snapshots()
            state = preference(data, SAMPLER_KEY) if data else None
            if not isinstance(state, dict):
                return None
            values = state.get('configList', [])
            index = state.get('currentConfigIndex')
            return data if (type(index) is int and 0 <= index < len(values)
                            and values[index].get('data', {}).get('genamt') == 16) else None

        data = self._wait('Real Generated Tokens setting saved on removable volume', sample_config, 45)
        self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
        def returned_home():
            xml = self._screen()
            return xml if (_nodes(xml, self.onboarding_character['name'])
                           and not _nodes(xml, 'Generated Tokens')) else None
        xml = self._wait('Actual Home after returning from Sampler', returned_home, 30)
        # RouteList pushes Sampler without closing its global settings drawer.
        # Close the actual overlay before using a visible underlying card label;
        # otherwise those coordinates can hit drawer UserInfo → Edit User.
        if home_drawer_visible(xml):
            self.s.shell('input', 'keyevent', 'KEYCODE_BACK')
        def uncovered_home():
            xml = self._screen()
            return xml if (_nodes(xml, self.onboarding_character['name'])
                           and not home_drawer_visible(xml)
                           and not _nodes(xml, 'Edit User')) else None
        xml = self._wait('Uncovered AI Bot Home card with drawer closed', uncovered_home, 30)
        self.s.write('chat-home-drawer-closed.xml', xml)
        self._tap_xml(xml, self.onboarding_character['name'])
        self._wait('Loaded-model chat input', lambda: _nodes(self._screen(), 'Message...'), 30)
        data = self._wait('Initial chat snapshot', self._snapshots, 30)
        self.baseline_ids = {str(row['id']) for row in rows(data, 'chat_entries')}
        self.current_revision = data['revision']
        return {'max_generated_tokens': 16, 'actual_chat_input_ready': True}

    def generate(self):
        with budget(300):
            began = time.monotonic()
            xml = self._screen()
            fields = [node for node in _nodes(xml, 'Message...')
                      if node.get('class') == 'android.widget.EditText']
            if len(fields) != 1:
                raise InvariantError('No unique empty real chat input')
            self._tap_xml(xml, 'Message...')
            self.s.shell('input', 'text', self.prompt.replace(' ', '%s'))
            self._wait('Exact fixture prompt typed in real chat input',
                       lambda: any(node.get('class') == 'android.widget.EditText' and node.get('text') == self.prompt
                                   for node in ET.fromstring(self._screen()).iter('node')), 15)
            self._hide_keyboard()
            self._icon(SEND)
            # Observe the real active accessibility tree if continuous animation
            # prevents UiAutomator's idle gate. No input, grants or appdata changes.
            self.s.probe_enabled = True
            self.s.probe_expected_pid = self.continuity_pid

            def finished():
                xml = self._screen()
                if icon_target(xml, STOP) or not icon_target(xml, SEND):
                    return None
                data = self._snapshots()
                if not data or data['revision'] <= self.current_revision:
                    return None
                reply = completed_reply(data, self.prompt, self.baseline_ids)
                if not reply:
                    return None
                # Actual rendered output must match durable output; hidden snapshot text alone cannot pass.
                text = ' '.join(node.get('text', '') for node in ET.fromstring(xml).iter('node'))
                words = visible_reply_words(reply['text'])
                if not words or not all(word in text for word in words):
                    return None
                return reply

            reply = self._wait('New completed native-model reply and durable conversation', finished, 295)
            result = {'actual_full_model_inference': True, 'durable_conversation_verified': True,
                      'model_profile': MODEL_PROFILE, 'model_sha256': MODEL_SHA256,
                      'model_filename': MODEL_FILE, 'cpu_threads': CPU_THREADS,
                      'virtual_removable_snapshot_sha256_verified': True,
                      'actual_model_output': reply['text'], 'native_timings': reply['timings'],
                      'snapshot_revision': reply['revision'],
                      'elapsed_seconds': round(time.monotonic() - began, 2),
                      'physical_phone_tested': False, 'physical_usb_tested': False,
                      'model_location': 'public phone Download; NOT removable USB'}
            self.s.results['model_inference_tested'] = True
            self.s.results['real_model_inference'] = result
            report_annotation('notice', 'real-model-inference: PASS ' + json.dumps({
                'model_profile': MODEL_PROFILE, 'model_sha256': MODEL_SHA256,
                'model_filename': MODEL_FILE, 'cpu_threads': CPU_THREADS,
                'actual_model_output': reply['text'][:400],
                'predicted_tokens': reply['timings']['predicted_n'],
                'snapshot_revision': reply['revision'], 'physical_phone_tested': False,
                'model_location': result['model_location']}, sort_keys=True))
            return result


def stage_model(smoke, directory):
    model = directory / MODEL_FILE
    smoke.command(['curl', '--fail', '--location', '--silent', '--show-error',
                   '--proto', '=https', '--proto-redir', '=https', '--max-time', '180',
                   '--max-filesize', str(MODEL_BYTES), '--output', model, MODEL_URL], timeout=185)
    if model.stat().st_size != MODEL_BYTES or digest(model) != MODEL_SHA256:
        raise InvariantError('Downloaded full GGUF size/SHA-256 differs from the pinned model')
    smoke.shell('mkdir', '-p', '/sdcard/Download')
    if smoke.adb('shell', 'test', '-e', MODEL_PHONE, check=False).returncode == 0:
        raise InvariantError('The fresh disposable Download fixture already has this model; refusing overwrite')
    smoke.adb('push', model, MODEL_PHONE, timeout=180)
    size = smoke.shell('stat', '-c', '%s', MODEL_PHONE)
    checksum = smoke.shell('sha256sum', MODEL_PHONE, timeout=120).split()
    if size != str(MODEL_BYTES) or not checksum or checksum[0] != MODEL_SHA256:
        raise InvariantError('Full GGUF failed its independent Android-side size/SHA-256 check')
    return {'model_bytes': MODEL_BYTES, 'model_sha256': MODEL_SHA256,
            'runner_and_android_checksums_verified': True,
            'model_location': 'public phone Download; NOT removable USB'}


def run(args):
    select_test_profile(getattr(args, 'model_profile', '3b'), getattr(args, 'cpu_threads', 2))
    evidence = args.evidence.resolve()
    if evidence.exists():
        raise RuntimeError('Use a new evidence directory')
    evidence.mkdir(parents=True)
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix='ai-drive-inference-') as temporary:
        directory = Path(temporary)
        env.update({'ANDROID_AVD_HOME': str(directory / 'avd'), 'ANDROID_USER_HOME': str(directory / 'android'),
                    'ANDROID_HOME': str(args.sdk.resolve()), 'ANDROID_SDK_ROOT': str(args.sdk.resolve())})
        Path(env['ANDROID_AVD_HOME']).mkdir()
        Path(env['ANDROID_USER_HOME']).mkdir()
        smoke, emulator, logcat, handles = InferenceSmoke(args.sdk, evidence, env), None, None, []
        smoke.results.update({'test': 'API35 exact usb.4 APK pinned full-model inference comparison',
                              'model_profile': MODEL_PROFILE, 'model_sha256': MODEL_SHA256,
                              'model_filename': MODEL_FILE, 'cpu_threads': CPU_THREADS,
                              'phone_model_storage': 'Download', 'model_on_removable_storage_tested': False,
                              'guest_ram_mib': 6144, 'fresh_install_only': True})
        stage = 'prerequisites'
        try:
            with budget(1440):
                if platform.machine() != 'x86_64' or not os.access('/dev/kvm', os.R_OK | os.W_OK):
                    raise InvariantError('Requires x86_64 hosted KVM; no software emulation fallback')
                if args.candidate.is_symlink() or digest(args.candidate) != APK_SHA256:
                    raise InvariantError('Candidate is not the exact reviewed APK')
                smoke.results['apk_sha256'] = {'candidate': APK_SHA256}
                stage = 'SDK installation'
                installed = smoke.command([args.sdk / 'cmdline-tools/latest/bin/sdkmanager', 'platform-tools',
                                           'emulator', IMAGE], timeout=360, check=False, input_text='y\n' * 100)
                smoke.write('sdk-install.txt', installed.stdout + installed.stderr)
                if installed.returncode:
                    raise RuntimeError('SDK installation failed: ' + installed.stderr[-1200:])
                smoke.write('system-image-source.properties',
                            (args.sdk / 'system-images/android-35/google_apis/x86_64/source.properties').read_text())
                stage = 'isolated 6GiB AVD creation'
                smoke.command([args.sdk / 'cmdline-tools/latest/bin/avdmanager', 'create', 'avd', '--name', AVD,
                               '--package', IMAGE, '--device', 'pixel_2', '--force'], timeout=90, input_text='no\n')
                emu_log = (evidence / 'emulator.log').open('w')
                handles.append(emu_log)
                emulator = subprocess.Popen([str(args.sdk / 'emulator/emulator'), '-avd', AVD, '-no-window',
                    '-no-metrics', '-no-audio', '-no-snapshot', '-no-boot-anim', '-accel', 'on',
                    '-gpu', 'swiftshader', '-memory', '6144', '-cores', '2', '-partition-size', '6144',
                    '-camera-back', 'none', '-camera-front', 'none', '-feature', '-Vulkan',
                    '-feature', '-HostComposition', '-feature', '-PlayStoreImage', '-port', '5554'],
                    env=env, stdout=emu_log, stderr=subprocess.STDOUT)
                smoke.command([smoke.adb_path, 'start-server'])
                stage = 'stable boot and official ARM translation'
                smoke.wait_boot(maximum=240)
                smoke.shell('input', 'keyevent', '82')
                props = {key: smoke.shell('getprop', key) for key in ['ro.product.cpu.abilist',
                    'ro.dalvik.vm.native.bridge', 'ro.dalvik.vm.isa.arm64', 'ro.build.version.sdk']}
                smoke.write('guest-runtime-properties.json', json.dumps(props, indent=2))
                if ('arm64-v8a' not in props['ro.product.cpu.abilist']
                        or props['ro.dalvik.vm.native.bridge'] != 'libndk_translation.so'
                        or props['ro.dalvik.vm.isa.arm64'] != 'x86_64' or props['ro.build.version.sdk'] != '35'):
                    raise InvariantError('Official API35 image lacks the required advertised ARM64 translation')
                smoke.write('guest-meminfo.txt', smoke.shell('cat', '/proc/meminfo'))
                smoke.write('guest-free-storage.txt', smoke.shell('df', '-k', '/sdcard'))
                log = (evidence / 'logcat.txt').open('w')
                handles.append(log)
                logcat = subprocess.Popen([str(smoke.adb_path), '-s', SERIAL, 'logcat', '-T', '1', '-v',
                    'threadtime', '-b', 'main', '-b', 'system', '-b', 'crash'], env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                stage = 'fresh candidate absence'
                packages = smoke.shell('cmd', 'package', 'list', 'packages', '--user', '0', PACKAGE)
                if re.search(rf'^package:{re.escape(PACKAGE)}\s*$', packages, re.MULTILINE):
                    raise InvariantError('Candidate already installed on new isolated AVD')
                test = InferenceTest(smoke)
                phases = [('candidate-install', lambda: smoke.install(args.candidate, 4)),
                          ('candidate-startup', lambda: smoke.launch('candidate')),
                          ('read-only-accessibility-probe-install', smoke.install_probe),
                          ('virtual-removable-fixture', test.fixture),
                          ('virtual-removable-saf-grant', test.grant),
                          ('actual-default-ai-bot-onboarding', test.onboarding),
                          ('low-memory-model-settings', test.configure),
                          ('verified-full-model-fixture', lambda: stage_model(smoke, directory)),
                          ('genuine-external-model-selection', test.select_model),
                          ('real-full-model-load', test.load_model),
                          ('real-chat-setup', test.configure_chat),
                          ('real-short-model-response', test.generate),
                          ('real-generated-chat-restart', lambda: run_generated_chat_restart(
                              test, expected_rendered_reply=lambda raw: ' '.join(visible_reply_words(raw))))]
                for label, operation in phases:
                    stage = label
                    if not smoke.phase(label, operation):
                        smoke.results['passed'] = False
                        smoke.save()
                        return 1
                    if label == 'candidate-startup':
                        test.continuity_pid = smoke.shell('pidof', PACKAGE)
                smoke.results['passed'] = smoke.results['model_inference_tested'] is True
                smoke.save()
                return 0 if smoke.results['passed'] else 1
        except Exception as error:
            smoke.results.update({'passed': False, 'failure_stage': stage, 'setup_or_runtime_error': str(error)})
            report_annotation('error', f'Full-model inference [{stage}]: {type(error).__name__}: {error}')
            smoke.capture('failure')
            smoke.save()
            return 1
        finally:
            for process in (logcat, emulator):
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for handle in handles:
                handle.close()
            if summary := os.getenv('GITHUB_STEP_SUMMARY'):
                with open(summary, 'a') as stream:
                    stream.write('## Real Android full-model inference experiment\n\n')
                    for name, result in smoke.results['phases'].items():
                        stream.write(f"- {name}: {'PASS' if result['passed'] else 'FAIL'}\n")
                    stream.write('\nExact APK; official API35 x86/ARM translation. Full GGUF in phone Download; '
                                 'snapshots on debug simulated removable storage. No physical phone, USB/SD, '
                                 'model-on-removable-storage or real-phone speed claim.\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--model-profile', choices=tuple(MODEL_PROFILES), default='3b')
    parser.add_argument('--cpu-threads', type=int, choices=(1, 2), default=2)
    raise SystemExit(run(parser.parse_args()))
