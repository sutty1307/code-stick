"""Optional 1B packaging profile; import has no filesystem/network side effects.

Copy beside each proposed packager/verifier before applying packaging.patch.
The supplied proposal JSON deliberately cannot authorize assembly.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil

APK_SHA256 = 'c833e55ff089903f3bc0e084f10b91516590fa5d9945138723e4ba466508d549'
SOURCE_SHA256 = '1a775d19b53faa8556a295880c303ef5e5f17bf1682d669c59ce79203072722e'
MODEL_NAME = 'Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf'
MODEL_BYTES = 955445792
MODEL_SHA256 = '68336c25367576f49b29f01ee9034658b51bbded6a7f215e934d997de96301e0'
MODEL_REVISION = 'becf36858832feddfea0ace2fabf791797571999'
MODEL_URL = ('https://huggingface.co/mradermacher/Llama-3.2-1B-Instruct-abliterated-GGUF/'
             f'resolve/{MODEL_REVISION}/{MODEL_NAME}?download=true')
PROFILE_ID = 'llama32-1b-q4km'
REVIEWED_PATHS = {
    'README.md', 'START-HERE.txt', 'START-HERE.html', 'PICTURE-GUIDE.pdf',
    'customer-guide/First-Install-usb4.png',
    'customer-guide/After-You-Plug-It-In-usb4.png',
    'REFERENCE-AND-LICENSE-NOTES.txt', 'MODEL-VALIDATION.md', 'MODEL-VALIDATION.json',
    'models/MODEL-CARD.md', 'models/MODEL-DOWNLOAD.txt',
    'models/MODEL-PROVENANCE.json', 'models/NOTICE.txt',
    'models/LLAMA-3.2-LICENSE.txt',
}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def load_profile(path):
    profile = json.loads(Path(path).read_text())
    require(profile.get('document_status') == 'final',
            '1B profile is NONFINAL: review actual native/restart evidence and guide pins first')
    require(profile.get('profile_id') == PROFILE_ID and
            profile.get('bundle_suffix') == '-1B' and profile.get('tag_suffix') == '-1b',
            'Unsupported model profile or colliding bundle/tag suffix')
    require(profile.get('apk_sha256') == APK_SHA256 and
            profile.get('source_sha256') == SOURCE_SHA256,
            '1B profile must retain the exact reviewed usb.4 APK and source')
    expected = {'filename': MODEL_NAME, 'revision': MODEL_REVISION,
                'size_bytes': MODEL_BYTES, 'published_sha256': MODEL_SHA256,
                'download_url': MODEL_URL}
    require(all(profile.get('model', {}).get(k) == v for k, v in expected.items()),
            '1B model pin differs from verified public payload')
    pins = profile.get('reviewed_files_sha256', {})
    require(set(pins) == REVIEWED_PATHS and all(
        isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in pins.values()),
        'Every exact 1B guide, text, provenance and license file requires a reviewed SHA-256')
    require(pins['models/LLAMA-3.2-LICENSE.txt'] ==
            '8cc15535a8a34b41888f644b339a1a9eb428af793a4f5e24df58a3e5b1487d74',
            'Use the verified primary Meta Llama 3.2 license')
    require(pins['models/MODEL-CARD.md'] ==
            'a2c3b545873ba32d3522f922294c2332d91a22d5cc711dbae8455a4c1bf7fd6b',
            'Retain the exact pinned quantizer model card')
    require(profile.get('native_profile') == '1b-comparison' and
            profile.get('model_settings') == {'max_context': 1024, 'threads': 2, 'batch': 16},
            'Review profile code/pins if actual selected Android settings change')
    return profile


def check_run(section):
    run = section.get('run_id')
    require(type(run) is int and run > 0 and section.get('run_url') ==
            f'https://github.com/sutty1307/code-stick/actions/runs/{run}' and
            re.fullmatch('[0-9a-f]{40}', section.get('test_commit', '')) is not None,
            'Evidence requires the actual run ID, matching URL and exact test commit')


def positive_pid(value):
    """Compare shell PID strings numerically without changing the raw evidence."""
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch('[1-9][0-9]*', value):
        return int(value)
    raise ValueError('Restart PID must be a positive integer or canonical ASCII digit string')


def guard_native_restart(validation, model_sha256, model_filename, native_profile, threads, apk_sha256=APK_SHA256):
    require(validation.get('document_status') == 'final' and
            validation.get('apk_build', {}).get('apk_sha256') == apk_sha256,
            'Final evidence for the exact usb.4 APK is required')
    memory = validation.get('fresh_candidate_and_simulated_usb', {})
    require(memory.get('status') == 'passed' and
            memory.get('actual_phase_evidence_supplied') is True,
            'Actual current install/memory/restart test evidence is required')
    check_run(memory)
    section = validation.get('android_model_inference', {})
    require(section.get('status') == 'passed' and section.get('apk_sha256') == apk_sha256 and
            section.get('model_sha256') == model_sha256,
            'Actual Android inference for this exact APK and 1B model is required')
    check_run(section)
    require(type(section.get('job_id')) is int and section['job_id'] > 0 and
            section.get('job_conclusion') == 'success',
            'Native evidence requires the actual successful matrix job ID/conclusion')
    require(isinstance(native_profile, str) and bool(native_profile.strip()),
            'Review and record the exact native profile label')
    phases = section.get('actual_passed_phases', {})
    reply = phases.get('real-model-inference', {})
    require(reply.get('passed') is True and
            isinstance(reply.get('actual_model_output'), str) and
            bool(reply['actual_model_output'].strip()) and
            type(reply.get('predicted_tokens')) is int and 1 <= reply['predicted_tokens'] <= 16 and
            type(reply.get('snapshot_revision')) is int and reply['snapshot_revision'] > 0,
            'Model Loaded, Linux output and native-only microbenchmarks cannot establish an app reply')
    require(reply.get('model_sha256') == model_sha256 and
            reply.get('model_filename') == model_filename and
            reply.get('model_profile') == native_profile and reply.get('cpu_threads') == threads,
            'Actual reply trace must identify the selected model/profile/thread count')
    restored = phases.get('real-generated-chat-restart', {})
    require(restored.get('passed') is True and all(restored.get(key) is True for key in (
        'generated_chat_restarted_and_restored', 'sanctioned_force_stop_only_pid_reset',
        'exact_timings_swipes_archive_preserved', 'actual_user_and_assistant_bubbles_visible')) and
        restored.get('app_data_cleared') is False and restored.get('grants_changed') is False,
        'Actual same generated chat must survive force-stop/relaunch with USB records and visible bubbles')
    for key in ('chat_id', 'user_entry_id', 'assistant_entry_id',
                'snapshot_revision_before', 'snapshot_revision_after'):
        require(type(restored.get(key)) is int and restored[key] > 0,
                'Restart proof lacks actual integer record/revision identifiers')
    old_pid, new_pid = positive_pid(restored.get('old_pid')), positive_pid(restored.get('new_pid'))
    require(old_pid != new_pid and
            restored['snapshot_revision_before'] >= reply['snapshot_revision'] and
            restored['snapshot_revision_after'] >= restored['snapshot_revision_before'],
            'Restart proof must change process and preserve the saved reply revision')


def guard_validation(validation, profile):
    guard_native_restart(validation, MODEL_SHA256, MODEL_NAME, profile['native_profile'],
                         profile['model_settings']['threads'])


def verify_reviewed_files(root, profile):
    root = Path(root).resolve()
    for name, expected in profile['reviewed_files_sha256'].items():
        target = root / name
        require(not target.is_symlink() and target.is_file() and root in target.resolve().parents,
                'Missing/unsafe reviewed 1B overlay file: ' + name)
        require(digest(target) == expected, '1B overlay differs from reviewed file: ' + name)


def verify_selected_model_validation(root):
    """The active generic evidence files must describe this model, never the template's 3B run."""
    root = Path(root)
    require({p.name for p in root.glob('MODEL-VALIDATION.*')} ==
            {'MODEL-VALIDATION.md', 'MODEL-VALIDATION.json'},
            'Review every active generic MODEL-VALIDATION file for the selected 1B model')
    validation = json.loads((root / 'MODEL-VALIDATION.json').read_text())
    expected = {'model_filename': MODEL_NAME, 'model_sha256': MODEL_SHA256,
                'model_bytes': MODEL_BYTES, 'context_tokens': 1024, 'threads': 2,
                'batch_tokens': 16, 'max_output_tokens': 16, 'elapsed_seconds': 4.091,
                'exit_code': 0, 'actual_model_reply': 'Hello.', 'actual_inference_observed': True,
                'requested_three_words_followed': False, 'android_tested': False,
                'android_reply_restart_evidence': False,
                'android_app_evidence_document': 'APP-VALIDATION.json'}
    require(all(type(validation.get(key)) is type(value) and validation[key] == value
                for key, value in expected.items()),
            'Generic MODEL-VALIDATION.json must retain the actual selected 1B Linux record and separate Android scope')
    source = validation.get('source_evidence', {})
    require(source.get('sha256') == '2bb0b7c27232fddfa0d58a667796af20a38addedb673ee6b4cd7e4e37d78c4c9' and
            source.get('original_fields_preserved') is True,
            'Generic model evidence must cite its exact original 1B Linux smoke record')
    original = {key: value for key, value in validation.items() if key not in {
        'model_filename', 'android_reply_restart_evidence', 'android_app_evidence_document', 'source_evidence'}}
    recorded = (json.dumps(original, indent=2) + '\n').encode('utf-8')
    require(hashlib.sha256(recorded).hexdigest() == source['sha256'],
            'Preserve every original 1B Linux smoke field and CLI argument verbatim')
    command = validation.get('command', [])
    require(isinstance(command, list) and all(isinstance(part, str) for part in command),
            'Preserve the actual recorded Linux CLI arguments')
    require('--model' in command and command.count('--model') == 1 and
            command.index('--model') + 1 < len(command) and
            Path(command[command.index('--model') + 1]).name == MODEL_NAME,
            'Recorded Linux CLI must identify the exact selected 1B model')
    for flag, value in {'--ctx-size': '1024', '--threads': '2', '--threads-batch': '2',
                        '--batch-size': '16', '--ubatch-size': '16', '--predict': '16'}.items():
        require(command.count(flag) == 1 and command.index(flag) + 1 < len(command) and
                command[command.index(flag) + 1] == value,
                'Preserve the actual selected 1B Linux CLI values: ' + flag)


def overlay_candidate(root, overlay, model_file, profile):
    root, overlay, model_file = Path(root), Path(overlay), Path(model_file)
    verify_reviewed_files(overlay, profile)
    verify_selected_model_validation(overlay)
    require(not model_file.is_symlink() and model_file.stat().st_size == MODEL_BYTES and
            digest(model_file) == MODEL_SHA256, 'Exact verified 1B model payload is required')
    require(not list((root / 'models').glob('*.gguf')), 'Fresh 1B candidate must contain no inherited model')
    for name in profile['reviewed_files_sha256']:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(overlay / name, target)
    shutil.copy2(model_file, root / 'models' / MODEL_NAME)


def check_seed(seed, profile, metadata, validation):
    seed = Path(seed)
    verify_reviewed_files(seed, profile)
    verify_selected_model_validation(seed)
    require(not list(seed.rglob('*.gguf')), 'CI seed must omit every model payload')
    require(metadata.get('bundle_profile') == PROFILE_ID and
            metadata.get('model') == profile['model'], 'CONTENTS must identify the exact 1B profile/model')
    require(metadata.get('cloud_cpu_inference') == json.loads((seed / 'MODEL-VALIDATION.json').read_text()),
            'CONTENTS Linux summary must preserve the selected 1B record, not template 3B measurements')
    sources = metadata.get('sources', [])
    provenance = json.loads((seed / 'models/MODEL-PROVENANCE.json').read_text())
    require(isinstance(sources, list) and profile['model']['download_url'] in sources and
            provenance['quantizer_model_card']['source_url'] in sources and
            not any('Llama-3.2-3B' in url or 'QuantFactory/' in url for url in sources),
            'CONTENTS active model sources must identify the selected pinned 1B model')
    memory = metadata.get('persistent_memory', {})
    require(memory.get('media_durability_android_tested') is False and
            memory.get('media_durability_validation_reference') == 'APP-VALIDATION.json',
            'CONTENTS must separate media design intent from actual Android durability evidence')
    guide = metadata.get('customer_guide', {})
    require(guide.get('page_images') == ['customer-guide/First-Install-usb4.png',
            'customer-guide/After-You-Plug-It-In-usb4.png'] and
            guide.get('model_settings') == profile['model_settings'],
            'CONTENTS guide paths/settings must match the reviewed 1B illustrations')
    guard_validation(validation, profile)


def configure_ci(namespace, profile):
    values = {'MODEL_NAME': MODEL_NAME, 'MODEL_REVISION': MODEL_REVISION, 'MODEL_BYTES': MODEL_BYTES,
              'MODEL_SHA256': MODEL_SHA256, 'MODEL_URL': MODEL_URL, 'MODEL_PATH': 'models/' + MODEL_NAME,
              'GUIDE_NAME': 'After-You-Plug-It-In-usb4.png',
              'GUIDE_PATH': 'customer-guide/After-You-Plug-It-In-usb4.png',
              'INSTALL_GUIDE_PATH': 'customer-guide/First-Install-usb4.png',
              'KIT_NAME': 'Android-Drive-Kit-with-Memory-v0.10.1-usb.4-1B.zip',
              'TAG': 'android-usb-memory-preview-v0.10.1-usb.4-1b'}
    pins = profile['reviewed_files_sha256']
    values['GUIDE_SHA256'] = pins[values['GUIDE_PATH']]
    values['INSTALL_GUIDE_SHA256'] = pins[values['INSTALL_GUIDE_PATH']]
    namespace.update(values)
