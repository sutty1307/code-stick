#!/usr/bin/env python3
"""Run an ALREADY INSTALLED test probe; validate nonce/protocol/package/hash.

No installation, app launch, target-app mutation, grants, input, idle wait, or
network. Use only against the reviewed fresh disposable emulator.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import xml.etree.ElementTree as ET

PROTOCOL = 'codestick-no-idle-accessibility-v1'
TARGET = 'com.codestick.aidrive'
COMPONENT = 'com.codestick.uiprobe/com.codestick.uiprobe.UiProbe'
MAX_XML_BYTES = 131072


def decode_result(stdout, nonce):
    fields = {}
    for line in stdout.splitlines():
        prefix = 'INSTRUMENTATION_RESULT: '
        if not line.startswith(prefix):
            continue
        key, separator, value = line[len(prefix):].partition('=')
        if not separator or not key.startswith('probe_'):
            continue
        if key in fields:
            raise RuntimeError('Duplicate probe result field: ' + key)
        fields[key] = value
    if fields.get('probe_protocol') != PROTOCOL or fields.get('probe_nonce') != nonce:
        raise RuntimeError('Probe result nonce/protocol mismatch or no completed result')
    metadata = {key: value for key, value in fields.items() if key != 'probe_xml_base64'}
    if fields.get('probe_status') != 'ok':
        raise RuntimeError('Probe did not return complete UI: ' + json.dumps(metadata, sort_keys=True))
    if (fields.get('probe_active_package') != TARGET or fields.get('probe_truncated') != 'false'
            or fields.get('probe_wait_for_idle') != 'false'):
        raise RuntimeError('Probe active-package/completeness/no-idle gate failed')
    if (fields.get('probe_version') != '2' or fields.get('probe_root_budget_ms') != '5000'
            or fields.get('probe_root_poll_ms') != '150'
            or not 1 <= int(fields.get('probe_root_attempts', '-1')) <= 35
            or not 0 <= int(fields.get('probe_root_elapsed_ms', '-1')) <= 5000
            or not 0 <= int(fields.get('probe_root_null_reads', '-1')) < int(fields['probe_root_attempts'])):
        raise RuntimeError('Probe root retry metadata gate failed')
    if not re.search(r'^INSTRUMENTATION_CODE:\s*-1\s*$', stdout, re.MULTILINE):
        raise RuntimeError('Instrumentation did not finish with RESULT_OK')
    raw = base64.b64decode(fields.get('probe_xml_base64', ''), validate=True)
    if not raw or len(raw) > MAX_XML_BYTES:
        raise RuntimeError('Probe XML is empty or exceeds its fixed limit')
    if hashlib.sha256(raw).hexdigest() != fields.get('probe_xml_sha256'):
        raise RuntimeError('Probe XML SHA-256 mismatch')
    xml = raw.decode('utf-8', errors='strict')
    root = ET.fromstring(xml)
    nodes = list(root.iter('node'))
    if (root.tag != 'hierarchy' or root.get('probe-method') != 'accessibility-no-idle'
            or not nodes or nodes[0].get('package') != TARGET
            or len(nodes) != int(fields.get('probe_nodes', '-1')) or len(nodes) > 256):
        raise RuntimeError('Probe XML hierarchy, target root or node-count gate failed')
    metadata.update({'xml_bytes': len(raw), 'actual_root_class': nodes[0].get('class'),
                     'validated_ui_nodes': len(nodes), 'physical_device_tested': False})
    return xml, metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    metadata_path = args.output.with_suffix(args.output.suffix + '.probe.json')
    if args.output.exists() or metadata_path.exists():
        raise RuntimeError('Use fresh evidence paths; existing files will not be overwritten')
    nonce = secrets.token_hex(16)
    result = subprocess.run([str(args.adb), '-s', args.serial, 'shell', 'am', 'instrument',
                             '-w', '-r', '-e', 'probe_nonce', nonce, COMPONENT],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('Probe instrumentation command failed: ' + result.stderr[-500:])
    xml, metadata = decode_result(result.stdout, nonce)
    args.output.write_text(xml, encoding='utf-8')
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
    # Output only the bounded protocol result, never the XML or app text.
    print(json.dumps({'status': 'ok', 'method': 'accessibility-no-idle',
                      'actual_active_package': metadata['probe_active_package'],
                      'nodes': metadata['validated_ui_nodes'], 'xml_bytes': metadata['xml_bytes'],
                      'xml_sha256': metadata['probe_xml_sha256'],
                      'output': str(args.output)}, sort_keys=True))


if __name__ == '__main__':
    main()
