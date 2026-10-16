"""Genuine DocumentsUI Search; no URI, grant, or application-data injection.

Caller retains its exact returned URI, size, checksum, metadata and model gates.
No Android commands run on import. Diagnostic observations come from this call.
"""
import json
import re
import time
import xml.etree.ElementTree as ET


PACKAGES = {'com.android.documentsui', 'com.google.android.documentsui'}
FIELDS = {'android.widget.EditText', 'android.widget.AutoCompleteTextView'}


class SearchInvariantError(Exception):
    """Ambiguity or unsupported state: never swallowed by generic UI retries."""


def _bounds(node):
    match = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', node.get('bounds', ''))
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return (left, top, right, bottom) if right > left and bottom > top else None


def _clip(box, size):
    if box is None:
        return None
    width, height = size
    clipped = (max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3]))
    return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else None


def _tree(xml, wm_size, require_documents=True):
    root = ET.fromstring(xml)
    nodes = list(root.iter('node'))
    if require_documents and (not nodes or nodes[0].get('package') not in PACKAGES):
        raise SearchInvariantError('Actual active hierarchy is not DocumentsUI')
    physical = re.findall(r'^Physical size:\s*(\d+)x(\d+)\s*$', wm_size, re.MULTILINE)
    override = re.findall(r'^Override size:\s*(\d+)x(\d+)\s*$', wm_size, re.MULTILINE)
    selected = override or physical
    if len(physical) != 1 or len(selected) != 1:
        raise SearchInvariantError('Actual wm size is missing or ambiguous')
    size = tuple(map(int, selected[0]))
    rotation = root.get('rotation', '0')
    if min(size) <= 0 or rotation not in ('0', '1', '2', '3'):
        raise SearchInvariantError('Invalid actual display size/rotation')
    if rotation in ('1', '3'):
        size = size[::-1]
    parents = {child: parent for parent in root.iter() for child in parent}
    return root, nodes, parents, size


def _enabled_visible(node, size):
    return (node.get('package') in PACKAGES and node.get('enabled') != 'false'
            and node.get('visible-to-user') != 'false' and _clip(_bounds(node), size))


def search_field(xml, wm_size):
    _, nodes, _, size = _tree(xml, wm_size)
    fields = [node for node in nodes if node.get('class') in FIELDS and _enabled_visible(node, size)]
    preferred = [node for node in fields if node.get('resource-id', '').endswith(':id/search_src_text')]
    selected = preferred or fields
    if len(selected) > 1:
        raise SearchInvariantError('Distinct visible DocumentsUI search fields are ambiguous')
    return selected[0] if selected else None


def _clickable_target(node, parents, size):
    current = node
    while current is not None and current.get('package') in PACKAGES:
        if not _enabled_visible(current, size):
            return None
        if current.get('clickable') == 'true':
            return _clip(_bounds(current), size)
        current = parents.get(current)
    return None


def search_action(xml, wm_size):
    _, nodes, parents, size = _tree(xml, wm_size)
    targets = set()
    for node in nodes:
        if node.get('class') in FIELDS or not _enabled_visible(node, size):
            continue
        resource = node.get('resource-id', '').split(':id/')[-1]
        observed_label = node.get('content-desc', '').casefold() in {'search', 'search files'}
        if resource not in {'option_menu_search', 'menu_search', 'search_button'} and not observed_label:
            continue
        target = _clickable_target(node, parents, size)
        if target:
            targets.add(target)
    if len(targets) > 1:
        raise SearchInvariantError('Distinct actual DocumentsUI Search actions are ambiguous')
    return next(iter(targets)) if targets else None


def exact_result(xml, wm_size, filename):
    _, nodes, parents, size = _tree(xml, wm_size)
    targets = set()
    for node in nodes:
        if node.get('class') in FIELDS or not _enabled_visible(node, size):
            continue
        is_title = node.get('class') == 'android.widget.TextView' and node.get('text') == filename
        if not is_title and node.get('content-desc') != filename:
            continue
        target = _clickable_target(node, parents, size)
        if target:
            targets.add(target)
    if len(targets) > 1:
        raise SearchInvariantError('Distinct exact filename results are ambiguous; refusing selection')
    return next(iter(targets)) if targets else None


def _state_lines(value, patterns, maximum=10):
    lines = [line.strip()[:240] for line in value.splitlines()
             if any(re.search(pattern, line) for pattern in patterns)]
    return lines[:maximum]


def describe_nodes(xml, wm_size, maximum=48):
    """Includes disabled and offscreen nodes, with descriptions and bounds."""
    _, nodes, _, size = _tree(xml, wm_size, require_documents=False)
    important = [node for node in nodes if node.get('text') or node.get('content-desc')
                 or node.get('scrollable') == 'true' or node.get('class') in FIELDS]
    return {'display': list(size), 'wm_size': wm_size[:160], 'nodes': [
        {'package': node.get('package', '')[:80],
         'class': node.get('class', '')[:80], 'id': node.get('resource-id', '')[:100],
         'text': node.get('text', '')[:120], 'desc': node.get('content-desc', '')[:120],
         'enabled': node.get('enabled', ''), 'focused': node.get('focused', ''),
         'clickable': node.get('clickable', ''), 'scrollable': node.get('scrollable', ''),
         'bounds': node.get('bounds', '')[:64],
         'clipped': _clip(_bounds(node), size)} for node in important[:maximum]],
        'important_nodes_omitted': max(0, len(important) - maximum)}


def select_exact_file_by_search(test, filename, maximum_seconds=75, emit=None):
    """Call after choosing the actual primary root; replaces Download+file taps.

    emit, if supplied, receives (label, data) only on failure, from actual current
    UI/window/IME observations. It may use the runtime's native CI annotations.
    A tapped exact result is NOT a successful app import, load or inference claim.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.gguf', filename):
        raise SearchInvariantError('Only the locally pinned safe ASCII GGUF filename may be typed')
    deadline = time.monotonic() + maximum_seconds
    trace, last_xml, wm_size = [], None, ''

    def screen(stage):
        nonlocal last_xml
        if time.monotonic() >= deadline:
            raise RuntimeError('Genuine Search UI reached its bounded deadline')
        last_xml = test._screen()  # Retains unchanged PID and actual app-error checks.
        observation = describe_nodes(last_xml, wm_size, maximum=20)
        trace.append({'stage': stage, **observation})
        if len(trace) > 3:
            trace.pop(1)  # Initial state and latest two actual observations.
        return last_xml

    def wait_for(stage, selector, maximum=20):
        local_deadline = min(deadline, time.monotonic() + maximum)
        while time.monotonic() < local_deadline:
            xml = screen(stage)
            result = selector(xml)
            if result is not None:
                return result
            time.sleep(0.35)
        raise RuntimeError('Actual DocumentsUI condition not observed: ' + stage)

    def tap(box):
        left, top, right, bottom = box
        test.s.shell('input', 'tap', str((left + right) // 2), str((top + bottom) // 2), timeout=5)

    try:
        wm_size = test.s.shell('wm', 'size', timeout=5)
        xml = screen('before-search')
        field = search_field(xml, wm_size)
        if field is None:
            target = wait_for('actual-search-action', lambda value: search_action(value, wm_size), 10)
            tap(target)
            field = wait_for('actual-search-field', lambda value: search_field(value, wm_size), 15)
        _, _, _, size = _tree(last_xml, wm_size)
        tap(_clip(_bounds(field), size))

        def focused(value):
            actual = search_field(value, wm_size)
            return actual if actual is not None and actual.get('focused') == 'true' else None

        field = wait_for('focused-real-search-field', focused, 10)
        previous = field.get('text', '')
        if len(previous) > 256:
            raise SearchInvariantError('Unexpectedly long existing search filter; refusing input')
        # This edits only the genuine temporary search filter. Accessibility can
        # expose a hint as text; deleting that length is harmless for an empty field.
        test.s.shell('input', 'keyevent', 'KEYCODE_MOVE_END', *(['KEYCODE_DEL'] * (len(previous) + 1)), timeout=8)
        test.s.shell('input', 'text', filename, timeout=8)

        def exact_query(value):
            actual = search_field(value, wm_size)
            return actual if actual is not None and actual.get('text') == filename else None

        wait_for('exact-search-query-readback', exact_query, 12)
        test.s.shell('input', 'keyevent', 'KEYCODE_ENTER', timeout=5)
        target = wait_for('enabled-exact-filename-result',
                          lambda value: exact_result(value, wm_size, filename), 25)
        # Hide only an explicitly shown IME before re-reading/reacquiring the
        # result. BACK without that guard would leave Search or cancel the picker.
        ime = test.s.shell('dumpsys', 'input_method', timeout=5)
        if re.search(r'\b(?:mInputShown|isInputViewShown)=true\b', ime):
            test.s.shell('input', 'keyevent', 'KEYCODE_BACK', timeout=5)
            target = wait_for('exact-result-after-ime-dismissal',
                              lambda value: exact_result(value, wm_size, filename), 10)
        tap(target)
        return {'genuine_documents_ui_search': True, 'exact_search_query_verified': True,
                'exact_enabled_filename_result_tapped': True, 'filename': filename,
                'app_import_or_model_execution_claimed': False}
    except Exception as error:
        if type(error).__name__ == 'BudgetExceeded':
            raise
        diagnostic = {'reason': type(error).__name__ + ': ' + str(error)[:300]}
        if last_xml is not None:
            diagnostic['actual_nodes'] = describe_nodes(last_xml, wm_size)
        for name, args, patterns in (
            ('window', ('dumpsys', 'window', 'windows'),
             (r'mCurrentFocus', r'mFocusedApp', r'mTopFocusedDisplay', r'mObscuringWindow', r'mInputMethodTarget', r'mIme')),
            ('ime', ('dumpsys', 'input_method'),
             (r'mInputShown', r'isInputViewShown', r'mServedView', r'mCurFocusedWindow', r'mShowRequested', r'EditorInfo'))):
            try:
                diagnostic[name] = _state_lines(test.s.shell(*args, timeout=5), patterns)
            except Exception as failure:
                diagnostic[name + '_read_error'] = type(failure).__name__ + ': ' + str(failure)[:160]
        test.s.write('picker-search-failure.json', json.dumps(diagnostic, indent=2) + '\n')
        if emit:
            emit('picker-search-current-window-ime', {key: diagnostic[key] for key in diagnostic if key != 'actual_nodes'})
            actual = diagnostic.get('actual_nodes', {})
            nodes = actual.get('nodes', [])
            for offset in range(0, len(nodes), 8):
                emit('picker-search-current-nodes-' + str(offset // 8 + 1),
                     {**{key: value for key, value in actual.items() if key != 'nodes'}, 'nodes': nodes[offset:offset + 8]})
        concise = [{'stage': item['stage'], 'display': item['display'],
                    'labels': [{'text': node['text'], 'desc': node['desc'],
                                'enabled': node['enabled'], 'bounds': node['bounds']}
                               for node in item['nodes'][:8]]} for item in trace]
        raise RuntimeError(diagnostic['reason'] + '; actual search trace=' +
                           json.dumps(concise, separators=(',', ':'))) from error
    finally:
        test.s.write('picker-search-trace.json', json.dumps(trace, indent=2) + '\n')
