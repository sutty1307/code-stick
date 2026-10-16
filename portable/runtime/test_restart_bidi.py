import copy
import json
import unittest

from generated_chat_restart import (bubble_match_diagnostic, exact_bubble_visible,
                                    reply_identity, _visible_bubble_text)
from test_restart_proof import PROMPT, STRICT, TIMINGS, bubble, snapshot


class RestartBidiTests(unittest.TestCase):
    def test_current_actual_ltr_prompt_and_reply_wrappers_match_exactly(self):
        for text in ('Hello there.', 'Session. Inference. 59c138b79a.',
                     'Say hello in three words. Session Inferenceb06fb5ae3c'):
            with self.subTest(text=text):
                self.assertTrue(exact_bubble_visible(bubble('\u2066' + text + '\u2069'), text))

    def test_only_one_surrounding_pair_is_removed(self):
        for actual in ('\u2066Hello there.', 'Hello there.\u2069',
                       '\u2066\u2066Hello there.\u2069\u2069',
                       '\u2066Hello\u2066 there.\u2069',
                       '\u2066Hello\u2069 there.\u2069',
                       '\u2067Hello there.\u2069', '\u2068Hello there.\u2069',
                       '\u202aHello there.\u202c', '\u200eHello there.'):
            with self.subTest(actual=repr(actual)):
                self.assertFalse(exact_bubble_visible(bubble(actual), 'Hello there.'))

    def test_wrapper_does_not_change_words_case_order_repetition_or_punctuation(self):
        for changed in ('hello there.', 'Hello there', 'there. Hello',
                        'Say Hello there.', 'Hello Hello there.', 'Hello 3 there.'):
            with self.subTest(changed=changed):
                self.assertFalse(exact_bubble_visible(bubble('\u2066' + changed + '\u2069'), 'Hello there.'))

    def test_current_whitespace_normalization_is_preserved(self):
        self.assertTrue(exact_bubble_visible(bubble('\u2066Hello\n there.\u2069'), 'Hello there.'))
        self.assertEqual(_visible_bubble_text('\u2066Hello there.\u2069'), 'Hello there.')

    def test_actual_source_supported_viewgroup_bubble_matches(self):
        xml = bubble('\u2066Hello there.\u2069').replace('class="android.view.View"', 'class="android.view.ViewGroup"')
        self.assertTrue(exact_bubble_visible(xml, 'Hello there.'))

    def test_unsupported_disabled_or_editor_containing_clickable_ancestor_fails(self):
        original = bubble('\u2066Hello there.\u2069')
        unsupported = original.replace('class="android.view.View"', 'class="android.widget.Button"')
        disabled = original.replace('clickable="true" enabled="true"', 'clickable="true" enabled="false"')
        editor = original.replace('</node></node></hierarchy>',
                                  '<node class="android.widget.EditText"/></node></node></hierarchy>')
        for xml in (unsupported, disabled, editor):
            with self.subTest(xml=xml): self.assertFalse(exact_bubble_visible(xml, 'Hello there.'))

    def test_nearest_clickable_ancestor_cannot_be_bypassed(self):
        xml = ('<hierarchy><node class="android.view.ViewGroup" clickable="true" enabled="true">'
               '<node class="android.widget.Button" clickable="true" enabled="true">'
               '<node class="android.widget.TextView" text="\u2066Hello there.\u2069" bounds="[20,100][500,200]"/>'
               '</node></node></hierarchy>')
        self.assertFalse(exact_bubble_visible(xml, 'Hello there.'))

    def test_wrapped_header_hidden_text_and_editor_do_not_pass(self):
        xml = '<hierarchy><node class="android.widget.TextView" text="\u2066AI Bot\u2069" bounds="[20,100][500,200]"/></hierarchy>'
        self.assertFalse(exact_bubble_visible(xml, 'AI Bot'))
        self.assertFalse(exact_bubble_visible(bubble('\u2066Hello there.\u2069', visible='false'), 'Hello there.'))
        self.assertFalse(exact_bubble_visible(bubble('\u2066Hello there.\u2069').replace(
            'android.widget.TextView', 'android.widget.EditText'), 'Hello there.'))

    def test_diagnostic_preserves_actual_candidate_and_ancestor_class(self):
        xml = bubble('\u2066Hello there.\u2069').replace('class="android.view.View"', 'class="android.view.ViewGroup"')
        observed = bubble_match_diagnostic(xml, 'Hello there.')['matching_candidates'][0]
        self.assertEqual(observed['candidate']['text'], '\u2066Hello there.\u2069')
        self.assertEqual(observed['ancestor_path'][0], {
            'class': 'android.view.ViewGroup', 'clickable': 'true', 'enabled': 'true', 'bounds': ''})
        repeated = '<hierarchy>' + xml.removeprefix('<hierarchy>').removesuffix('</hierarchy>') * 4 + '</hierarchy>'
        self.assertEqual(len(bubble_match_diagnostic(repeated, 'Hello there.')
                             ['matching_candidates']), 2)

    def test_raw_native_and_archive_records_are_still_exact(self):
        original = snapshot()
        changed = copy.deepcopy(original)
        changed['applicationState']['tables'][1]['rows'][1]['swipe'] = '\u2066Hello there.\u2069'
        with self.assertRaisesRegex(ValueError, 'text/timings'):
            reply_identity(changed, PROMPT, STRICT)
        self.assertEqual(reply_identity(original, PROMPT, STRICT)['assistant_swipe']['swipe'], 'Hello there.')


if __name__ == '__main__': unittest.main()
