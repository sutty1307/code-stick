"""Local proposal: actual DocumentsUI scrolling clipped to the real display.

No execution on import. Opens only the visible Download directory; caller's
unchanged file-selection/exact URI/size/model checks must run afterwards.
"""
import json
import re
import time
import xml.etree.ElementTree as ET


class PickerInvariantError(Exception):
    pass


PACKAGES = {'com.android.documentsui', 'com.google.android.documentsui'}
FILE_LIST_CLASSES = {
    'androidx.recyclerview.widget.RecyclerView',
    'android.support.v7.widget.RecyclerView',
    'android.widget.GridView',
    'android.widget.ListView',
    'android.widget.ScrollView',  # Actual API35 DocumentsUI hierarchy in run38875547356.
}


def display_dimensions(wm_size, xml):
    physical = re.findall(r'^Physical size:\s*(\d+)x(\d+)\s*$', wm_size, re.MULTILINE)
    override = re.findall(r'^Override size:\s*(\d+)x(\d+)\s*$', wm_size, re.MULTILINE)
    selected = override if override else physical
    if len(selected) != 1:
        raise PickerInvariantError('Actual wm size did not identify one display size')
    width, height = map(int, selected[0])
    if width <= 0 or height <= 0:
        raise PickerInvariantError('Actual display has invalid dimensions')
    rotation = ET.fromstring(xml).get('rotation', '0')
    if rotation not in ('0', '1', '2', '3'):
        raise PickerInvariantError('Actual hierarchy has an unsupported rotation')
    return (height, width) if rotation in ('1', '3') else (width, height)


def _bounds(node):
    match = re.fullmatch(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', node.get('bounds', ''))
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return (left, top, right, bottom) if right > left and bottom > top else None


def _clip(bounds, width, height):
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    result = (max(0, left), max(0, top), min(width, right), min(height, bottom))
    return result if result[2] > result[0] and result[3] > result[1] else None


def _document_nodes(xml):
    root = ET.fromstring(xml)
    all_nodes = list(root.iter('node'))
    if not all_nodes or all_nodes[0].get('package') not in PACKAGES:
        raise PickerInvariantError('Actual active hierarchy is not genuine DocumentsUI')
    return [node for node in all_nodes if node.get('package') in PACKAGES
            and node.get('enabled') != 'false' and node.get('visible-to-user') != 'false']


def visible_label_target(xml, label, width, height):
    matches = []
    for node in _document_nodes(xml):
        if not any(node.get(key, '').casefold() == label.casefold() for key in ('text', 'content-desc')):
            continue
        clipped = _clip(_bounds(node), width, height)
        if clipped:
            matches.append((node, clipped))
    if not matches:
        return None
    positions = {bounds for _, bounds in matches}
    if len(positions) == 1:
        return next(iter(positions))
    labels = [(node, bounds) for node, bounds in matches
              if node.get('class') == 'android.widget.TextView'
              and node.get('text', '').casefold() == label.casefold()]
    if len(labels) == 1:
        _, target = labels[0]
        if all(box[0] <= target[0] and box[1] <= target[1]
               and box[2] >= target[2] and box[3] >= target[3] for box in positions):
            return target  # One real label and its enclosing accessible row.
    raise PickerInvariantError('Distinct visible DocumentsUI controls have the same exact label: ' + label)


def _visible_scroll_nodes(xml, width, height):
    return [(node, _clip(_bounds(node), width, height))
            for node in _document_nodes(xml) if node.get('scrollable') == 'true'
            and _clip(_bounds(node), width, height)]


def _has_visible_descendant_label(node, width, height):
    return any(child is not node and child.get('package') in PACKAGES
               and child.get('class') == 'android.widget.TextView'
               and child.get('text', '').strip()
               and child.get('visible-to-user') != 'false'
               and _clip(_bounds(child), width, height)
               for child in node.iter('node'))


def visible_scroll_bounds(xml, width, height):
    candidates = []
    for node, clipped in _visible_scroll_nodes(xml, width, height):
        # DocumentsUI also exposes a disjoint horizontal breadcrumb scroller.
        # Only actual file-list widgets with actual visible labels may scroll.
        is_dir_list = node.get('resource-id', '').endswith(':id/dir_list')
        if node.get('class') == 'android.widget.HorizontalScrollView':
            continue
        if not is_dir_list and node.get('class') not in FILE_LIST_CLASSES:
            continue
        if clipped[2] - clipped[0] < 64 or clipped[3] - clipped[1] < 64:
            continue
        if _has_visible_descendant_label(node, width, height):
            candidates.append((is_dir_list, clipped))
    preferred = [box for is_dir_list, box in candidates if is_dir_list]
    boxes = set(preferred or [box for _, box in candidates])
    if not boxes:
        raise PickerInvariantError('No actual visible DocumentsUI file-list scrollable container')
    containing = [box for box in boxes if all(box[0] <= other[0] and box[1] <= other[1]
                  and box[2] >= other[2] and box[3] >= other[3] for other in boxes)]
    if len(containing) != 1:
        raise PickerInvariantError('Ambiguous disjoint visible DocumentsUI file-list scroll containers')
    return containing[0]


def visible_scroll_target(xml, width, height):
    left, top, right, bottom = visible_scroll_bounds(xml, width, height)
    return ((left + right) // 2, top + (bottom - top) * 3 // 4,
            (left + right) // 2, top + (bottom - top) // 4)


def open_visible_download(test, max_swipes=8, maximum_seconds=60):
    """Replace ONLY the old _find('Download') tap after selecting primary root.

    Keep caller's subsequent real model-file tap and final strict document proof.
    This uses observed container/display bounds, never guessed picker positions.
    Existing outer runtime budget still bounds any blocking Android UI dump.
    """
    deadline = time.monotonic() + maximum_seconds
    wm_size = test.s.shell('wm', 'size', timeout=10)
    trace = []
    try:
        for attempt in range(max_swipes + 1):
            if time.monotonic() >= deadline:
                raise RuntimeError('Visible Download navigation reached its bounded deadline')
            xml = test._screen()  # Preserve existing process/error guards.
            width, height = display_dimensions(wm_size, xml)
            labels = list(dict.fromkeys(node.get('text', '')[:56] for node in _document_nodes(xml)
                          if node.get('text') and _clip(_bounds(node), width, height)))[:8]
            observation = {
                'after_scrolls': attempt, 'display': [width, height],
                'wm_size': wm_size[:160], 'visible_labels': labels,
                'scrollables': [
                    {'resource_id': node.get('resource-id', '')[:120],
                     'class': node.get('class', '')[:96],
                     'bounds': list(_bounds(node)), 'clipped': list(clipped)}
                    for node, clipped in _visible_scroll_nodes(xml, width, height)][:2],
            }
            trace.append(observation)
            if len(trace) > 3:
                trace.pop(1)  # Preserve the actual initial view and two latest views.
            target = visible_label_target(xml, 'Download', width, height)
            if target:
                observation['visible_download_bounds'] = list(target)
                left, top, right, bottom = target
                test.s.shell('input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
                return {'visible_download_directory_tapped': True, 'actual_scrolls': attempt,
                        'actual_display_dimensions': [width, height], 'used_genuine_documents_ui': True}
            if attempt == max_swipes:
                break
            observation['clipped_container'] = list(visible_scroll_bounds(xml, width, height))
            coordinates = visible_scroll_target(xml, width, height)
            observation['actual_swipe'] = list(coordinates)
            test.s.shell('input', 'swipe', *map(str, coordinates), '350')
            time.sleep(0.35)
        raise RuntimeError('Exact visible Download folder was not found after bounded real-container scrolling')
    except Exception as error:
        if type(error).__name__ == 'BudgetExceeded':
            raise
        # This is a bounded diagnostic from the current UI operation, not a
        # replay of artifact contents. Root's ordinary phase error surfaces it.
        raise RuntimeError(str(error)[:400] + '; actual picker trace=' + json.dumps(trace, separators=(',', ':'))) from error
    finally:
        test.s.write('picker-download-scroll-trace.json', json.dumps(trace, indent=2) + '\n')
