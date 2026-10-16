"""Future proposal only: restore ONE already-proven generated chat after restart.

No code executes on import. Do not call until the unchanged real generation
phase succeeds. Nothing clears data/grants, rewrites snapshots or loads a model.
"""
import copy
import json
import re
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def _table(data, name):
    matches = [table for table in data.get('applicationState', {}).get('tables', [])
               if table.get('name') == name and isinstance(table.get('rows'), list)]
    if len(matches) != 1:
        raise ValueError('Missing/ambiguous actual application table: ' + name)
    return matches[0]['rows']


def _one(items, label):
    if len(items) != 1:
        raise ValueError('Expected one actual ' + label)
    return items[0]


def reply_identity(data, prompt, strict_reply):
    """Select exact preserved records AFTER the existing strict native gate."""
    if not strict_reply:
        raise ValueError('The original strict native/durable reply gate is not satisfied')
    chat_id, assistant_id = strict_reply['chat_id'], strict_reply['entry_id']
    entries, swipes = _table(data, 'chat_entries'), _table(data, 'chat_swipes')
    user = _one([entry for entry in entries if entry.get('chat_id') == chat_id
                 and entry.get('is_user') == 1 and any(
                     swipe.get('entry_id') == entry['id'] and swipe.get('active') == 1
                     and swipe.get('swipe') == prompt for swipe in swipes)], 'nonce prompt entry')
    assistant = _one([entry for entry in entries if entry.get('id') == assistant_id
                      and entry.get('chat_id') == chat_id and entry.get('is_user') == 0], 'generated assistant entry')
    user_swipe = _one([swipe for swipe in swipes if swipe.get('entry_id') == user['id']
                      and swipe.get('active') == 1], 'active user swipe')
    assistant_swipe = _one([swipe for swipe in swipes if swipe.get('entry_id') == assistant_id
                           and swipe.get('active') == 1], 'active generated swipe')
    timings = assistant_swipe.get('timings')
    if isinstance(timings, str):
        timings = json.loads(timings)
    if assistant_swipe.get('swipe') != strict_reply['text'] or timings != strict_reply['timings']:
        raise ValueError('Generated text/timings differ from the unchanged native proof')
    archive = _one([item for item in data.get('conversations', [])
                    if item.get('id') == str(chat_id)], 'matching archived conversation')
    archived_messages = []
    for identifier, role, text in ((user['id'], 'user', prompt), (assistant_id, 'assistant', strict_reply['text'])):
        message = _one([item for item in archive.get('messages', []) if item.get('id') == str(identifier)
                        and item.get('role') == role and item.get('text') == text], 'exact archived message')
        archived_messages.append(copy.deepcopy(message))
    def swipe_record(swipe):
        record = {key: copy.deepcopy(swipe.get(key)) for key in
                  ('id', 'entry_id', 'active', 'swipe', 'send_date', 'gen_started', 'gen_finished', 'token_length')}
        raw = swipe.get('timings')
        record['timings'] = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
        return record
    return {'chat_id': chat_id, 'user_entry_id': user['id'], 'assistant_entry_id': assistant_id,
            'user_swipe': swipe_record(user_swipe), 'assistant_swipe': swipe_record(assistant_swipe),
            'archived_messages': archived_messages,
            'all_chat_entry_ids': sorted(entry['id'] for entry in entries if entry.get('chat_id') == chat_id)}


def _visible_bubble_text(value):
    """Remove ONE source-inserted LTR textgroup wrapper, only on observed UI.

    Markdown.tsx:279-281 emits LRI + rendered children + PDI for an LTR run.
    Internal/repeated/unmatched isolates and RTL/multiple runs stay unsupported.
    Canonical expected text and all native/USB/archive records are unchanged.
    """
    if value.startswith('\u2066') and value.endswith('\u2069'):
        value = value[1:-1]
    if any(character in value for character in '\u2066\u2067\u2068\u2069'):
        return None
    return ' '.join(value.split())


def bubble_match_diagnostic(xml, text, maximum=2):
    """Bounded current matching-text ancestry; never changes the UI predicate."""
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    expected = ' '.join(text.split())
    matches = []
    for node in root.iter('node'):
        if _visible_bubble_text(node.get('text', '')) != expected:
            continue
        path, ancestor = [], parents.get(node)
        for _ in range(5):
            if ancestor is None:
                break
            path.append({key: ancestor.get(key, '')[:64] for key in
                         ('class', 'clickable', 'enabled', 'bounds')})
            ancestor = parents.get(ancestor)
        matches.append({'candidate': {key: node.get(key, '')[:120] for key in
                        ('class', 'text', 'bounds', 'visible-to-user', 'enabled')},
                        'ancestor_path': path})
        if len(matches) >= maximum:
            break
    return {'matching_candidates': matches}


def exact_bubble_visible(xml, text):
    """Exact TextView inside an actual clickable message bubble, not word bags.

    The source's ChatBubble is a Pressable; ChatFrame's name/time labels are
    outside that Pressable. No coordinates, taps or hidden text are invented.
    Plain short replies work directly. Markdown/typography requires a separately
    reviewed renderer-equivalent expected text, never approximate matching.
    """
    normalized = lambda value: ' '.join(value.split())
    expected = normalized(text)
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter('node'):
        bounds = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', node.get('bounds', ''))
        if (node.get('class') != 'android.widget.TextView'
                or node.get('visible-to-user') == 'false' or not bounds
                or int(bounds[3]) <= int(bounds[1]) or int(bounds[4]) <= int(bounds[2])
                or _visible_bubble_text(node.get('text', '')) != expected):
            continue
        ancestor = parents.get(node)
        while ancestor is not None:
            if ancestor.get('clickable') == 'true':
                # Header/avatar names have no message-bubble clickable ancestor.
                if ancestor.get('enabled') == 'false':
                    break
                if ancestor.get('class') in {'android.view.View', 'android.view.ViewGroup'} and not any(
                        child.get('class') == 'android.widget.EditText' for child in ancestor.iter('node')):
                    return True
                break
            ancestor = parents.get(ancestor)
    return False


def run_generated_chat_restart(test, expected_rendered_reply=None):
    """Add as a NEW phase only after test.generate passes; no run on import.

    expected_rendered_reply, when supplied, must come from the separately
    reviewed actual parser and the exact proven raw reply. Default comparison
    fails closed on markup differences. Raw USB/native/archive proof is exact.
    """
    from model_inference_smoke import budget, completed_reply, home_drawer_visible
    from runtime_smoke import PACKAGE, COMPONENT
    from usb_memory_smoke import _nodes, InvariantError

    if test.s.results.get('model_inference_tested') is not True:
        raise InvariantError('The original real generated-response phase has not passed')
    if not test.onboarding_character or not test.onboarding_character.get('name'):
        raise InvariantError('No previously proven original character identity')
    remaining = signal.getitimer(signal.ITIMER_REAL)[0]
    if not remaining or remaining < 150:
        raise InvariantError('Need 120s restart + 30s capture reserve inside the existing overall budget; no limits extended')
    with budget(120):
        began = time.monotonic()
        before = test._snapshots()  # Existing genuine-volume checksum/revision reader.
        strict_before = completed_reply(before, test.prompt, test.baseline_ids)
        expected = reply_identity(before, test.prompt, strict_before)
        prior_result = test.s.results.get('real_model_inference', {})
        if (prior_result.get('actual_model_output') != strict_before['text']
                or prior_result.get('native_timings') != strict_before['timings']):
            raise InvariantError('Current durable reply differs from the response phase that passed')
        expected_ui = strict_before['text'] if expected_rendered_reply is None else expected_rendered_reply(strict_before['text'])
        if not isinstance(expected_ui, str) or not expected_ui.strip():
            raise InvariantError('No complete expected rendered reply text')
        old_pid = test.s.shell('pidof', PACKAGE, check=False)
        if old_pid != test.continuity_pid or not re.fullmatch(r'\d+', old_pid):
            raise InvariantError('Original proven app PID was already lost before sanctioned restart')
        framework = test.s.shell('pidof', 'system_server')

        test.s.shell('am', 'force-stop', PACKAGE)
        test._wait('Sanctioned app force-stop completed',
                   lambda: not test.s.shell('pidof', PACKAGE, check=False), 10)
        # The ONLY continuity reset: the intentionally stopped original process
        # is proven absent. No app/probe/UI query runs in this unbound interval.
        test.continuity_pid = None
        test.s.probe_expected_pid = None
        try:
            launch = test.s.shell('am', 'start', '-W', '-n', COMPONENT, timeout=25)
        except subprocess.TimeoutExpired:
            launch = 'Harness am-start wait timeout; pending genuine fresh process/UI readiness'
        test.s.write('generated-chat-restart-launch.txt', launch)
        if 'Error' in launch or ('Status:' in launch and not re.search(r'Status:\s*(?:ok|timeout)\b', launch)):
            raise InvariantError('Android rejected sanctioned relaunch')
        def new_process():
            pid = test.s.shell('pidof', PACKAGE, check=False)
            return pid if re.fullmatch(r'\d+', pid) and pid != old_pid else None
        new_pid = test._wait('One fresh app process after force-stop', new_process, 15)
        test.continuity_pid = new_pid
        test.s.probe_expected_pid = new_pid

        def restored_home():
            if test.s.shell('pidof', 'system_server') != framework:
                raise InvariantError('Android framework restarted during chat restore')
            xml = test._screen()  # Enforces the one newly bound PID from now on.
            return xml if _nodes(xml, 'Recent') and _nodes(xml, test.onboarding_character['name']) else None
        xml = test._wait('Actual restored Home and existing AI Bot', restored_home, 45)
        if home_drawer_visible(xml):
            test.s.shell('input', 'keyevent', 'KEYCODE_BACK')
        def uncovered_home():
            current = test._screen()
            return current if (_nodes(current, 'Recent') and _nodes(current, test.onboarding_character['name'])
                               and not home_drawer_visible(current)) else None
        xml = test._wait('Uncovered restored Home', uncovered_home, 15)
        test._tap_xml(xml, test.onboarding_character['name'])

        def restored_chat():
            xml = test._screen()
            if not any(_nodes(xml, label) for label in ('No Model Loaded', 'Message...')):
                return None
            if not exact_bubble_visible(xml, test.prompt) or not exact_bubble_visible(xml, expected_ui):
                return None
            after = test._snapshots()
            if after['revision'] < before['revision']:
                raise InvariantError('Generated chat rolled back to an earlier USB revision')
            strict_after = completed_reply(after, test.prompt, test.baseline_ids)
            if reply_identity(after, test.prompt, strict_after) != expected:
                raise InvariantError('Restart changed generated chat IDs, raw text, timings, active swipes or archive')
            return after
        try:
            after = test._wait('Exact restored generated chat in actual UI and genuine USB snapshot', restored_chat, 35)
        except RuntimeError as error:
            # The last hierarchy is from this operation's unchanged PID-guarded
            # UI read. Report actual matching candidate classes/ancestry only.
            diagnostic = {}
            try:
                diagnostic = {'user': bubble_match_diagnostic(test.last_ui, test.prompt),
                              'assistant': bubble_match_diagnostic(test.last_ui, expected_ui)}
                test.s.write('generated-chat-restart-bubble-diagnostic.json',
                             json.dumps(diagnostic, indent=2) + '\n')
            except Exception as diagnostic_error:
                diagnostic = {'diagnostic_error': type(diagnostic_error).__name__ + ': ' + str(diagnostic_error)[:160]}
            raise RuntimeError(str(error) + '; actual matching bubble ancestry=' +
                               json.dumps(diagnostic, separators=(',', ':'))) from error
        test.s.capture('generated-chat-restored-after-force-stop')
        result = {'generated_chat_restarted_and_restored': True, 'chat_id': expected['chat_id'],
                  'user_entry_id': expected['user_entry_id'], 'assistant_entry_id': expected['assistant_entry_id'],
                  'old_pid': old_pid, 'new_pid': new_pid, 'sanctioned_force_stop_only_pid_reset': True,
                  'app_data_cleared': False, 'grants_changed': False, 'snapshot_revision_before': before['revision'],
                  'snapshot_revision_after': after['revision'], 'exact_timings_swipes_archive_preserved': True,
                  'actual_user_and_assistant_bubbles_visible': True,
                  'physical_usb_tested': False, 'all_conversations_migration_tested': False,
                  'elapsed_seconds': round(time.monotonic() - began, 2)}
        test.s.results['generated_chat_restart_tested'] = True
        test.s.results['generated_chat_restart'] = result
        return result
