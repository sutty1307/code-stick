import copy
import json
import unittest

from generated_chat_restart import reply_identity, exact_bubble_visible


PROMPT = 'Say hello Session Inferenceabc123'
TIMINGS = {'predicted_n': 3, 'predicted_ms': 120.5, 'prompt_n': 25}
STRICT = {'chat_id': 1, 'entry_id': 3, 'text': 'Hello there.', 'timings': TIMINGS, 'revision': 20}


def snapshot():
    return {'revision': 20, 'applicationState': {'tables': [
        {'name': 'chat_entries', 'rows': [
            {'id': 2, 'chat_id': 1, 'is_user': 1}, {'id': 3, 'chat_id': 1, 'is_user': 0}]},
        {'name': 'chat_swipes', 'rows': [
            {'id': 4, 'entry_id': 2, 'active': 1, 'swipe': PROMPT, 'send_date': 10,
             'gen_started': 10, 'gen_finished': 10, 'token_length': 7, 'timings': None},
            {'id': 5, 'entry_id': 3, 'active': 1, 'swipe': 'Hello there.', 'send_date': 11,
             'gen_started': 11, 'gen_finished': 12, 'token_length': 3, 'timings': json.dumps(TIMINGS)}]}]},
        'conversations': [{'id': '1', 'messages': [
            {'id': '2', 'role': 'user', 'text': PROMPT, 'createdAt': '2026-10-16T00:00:10Z'},
            {'id': '3', 'role': 'assistant', 'text': 'Hello there.', 'createdAt': '2026-10-16T00:00:11Z'}]}]}


def bubble(text, visible='true', bounds='[20,100][500,200]'):
    return ('<hierarchy><node class="android.widget.FrameLayout" clickable="false">'
            '<node class="android.view.View" clickable="true" enabled="true">'
            f'<node class="android.widget.TextView" text="{text}" visible-to-user="{visible}" bounds="{bounds}"/>'
            '</node></node></hierarchy>')


class RestartProofTests(unittest.TestCase):
    def test_exact_reloaded_records_compare_identically(self):
        original, restarted = snapshot(), copy.deepcopy(snapshot())
        restarted['revision'] = 22
        self.assertEqual(reply_identity(original, PROMPT, STRICT), reply_identity(restarted, PROMPT, STRICT))

    def test_raw_reply_change_fails(self):
        changed = snapshot()
        changed['applicationState']['tables'][1]['rows'][1]['swipe'] = 'Different reply'
        with self.assertRaisesRegex(ValueError, 'text/timings'):
            reply_identity(changed, PROMPT, STRICT)

    def test_native_timings_change_fails(self):
        changed = snapshot()
        changed['applicationState']['tables'][1]['rows'][1]['timings'] = json.dumps({**TIMINGS, 'predicted_n': 4})
        with self.assertRaisesRegex(ValueError, 'text/timings'):
            reply_identity(changed, PROMPT, STRICT)

    def test_self_consistent_rewritten_ids_do_not_equal_original_identity(self):
        changed = snapshot()
        changed['applicationState']['tables'][0]['rows'][1]['id'] = 30
        changed['applicationState']['tables'][1]['rows'][1]['entry_id'] = 30
        changed['conversations'][0]['messages'][1]['id'] = '30'
        self.assertNotEqual(reply_identity(snapshot(), PROMPT, STRICT),
                            reply_identity(changed, PROMPT, {**STRICT, 'entry_id': 30}))

    def test_archive_change_fails_even_when_sql_reply_unchanged(self):
        changed = snapshot()
        changed['conversations'][0]['messages'][1]['text'] = 'Stale archive'
        with self.assertRaisesRegex(ValueError, 'archived message'):
            reply_identity(changed, PROMPT, STRICT)

    def test_duplicate_nonce_prompt_fails(self):
        changed = snapshot()
        changed['applicationState']['tables'][0]['rows'].append({'id': 7, 'chat_id': 1, 'is_user': 1})
        changed['applicationState']['tables'][1]['rows'].append({'id': 8, 'entry_id': 7, 'active': 1, 'swipe': PROMPT})
        with self.assertRaisesRegex(ValueError, 'nonce prompt'):
            reply_identity(changed, PROMPT, STRICT)

    def test_new_extra_entry_prevents_same_history_identity(self):
        changed = snapshot()
        changed['applicationState']['tables'][0]['rows'].append({'id': 9, 'chat_id': 1, 'is_user': 0})
        self.assertNotEqual(reply_identity(snapshot(), PROMPT, STRICT), reply_identity(changed, PROMPT, STRICT))

    def test_visible_message_bubble_matches_exactly(self):
        self.assertTrue(exact_bubble_visible(bubble('Hello there.'), 'Hello there.'))
        self.assertFalse(exact_bubble_visible(bubble('Say Hello there. Session'), 'Hello there.'))
        self.assertFalse(exact_bubble_visible(bubble('hello there.'), 'Hello there.'))

    def test_header_text_without_message_pressable_is_not_a_reply(self):
        xml = '<hierarchy><node class="android.widget.TextView" text="AI Bot" bounds="[20,100][500,200]"/></hierarchy>'
        self.assertFalse(exact_bubble_visible(xml, 'AI Bot'))

    def test_hidden_zero_area_and_editor_text_are_not_reply_proof(self):
        self.assertFalse(exact_bubble_visible(bubble('Hello there.', visible='false'), 'Hello there.'))
        self.assertFalse(exact_bubble_visible(bubble('Hello there.', bounds='[0,0][0,0]'), 'Hello there.'))
        self.assertFalse(exact_bubble_visible(bubble('Hello there.').replace('android.widget.TextView', 'android.widget.EditText'), 'Hello there.'))


if __name__ == '__main__':
    unittest.main()
