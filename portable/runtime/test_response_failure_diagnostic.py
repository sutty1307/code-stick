"""Pure harness contracts; these fixtures do not claim Android execution."""
import copy
import json
import signal
import time
import unittest
from unittest.mock import patch

import model_inference_smoke as m


def fixture():
    prompt, reply = 'fixture nonce prompt', 'Hello my friend'
    timing = {'predicted_n': 3, 'predicted_ms': 400.0}
    entries = [{'id': 2, 'chat_id': 7, 'is_user': 1},
               {'id': 3, 'chat_id': 7, 'is_user': 0}]
    swipes = [{'id': 2, 'entry_id': 2, 'active': 1, 'swipe': prompt, 'timings': None},
              {'id': 3, 'entry_id': 3, 'active': 1, 'swipe': reply,
               'timings': json.dumps(timing), 'gen_started': 10, 'gen_finished': 11}]
    data = {'revision': 12, 'applicationState': {'format': 'chatterui-drive-application-v1',
        'tables': [{'name': 'chat_entries', 'rows': entries}, {'name': 'chat_swipes', 'rows': swipes}],
        'preferences': {}}, 'conversations': [{'id': '7', 'messages': [
            {'id': '2', 'role': 'user', 'text': prompt}, {'id': '3', 'role': 'assistant', 'text': reply}]}]}
    return data, prompt


class DiagnosticContract(unittest.TestCase):
    def test_proc_stat_comm_spaces_and_parentheses_preserve_field_offsets(self):
        fields = ['S'] + ['0'] * 38
        fields[11], fields[12], fields[17], fields[19], fields[21] = '120', '45', '6', '9999', '333'
        parsed = m.parse_proc_stat('4321 (mqt (v) js) ' + ' '.join(fields))
        self.assertEqual(parsed, {'pid': 4321, 'comm': 'mqt (v) js', 'state': 'S',
            'user_ticks': 120, 'system_ticks': 45, 'threads': 6, 'start_ticks': 9999, 'rss_pages': 333,
            'minflt': 0, 'majflt': 0, 'virtual_bytes': 0})

    def test_backtrace_prioritizes_observed_busy_threads_and_retains_deep_engine_frame(self):
        raw = '\n'.join(['"main" sysTid=1', '  #00 pc 111 /system/lib64/libart.so',
            '"mqt_v_native" sysTid=8'] + ['  #%02d pc 222 /system/lib64/libndk_translation.so' % i
                for i in range(8)] + ['  #08 pc 333 /data/app/libllama.so (ggml_compute_forward+12)',
            '"RenderThread" sysTid=9', '  #00 pc 444 /system/lib64/libhwui.so'])
        result = m.summarize_backtrace(raw, [8, 9, 1])
        self.assertEqual([item['tid'] for item in result['selected_threads']], [8, 9, 1])
        self.assertIn('libllama.so', result['selected_threads'][0]['frames'][-1])
        self.assertEqual(result['translation_frame_count'], 8)
        self.assertEqual(result['engine_frame_count'], 1)
        self.assertLess(len(json.dumps(result)), 6000)

    def test_backtrace_error_never_substitutes_thread_or_engine_evidence(self):
        result = m.summarize_backtrace('debuggerd: failed to attach to process', [8])
        self.assertEqual(result['thread_count'], 0)
        self.assertEqual(result['selected_threads'], [])
        self.assertEqual(result['engine_frame_count'], 0)

    def test_log_noise_does_not_evict_engine_or_java_error(self):
        native = 'I/RNLlama(12): llama_eval prompt eval time = 12 ms'
        java = 'E/ReactNativeJS(12): Error: actual engine rejection'
        provider = 'W/DocumentFile(12): Failed query: Unsupported Uri'
        gc = 'I/app(12): Background concurrent mark compact GC freed 12MB'
        result = m.grouped_native_log('\n'.join([native, java] + [provider, gc] * 20))
        self.assertEqual(result['engine_perf_lines'], [native])
        self.assertEqual(result['java_js_error_lines'], [java])
        self.assertEqual(result['provider_query_warning_count'], 20)
        self.assertEqual(result['gc_line_count'], 20)

    def test_zero_exit_missing_ui_xml_retains_actual_dump_output(self):
        from types import SimpleNamespace
        class FakeSmoke:
            def shell(self, *args, **kwargs):
                return ''
            def adb(self, *args, **kwargs):
                if args[1] == 'uiautomator':
                    return SimpleNamespace(returncode=0, stdout='ERROR: could not get idle state.\n', stderr='')
                return SimpleNamespace(returncode=1)
        owner = m.InferenceTest.__new__(m.InferenceTest)
        owner.s = FakeSmoke()
        diagnostic = {}
        with self.assertRaisesRegex(RuntimeError, 'did not create XML'):
            owner._diagnostic_xml(diagnostic, 'ui_dump', 'unused.xml')
        self.assertEqual(diagnostic['ui_dump']['exit_code'], 0)
        self.assertFalse(diagnostic['ui_dump']['xml_exists'])
        self.assertIn('could not get idle state', diagnostic['ui_dump']['stdout'])

    def test_native_and_durable_reply_separate_from_ui(self):
        data, prompt = fixture()
        result = m.response_snapshot_diagnostic(data, prompt, {'1'})
        self.assertTrue(result['strict_native_and_durable_gate_satisfied'])
        self.assertEqual(result['exact_prompt_user_count'], 1)
        self.assertEqual(result['new_entries'][1]['swipes'][0]['timings']['predicted_n'], 3)

    def test_no_sent_prompt_and_incomplete_reply_are_distinct(self):
        data, prompt = fixture()
        self.assertEqual(m.response_snapshot_diagnostic(data, 'unsent', {'1'})['exact_prompt_user_count'], 0)
        data['applicationState']['tables'][1]['rows'][1].update({'swipe': '', 'timings': None})
        result = m.response_snapshot_diagnostic(data, prompt, {'1'})
        self.assertEqual(result['exact_prompt_user_count'], 1)
        self.assertFalse(result['strict_native_and_durable_gate_satisfied'])
        self.assertEqual(result['new_entries'][1]['swipes'][0]['text_characters'], 0)

    def test_malformed_timing_and_archive_mismatch_cannot_pass(self):
        data, prompt = fixture()
        data['applicationState']['tables'][1]['rows'][1]['timings'] = '{bad'
        result = m.response_snapshot_diagnostic(data, prompt, {'1'})
        self.assertIn('timings_parse_error', result['new_entries'][1]['swipes'][0])
        self.assertIn('strict_gate_error', result)
        data, prompt = fixture()
        data['conversations'][0]['messages'][1]['text'] = 'different'
        self.assertFalse(m.response_snapshot_diagnostic(data, prompt, {'1'})[
            'strict_native_and_durable_gate_satisfied'])

    def test_failure_diagnostic_runs_after_generation_alarm_and_preserves_failure(self):
        class TimedTest(m.InferenceTest):
            def generate(self):
                with m.budget(0.01):
                    time.sleep(0.1)

            def response_diagnostic(self, result):
                result['fresh_operation'] = True
                result['own_diagnostic_budget_seconds'] = signal.getitimer(signal.ITIMER_REAL)[0]

        owner = TimedTest.__new__(TimedTest)
        smoke = m.InferenceSmoke.__new__(m.InferenceSmoke)
        smoke.results = {'phases': {}}
        smoke.shell = lambda *args, **kwargs: ''
        smoke.capture = lambda *args: None
        smoke.save = lambda: None
        smoke.write = lambda *args: None
        with patch.object(m, 'report_annotation'):
            self.assertFalse(smoke.phase('real-short-model-response', owner.generate))
        phase = smoke.results['phases']['real-short-model-response']
        self.assertFalse(phase['passed'])
        self.assertIn('0.01-second budget', phase['error'])
        diagnostic = smoke.results['response_failure_diagnostic']
        self.assertTrue(diagnostic['fresh_operation'])
        self.assertGreater(diagnostic['own_diagnostic_budget_seconds'], 80)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)


if __name__ == '__main__':
    unittest.main()
