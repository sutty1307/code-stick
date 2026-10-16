import json
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from picker_search import (SearchInvariantError, describe_nodes, exact_result,
                           search_action, search_field, select_exact_file_by_search)

PKG = 'com.google.android.documentsui'
WM = 'Physical size: 1080x1920'
NAME = 'Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf'


def node(text='', desc='', cls='android.widget.TextView', box='[20,200][600,260]',
         rid='', enabled='true', clickable='false', focused='false', children=''):
    return (f'<node package="{PKG}" class="{cls}" text="{escape(text, {chr(34): "&quot;"})}" '
            f'content-desc="{escape(desc, {chr(34): "&quot;"})}" resource-id="{rid}" bounds="{box}" '
            f'enabled="{enabled}" clickable="{clickable}" focused="{focused}">{children}</node>')


def screen(contents):
    return '<hierarchy rotation="0">' + node(cls='android.widget.FrameLayout', box='[0,0][1080,1920]', children=contents) + '</hierarchy>'


def action(**kwargs):
    return node(desc='Search', cls='android.widget.ImageButton', box='[900,60][1060,150]',
                rid=PKG + ':id/option_menu_search', clickable='true', **kwargs)


def field(text='', focused='true', **kwargs):
    return node(text=text, cls='android.widget.EditText', box='[90,60][950,150]',
                rid='android:id/search_src_text', clickable='true', focused=focused, **kwargs)


def result(text=NAME, enabled='true', top=300, duplicate_label=False):
    title = node(text=text, rid='android:id/title', box=f'[50,{top + 20}][900,{top + 70}]')
    return node(desc=text if duplicate_label else '', cls='android.widget.LinearLayout',
                box=f'[0,{top}][1080,{top + 140}]', enabled=enabled,
                clickable='true', children=title)


class FakeSmoke:
    def __init__(self, ime=False, wrong_readback=False):
        self.stage, self.calls, self.files = 0, [], {}
        self.ime, self.wrong_readback = ime, wrong_readback
    def shell(self, *args, **kwargs):
        self.calls.append(args)
        if args == ('wm', 'size'): return WM
        if args == ('dumpsys', 'input_method'):
            return 'mInputShown=' + ('true' if self.ime else 'false')
        if args[:2] == ('dumpsys', 'window'): return 'mCurrentFocus=Window{' + PKG + '/.files.FilesActivity}'
        if args[:2] == ('input', 'tap'):
            if self.stage == 0: self.stage = 1
            elif self.stage == 3: self.stage = 4
        elif args[:2] == ('input', 'text'): self.stage = 2
        elif args == ('input', 'keyevent', 'KEYCODE_ENTER'): self.stage = 3
        elif args == ('input', 'keyevent', 'KEYCODE_BACK'): self.ime = False
        return ''
    def write(self, name, value): self.files[name] = json.loads(value)


class FakeTest:
    def __init__(self, **kwargs): self.s = FakeSmoke(**kwargs)
    def _screen(self):
        if self.s.stage == 0: return screen(action())
        if self.s.stage == 1: return screen(field('Search this phone'))
        if self.s.stage == 2: return screen(field('WRONG' if self.s.wrong_readback else NAME))
        return screen(field(NAME, focused='false') + result())


class PickerSearchTests(unittest.TestCase):
    def test_actual_search_action(self):
        self.assertEqual(search_action(screen(action()), WM), (900, 60, 1060, 150))

    def test_disabled_search_action_not_tapped_but_diagnostic_preserved(self):
        xml = screen(action(enabled='false'))
        self.assertIsNone(search_action(xml, WM))
        actual = describe_nodes(xml, WM)['nodes'][0]
        self.assertEqual((actual['desc'], actual['enabled'], actual['bounds']),
                         ('Search', 'false', '[900,60][1060,150]'))

    def test_distinct_search_actions_fail(self):
        extra = action().replace('[900,60][1060,150]', '[500,60][600,150]')
        with self.assertRaises(SearchInvariantError): search_action(screen(action() + extra), WM)

    def test_field_requires_actual_documentsui_and_is_unique(self):
        self.assertEqual(search_field(screen(field()), WM).get('resource-id'), 'android:id/search_src_text')
        with self.assertRaises(SearchInvariantError):
            search_field(screen(field() + field().replace('[90,60][950,150]', '[90,500][950,590]')), WM)
        with self.assertRaises(SearchInvariantError):
            search_field(screen(field()).replace(PKG, 'com.example.other'), WM)

    def test_query_and_substrings_are_not_exact_results(self):
        self.assertIsNone(exact_result(screen(field(NAME) + result(NAME + '.backup')), WM, NAME))

    def test_disabled_result_and_offscreen_results_are_not_tapped(self):
        self.assertIsNone(exact_result(screen(result(enabled='false')), WM, NAME))
        self.assertIsNone(exact_result(screen(result(top=3000)), WM, NAME))

    def test_one_row_and_nested_identical_label_deduplicate(self):
        self.assertEqual(exact_result(screen(result(duplicate_label=True)), WM, NAME), (0, 300, 1080, 440))

    def test_distinct_exact_results_fail(self):
        with self.assertRaises(SearchInvariantError):
            exact_result(screen(result() + result(top=700)), WM, NAME)

    def test_actual_query_readback_submission_and_result_tap(self):
        test = FakeTest()
        with patch('picker_search.time.sleep'):
            proof = select_exact_file_by_search(test, NAME)
        self.assertEqual(test.s.stage, 4)
        self.assertIn(('input', 'text', NAME), test.s.calls)
        self.assertIn(('input', 'keyevent', 'KEYCODE_ENTER'), test.s.calls)
        self.assertNotIn(('input', 'keyevent', 'KEYCODE_BACK'), test.s.calls)
        self.assertEqual(test.s.calls[-1], ('input', 'tap', '540', '370'))
        self.assertTrue(proof['exact_search_query_verified'])
        self.assertFalse(proof['app_import_or_model_execution_claimed'])

    def test_keyboard_back_only_when_explicitly_shown(self):
        test = FakeTest(ime=True)
        with patch('picker_search.time.sleep'):
            select_exact_file_by_search(test, NAME)
        self.assertIn(('input', 'keyevent', 'KEYCODE_BACK'), test.s.calls)
        self.assertEqual(test.s.stage, 4)

    def test_wrong_query_readback_fails_before_enter_and_result(self):
        test, emitted = FakeTest(wrong_readback=True), []
        counter = iter(range(200))
        with patch('picker_search.time.sleep'), patch('picker_search.time.monotonic', side_effect=lambda: next(counter)):
            with self.assertRaisesRegex(RuntimeError, 'exact-search-query-readback'):
                select_exact_file_by_search(test, NAME, emit=lambda label, value: emitted.append((label, value)))
        self.assertNotIn(('input', 'keyevent', 'KEYCODE_ENTER'), test.s.calls)
        self.assertEqual(test.s.stage, 2)
        self.assertIn('picker-search-failure.json', test.s.files)
        self.assertTrue(any(label == 'picker-search-current-window-ime' for label, _ in emitted))
        self.assertTrue(any(label.startswith('picker-search-current-nodes-') for label, _ in emitted))

    def test_unsafe_filename_rejected_without_android_input(self):
        test = FakeTest()
        with self.assertRaises(SearchInvariantError): select_exact_file_by_search(test, 'x;echo.gguf')
        self.assertEqual(test.s.calls, [])


if __name__ == '__main__': unittest.main()
