import base64
import hashlib
import unittest

from run_probe import decode_result, PROTOCOL


NONCE = '1234567890abcdef1234567890abcdef'
XML = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<hierarchy rotation="0" probe-method="accessibility-no-idle">'
       '<node package="com.codestick.aidrive" class="android.widget.FrameLayout" '
       'text="I’m here &amp; ready" bounds="[0,0][1080,1920]"/></hierarchy>')


def output(**changes):
    raw = XML.encode()
    fields = {'probe_protocol': PROTOCOL, 'probe_nonce': NONCE, 'probe_status': 'ok',
              'probe_error': '', 'probe_active_package': 'com.codestick.aidrive',
              'probe_nodes': '1', 'probe_truncated': 'false', 'probe_wait_for_idle': 'false',
              'probe_xml_base64': base64.b64encode(raw).decode(),
              'probe_xml_sha256': hashlib.sha256(raw).hexdigest()}
    fields.update({'probe_version': '2', 'probe_root_budget_ms': '5000', 'probe_root_poll_ms': '150',
                   'probe_root_attempts': '3', 'probe_root_null_reads': '2', 'probe_root_elapsed_ms': '301'})
    fields.update(changes)
    return '\n'.join('INSTRUMENTATION_RESULT: ' + key + '=' + value for key, value in fields.items()) + '\nINSTRUMENTATION_CODE: -1\n'


class ProtocolTests(unittest.TestCase):
    def test_complete_current_target_xml_roundtrips_exactly(self):
        xml, metadata = decode_result(output(), NONCE)
        self.assertEqual(xml, XML)
        self.assertEqual(metadata['validated_ui_nodes'], 1)
        self.assertNotIn('probe_xml_base64', metadata)

    def test_nonce_mismatch_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'nonce/protocol'):
            decode_result(output(probe_nonce='0' * 32), NONCE)

    def test_timeout_and_null_root_never_become_success(self):
        for status in ('timeout', 'root_null', 'query_error', 'wrong_active_package', 'truncated'):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'did not return complete UI'):
                decode_result(output(probe_status=status), NONCE)

    def test_wrong_target_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'active-package'):
            decode_result(output(probe_active_package='com.android.launcher3'), NONCE)

    def test_truncated_and_idle_waiting_flags_rejected(self):
        for change in ({'probe_truncated': 'true'}, {'probe_wait_for_idle': 'true'}):
            with self.assertRaisesRegex(RuntimeError, 'completeness/no-idle'):
                decode_result(output(**change), NONCE)

    def test_xml_hash_mismatch_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            decode_result(output(probe_xml_sha256='0' * 64), NONCE)

    def test_duplicate_protocol_fields_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            decode_result(output() + 'INSTRUMENTATION_RESULT: probe_nonce=' + NONCE + '\n', NONCE)

    def test_wrong_node_count_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'node-count'):
            decode_result(output(probe_nodes='2'), NONCE)

    def test_non_ok_instrumentation_code_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'RESULT_OK'):
            decode_result(output().replace('INSTRUMENTATION_CODE: -1', 'INSTRUMENTATION_CODE: 0'), NONCE)

    def test_retry_metadata_cannot_claim_a_late_or_unattempted_success(self):
        for change in ({'probe_root_attempts': '0'}, {'probe_root_elapsed_ms': '5001'},
                       {'probe_root_null_reads': '3'}, {'probe_version': '1'}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'root retry metadata'):
                decode_result(output(**change), NONCE)


if __name__ == '__main__':
    unittest.main()
