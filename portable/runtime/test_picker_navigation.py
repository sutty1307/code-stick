import unittest
import json
from unittest.mock import patch

from picker_navigation import (display_dimensions, visible_label_target, visible_scroll_target,
                               visible_scroll_bounds, open_visible_download, PickerInvariantError)


def screen(download_y=None, overflow=6000, extra='', rotation='0'):
    download = (f'<node package="com.google.android.documentsui" class="android.widget.TextView" '
                f'text="Download" bounds="[30,{download_y}][330,{download_y+50}]"/>') if download_y is not None else ''
    return (f'<hierarchy rotation="{rotation}"><node package="com.google.android.documentsui" '
            f'class="android.widget.FrameLayout" bounds="[0,0][1080,{overflow}]">'
            f'<node package="com.google.android.documentsui" class="androidx.recyclerview.widget.RecyclerView" '
            f'scrollable="true" enabled="true" bounds="[0,170][1080,{overflow}]">'
            '<node package="com.google.android.documentsui" class="android.widget.TextView" text="Documents" bounds="[30,180][330,230]"/>'
            + download + extra + '</node></node></hierarchy>')


class PickerTests(unittest.TestCase):
    def test_offscreen_extents_never_expand_real_display_or_swipe(self):
        xml = screen(download_y=5000)
        self.assertEqual(display_dimensions('Physical size: 1080x1920', xml), (1080, 1920))
        self.assertIsNone(visible_label_target(xml, 'Download', 1080, 1920))
        self.assertEqual(visible_scroll_bounds(xml, 1080, 1920), (0, 170, 1080, 1920))
        self.assertEqual(visible_scroll_target(xml, 1080, 1920), (540, 1482, 540, 607))

    def test_real_override_and_rotation_are_honored(self):
        self.assertEqual(display_dimensions('Physical size: 1080x1920\nOverride size: 720x1280', screen(rotation='1')), (1280, 720))

    def test_visible_folder_label_has_actual_bounded_target(self):
        self.assertEqual(visible_label_target(screen(download_y=700), 'Download', 1080, 1920), (30, 700, 330, 750))

    def test_distinct_duplicate_labels_are_not_guessed(self):
        extra = '<node package="com.google.android.documentsui" class="android.widget.TextView" text="Download" bounds="[30,900][330,950]"/>'
        with self.assertRaises(PickerInvariantError):
            visible_label_target(screen(download_y=700, extra=extra), 'Download', 1080, 1920)

    def test_wrong_application_hierarchy_fails(self):
        with self.assertRaises(PickerInvariantError):
            visible_scroll_target(screen().replace('com.google.android.documentsui', 'com.example.other'), 1080, 1920)

    def test_disjoint_horizontal_breadcrumb_does_not_hide_file_list(self):
        toolbar = ('<node package="com.google.android.documentsui" class="android.widget.HorizontalScrollView" '
                   'scrollable="true" bounds="[0,70][1080,150]">'
                   '<node package="com.google.android.documentsui" class="android.widget.TextView" '
                   'text="sdk_gphone64_x86_64" bounds="[20,80][300,120]"/></node>')
        xml = screen().replace('</node></hierarchy>', toolbar + '</node></hierarchy>')
        self.assertEqual(visible_scroll_bounds(xml, 1080, 1920), (0, 170, 1080, 1920))

    def test_actual_dir_list_preferred_over_other_list_widget(self):
        drawer = ('<node package="com.google.android.documentsui" class="android.widget.ListView" '
                  'scrollable="true" bounds="[0,0][400,150]">'
                  '<node package="com.google.android.documentsui" class="android.widget.TextView" '
                  'text="Recent" bounds="[20,10][100,80]"/></node>')
        xml = screen().replace('scrollable="true" enabled="true"',
                               'resource-id="com.google.android.documentsui:id/dir_list" scrollable="true" enabled="true"')
        xml = xml.replace('</node></hierarchy>', drawer + '</node></hierarchy>')
        self.assertEqual(visible_scroll_bounds(xml, 1080, 1920), (0, 170, 1080, 1920))

    def test_distinct_file_lists_are_not_guessed(self):
        second = ('<node package="com.google.android.documentsui" class="android.widget.GridView" '
                  'scrollable="true" bounds="[0,0][1080,150]">'
                  '<node package="com.google.android.documentsui" class="android.widget.TextView" '
                  'text="Other" bounds="[20,10][100,80]"/></node>')
        xml = screen().replace('</node></hierarchy>', second + '</node></hierarchy>')
        with self.assertRaises(PickerInvariantError):
            visible_scroll_bounds(xml, 1080, 1920)

    def test_failed_scroll_reports_actual_geometry_without_sending_input(self):
        class Smoke:
            def __init__(self): self.calls, self.trace = [], None
            def shell(self, *args, **kwargs):
                self.calls.append(args)
                return 'Physical size: 1080x1920'
            def write(self, name, value): self.trace = json.loads(value)
        class Test:
            def __init__(self): self.s = Smoke()
            def _screen(self):
                return screen().replace('androidx.recyclerview.widget.RecyclerView', 'android.widget.HorizontalScrollView')
        test = Test()
        with self.assertRaisesRegex(RuntimeError, 'actual picker trace=.*HorizontalScrollView'):
            open_visible_download(test)
        self.assertEqual(test.s.calls, [('wm', 'size')])
        self.assertEqual(test.s.trace[0]['scrollables'][0]['bounds'], [0, 170, 1080, 6000])
        self.assertEqual(test.s.trace[0]['scrollables'][0]['clipped'], [0, 170, 1080, 1920])
        self.assertEqual(test.s.trace[0]['wm_size'], 'Physical size: 1080x1920')

    def test_one_actual_clipped_swipe_reveals_and_taps_folder(self):
        class Smoke:
            def __init__(self): self.calls, self.stage, self.trace = [], 0, None
            def shell(self, *args, **kwargs):
                self.calls.append(args)
                if args == ('wm', 'size'): return 'Physical size: 1080x1920'
                if args[:2] == ('input', 'swipe'): self.stage += 1
                return ''
            def write(self, name, value): self.trace = value
        class Test:
            def __init__(self): self.s = Smoke()
            def _screen(self): return screen(download_y=5000 if self.s.stage == 0 else 700)
        test = Test()
        with patch('picker_navigation.time.sleep'):
            result = open_visible_download(test)
        self.assertEqual(result['actual_scrolls'], 1)
        self.assertEqual(test.s.calls, [('wm', 'size'), ('input', 'swipe', '540', '1482', '540', '607', '350'), ('input', 'tap', '180', '725')])
        self.assertIn('clipped_container', test.s.trace)
        self.assertIn('visible_download_bounds', test.s.trace)


if __name__ == '__main__':
    unittest.main()
