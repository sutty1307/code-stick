#!/usr/bin/env python3
"""Review a seed, assemble a complete kit, or explicitly upload a CI-only draft.

No invocation publishes a public release. `inspect` is local/read-only; `assemble`
only downloads the public pinned model; `draft --create-draft` requires a normal
GitHub Actions runner and its repository-scoped GH_TOKEN.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from model_profile import load_profile, configure_ci, check_seed, guard_native_restart

REPOSITORY = "sutty1307/code-stick"
BRANCH = "portable/android-usb-download"
VERSION = "0.10.1-usb.4"
VERSION_CODE = 4
APPLICATION_ID = "com.codestick.aidrive"
APK_NAME = f"AI-Drive-Offline_v{VERSION}.apk"
APK_SHA256 = "c833e55ff089903f3bc0e084f10b91516590fa5d9945138723e4ba466508d549"
SOURCE_NAME = f"AI-Drive-Offline-v{VERSION}-source.zip"
SOURCE_SHA256 = "1a775d19b53faa8556a295880c303ef5e5f17bf1682d669c59ce79203072722e"
GUIDE_NAME = "After-You-Plug-It-In-usb4.png"
GUIDE_PATH = f"customer-guide/{GUIDE_NAME}"
GUIDE_SHA256 = "fb2721c560d7f89a1b25d9cd608709c2d48b489e8e3f60e3bf69a6f21a8d7eb8"
INSTALL_GUIDE_PATH = "customer-guide/First-Install-usb4.png"
INSTALL_GUIDE_SHA256 = "dcc9e6e89038f2653a731655159bc04adc0e6b65a6d3c9bb7c4d66bf434ef994"
MODEL_NAME = "Llama-3.2-3B-Instruct-abliterated.Q4_K_M.gguf"
MODEL_REVISION = "120d74373f679f9d9e0463b435dfd3fb802d9eb0"
MODEL_BYTES = 2019377472
MODEL_SHA256 = "e1f2b428e316d8bd1cb5ca13e9649329e299d951fbcee6b3cd0e9f1d646c9fa7"
MODEL_URL = (
    "https://huggingface.co/QuantFactory/Llama-3.2-3B-Instruct-abliterated-GGUF/resolve/"
    f"{MODEL_REVISION}/{MODEL_NAME}?download=true"
)
MODEL_PATH = f"models/{MODEL_NAME}"
KIT_NAME = f"Android-Drive-Kit-with-Memory-v{VERSION}.zip"
TAG = f"android-usb-memory-preview-v{VERSION}"
PROFILE = None
LIMIT = 2 * 1024**3
CHUNK = 2 * 1024**2


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def relative_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Invalid distribution path")
    path = PurePosixPath(name)
    # Reject noncanonical spellings too: repeated slashes and ./ are not distinct files.
    if (not path.parts or path.is_absolute() or path.as_posix() != name
            or any(p in (".", "..") or ":" in p for p in path.parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f"Unsafe distribution path: {name}")
    return path


def safe_path(root, name):
    relative_name(name)
    target = Path(root) / name
    if Path(root).resolve() not in target.resolve().parents:
        raise ValueError(f"Distribution path escapes its root: {name}")
    return target


def files_under(root):
    result = {}
    for path in Path(root).rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Distribution symlink is forbidden: {path.name}")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            relative_name(name)
            if path.suffix.lower() in {".jks", ".keystore", ".pem", ".p12", ".pfx"} or path.name.startswith(".env"):
                raise ValueError(f"Private signing/environment material is forbidden: {name}")
            result[name] = path
    return result


def inspect_seed(seed):
    seed = Path(seed).resolve()
    actual = files_under(seed)
    manifest = read_json(seed / "FILES-SHA256.json")
    if not isinstance(manifest, dict) or not manifest or "FILES-SHA256.json" in manifest:
        raise ValueError("A complete checksum manifest is required")
    for name, expected in manifest.items():
        safe_path(seed, name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"Invalid manifest digest: {name}")
    if MODEL_PATH in actual:
        raise ValueError("The Git seed must omit the model payload; CI obtains the pinned public file")
    if set(actual) != (set(manifest) - {MODEL_PATH}) | {"FILES-SHA256.json"}:
        raise ValueError("Seed contains missing or unlisted files")
    if manifest.get(MODEL_PATH) != MODEL_SHA256:
        raise ValueError("Manifest must include the pinned model digest despite omitting its payload")
    for name, path in actual.items():
        if path.stat().st_size >= 100_000_000:
            raise ValueError(f"Seed file exceeds the normal Git blob threshold: {name}")
        if name != "FILES-SHA256.json" and sha256(path) != manifest[name]:
            raise ValueError(f"Seed checksum mismatch: {name}")

    metadata = read_json(seed / "CONTENTS.json")
    app, model = metadata["app"], metadata["model"]
    expected_app = {
        "name": "AI Drive Offline", "version": VERSION, "version_code": VERSION_CODE,
        "application_id": APPLICATION_ID, "apk_filename": APK_NAME, "apk_sha256": APK_SHA256,
        "modified_source": f"sources/{SOURCE_NAME}", "modified_source_sha256": SOURCE_SHA256,
        "internet_permission": False, "network_state_permission": False,
        "android_backup_enabled": False, "remote_mode_available": False,
    }
    if any(app.get(key) != value for key, value in expected_app.items()):
        raise ValueError("Seed describes a different candidate or an online/backup-enabled app")
    expected_model = {
        "filename": MODEL_NAME, "revision": MODEL_REVISION, "size_bytes": MODEL_BYTES,
        "published_sha256": MODEL_SHA256, "download_url": MODEL_URL,
    }
    if any(model.get(key) != value for key, value in expected_model.items()):
        raise ValueError("Seed describes a different model or download URL")
    required = {
        f"apps/{APK_NAME}", f"sources/{SOURCE_NAME}",
        f"apps/AI-Drive-Offline_v{VERSION}.build.json", "APP-VALIDATION.json",
        "apps/ChatterUI-LICENSE.txt", "models/LLAMA-3.2-LICENSE.txt", "models/NOTICE.txt",
        "PICTURE-GUIDE.pdf", "START-HERE.txt", "START-HERE.html",
        GUIDE_PATH, INSTALL_GUIDE_PATH,
    }
    if not required.issubset(actual):
        raise ValueError("The reviewed app, exact corresponding source, validation, guide or licenses are missing")
    if manifest[f"apps/{APK_NAME}"] != APK_SHA256 or manifest[f"sources/{SOURCE_NAME}"] != SOURCE_SHA256:
        raise ValueError("Manifest is not for the reviewed APK and source archive")
    if manifest[GUIDE_PATH] != GUIDE_SHA256:
        raise ValueError("Manifest is not for the reviewed after-insertion picture guide")
    if manifest[INSTALL_GUIDE_PATH] != INSTALL_GUIDE_SHA256:
        raise ValueError("Manifest is not for the reviewed first-install picture guide")
    build = read_json(seed / f"apps/AI-Drive-Offline_v{VERSION}.build.json")
    expected_build = {
        "version": VERSION, "version_code": VERSION_CODE, "application_id": APPLICATION_ID,
        "apk_sha256": APK_SHA256, "native_files_byte_identical": True,
        "zip_crc_verified": True, "zip_alignment_verified": True,
        "internet_permission": False, "network_state_permission": False,
        "allow_backup": False, "ota_updates": False, "private_signing_key_included": False,
    }
    if any(build.get(key) != value for key, value in expected_build.items()):
        raise ValueError("Build provenance does not match the reviewed offline candidate")
    validation = read_json(seed / "APP-VALIDATION.json")
    if validation.get("apk_build", {}).get("apk_sha256") != APK_SHA256:
        raise ValueError("Validation must identify this exact candidate, not the previous version")
    if validation.get("document_status") != "final":
        raise ValueError("Final reviewed validation is required; a pending template is not a release seed")
    if PROFILE:
        check_seed(seed, PROFILE, metadata, validation)
    else:
        guard_native_restart(validation, MODEL_SHA256, MODEL_NAME, '3b', 2, apk_sha256=APK_SHA256)
    # ZIP_STORED avoids an optimistic compression estimate. Account for every path/header.
    sizes = {name: path.stat().st_size for name, path in actual.items()}
    sizes[MODEL_PATH] = MODEL_BYTES
    estimate = 22 + sum(size + 76 + 2 * len(f"Android-Drive-Kit/{name}".encode("utf-8")) for name, size in sizes.items())
    if estimate >= LIMIT:
        raise ValueError("Complete stored ZIP would reach GitHub's 2 GiB per-asset limit")
    return {"version": VERSION, "seed_files": len(actual), "complete_files": len(sizes), "stored_zip_bytes": estimate, "manifest": manifest}


def verify_zip(path, manifest):
    if Path(path).stat().st_size >= LIMIT:
        raise ValueError("Complete ZIP must be strictly smaller than 2 GiB")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        expected_names = {f"Android-Drive-Kit/{name}" for name in manifest} | {"Android-Drive-Kit/FILES-SHA256.json"}
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise ValueError("ZIP contains duplicate, missing or unlisted members")
        for member in archive.infolist():
            relative_name(member.filename)
            if member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("ZIP must contain regular files only")
            name = member.filename.removeprefix("Android-Drive-Kit/")
            with archive.open(member) as stream:
                result = hashlib.sha256()
                for block in iter(lambda: stream.read(CHUNK), b""):
                    result.update(block)
            if name != "FILES-SHA256.json" and result.hexdigest() != manifest[name]:
                raise ValueError(f"Packaged checksum mismatch: {name}")
        if json.loads(archive.read("Android-Drive-Kit/FILES-SHA256.json")) != manifest:
            raise ValueError("Packaged checksum manifest changed")


def obtain_model(destination, existing=None):
    if existing:
        source = Path(existing)
        if source.is_symlink() or source.stat().st_size != MODEL_BYTES or sha256(source) != MODEL_SHA256:
            raise ValueError("Local model does not match the pinned size and digest")
        shutil.copyfile(source, destination)
        return
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "AI-Drive-Offline-CI/1"})
    digest, total = hashlib.sha256(), 0
    # urllib preserves normal HTTPS certificate checks and the configured proxy.
    with urllib.request.urlopen(request, timeout=90) as response, Path(destination).open("wb") as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Model download must remain on HTTPS")
        while block := response.read(CHUNK):
            total += len(block)
            if total > MODEL_BYTES:
                raise ValueError("Model response exceeds the pinned size")
            digest.update(block)
            output.write(block)
    if total != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
        raise ValueError("Downloaded model size or publisher digest mismatch")


def assemble(seed, output, model_file=None):
    seed, output = Path(seed).resolve(), Path(output).resolve()
    report = inspect_seed(seed)
    if output.exists():
        raise ValueError("Use a new output directory; existing artifacts are never overwritten")
    output.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix="ai-drive-ci-") as temporary:
            root = Path(temporary) / "Android-Drive-Kit"
            shutil.copytree(seed, root)
            obtain_model(root / MODEL_PATH, model_file)
            target = output / KIT_NAME
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for name, path in sorted(files_under(root).items()):
                    # Fixed headers make the complete ZIP byte-identical on a retry.
                    info = zipfile.ZipInfo(f"Android-Drive-Kit/{name}", date_time=(2026, 10, 15, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.file_size = path.stat().st_size
                    with path.open("rb") as source, archive.open(info, "w") as member:
                        shutil.copyfileobj(source, member, length=CHUNK)
            verify_zip(target, report["manifest"])
        copies = {
            APK_NAME: seed / f"apps/{APK_NAME}", SOURCE_NAME: seed / f"sources/{SOURCE_NAME}",
            "AI-Drive-Offline-AGPL-3.0.txt": seed / "apps/ChatterUI-LICENSE.txt",
            "Llama-3.2-Community-License.txt": seed / "models/LLAMA-3.2-LICENSE.txt",
            "Llama-3.2-NOTICE.txt": seed / "models/NOTICE.txt",
            "Android-AI-Drive-Picture-Guide.pdf": seed / "PICTURE-GUIDE.pdf",
            GUIDE_NAME: seed / GUIDE_PATH,
        }
        for name, source in copies.items():
            shutil.copy2(source, output / name)
        assets = [target, *(output / name for name in copies)]
        checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in assets)
        (output / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
        report.pop("manifest")
        report.update({"archive": target.name, "size_bytes": target.stat().st_size, "sha256": sha256(target), "public_release_created": False})
        (output / "PACKAGE-REPORT.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / "RELEASE-NOTES.md").write_text(
            f"# AI Drive Offline {VERSION} — Android USB kit preview\n\n"
            f"**For the complete drive, download `{KIT_NAME}`. The app, full Llama model, source, guide and licenses are all inside.**\n\n"
            "On Android, download the complete ZIP, extract it in Files, and copy the Android-Drive-Kit folder to your USB drive. "
            f"After connecting the drive to the Android phone, follow START-HERE.html, PICTURE-GUIDE.pdf or {GUIDE_NAME}. "
            "Android requires you to tap the APK to install and open the app; connecting a drive does not automatically launch it.\n\n"
            "Separate APK/source/license/guide files below are optional individual downloads. "
            "SHA256SUMS.txt lists the exact files. This is an independently signed modified ChatterUI app, "
            "with local USB memory and no internet permission.\n\n"
            "Consult APP-VALIDATION.json for this exact usb.4 installer's actual runtime evidence, "
            "including snapshot persistence checks and model-inference results or outstanding limits. "
            "Physical phone and physical USB operation remain untested; "
            "emulator results do not establish retail readiness.\n\n"
            f"APK SHA-256: `{APK_SHA256}`\n\nSource SHA-256: `{SOURCE_SHA256}`\n\n"
            "Built with Llama. Llama 3.2 is licensed under the Llama 3.2 Community License, "
            "Copyright © Meta Platforms, Inc. All Rights Reserved.\n",
            encoding="utf-8",
        )
        return report
    except BaseException:
        shutil.rmtree(output)
        raise


def diagnostic_text(value, limit=2400):
    value = str(value)[:16000]
    # Diagnostic output must never turn transport/authentication material into
    # a public Check annotation. Preserve the HTTP error and command stage.
    value = re.sub(r"(?im)^[^\n]*\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token)\s*:.*$",
                   "[redacted authentication header]", value)

    def clean_url(match):
        try:
            parsed = urllib.parse.urlsplit(match[0])
            # Keep the endpoint, remove URL userinfo, every query value and fragment.
            host = parsed.netloc.rsplit("@", 1)[-1]
            return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path,
                                            "[redacted-query]" if parsed.query else "", ""))
        except ValueError:
            return "[redacted URL]"

    value = re.sub(r'''https?://[^\s<>"']+''', clean_url, value)
    # URLs must be cleaned before substitutions introduce spaces into a token
    # value; otherwise a later URL match could miss remaining query parameters.
    value = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)",
                   "[redacted token]", value)
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[redacted authentication]", value)
    value = re.sub(
        r'''(?i)["']?(?:GH_TOKEN|GITHUB_TOKEN|access_token|refresh_token|client_secret|password|api_key)["']?\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''',
        "[redacted credential assignment]", value)
    return value[:limit] + (" [diagnostic truncated]" if len(value) > limit else "")


def gh(*args):
    result = subprocess.run(["gh", *args], check=False, text=True, capture_output=True)
    if result.returncode:
        stage = f"gh release {args[1]}" if len(args) > 1 and args[0] == "release" and args[1] in {"create", "upload", "download"} else "gh api metadata"
        details = [f"{stage} failed (exit {result.returncode})"]
        if result.stderr.strip():
            details.append("stderr: " + diagnostic_text(result.stderr, 1600))
        if result.stdout.strip():
            details.append("stdout: " + diagnostic_text(result.stdout, 600))
        raise RuntimeError(diagnostic_text("\n".join(details)))
    return result.stdout


def release_assets(output):
    output = Path(output)
    expected_names = [KIT_NAME, APK_NAME, SOURCE_NAME, "AI-Drive-Offline-AGPL-3.0.txt", "Llama-3.2-Community-License.txt", "Llama-3.2-NOTICE.txt", "Android-AI-Drive-Picture-Guide.pdf", GUIDE_NAME]
    lines = (output / "SHA256SUMS.txt").read_text().splitlines()
    expected = {}
    for line in lines:
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9._+-]+)", line)
        if not match or match[2] in expected:
            raise ValueError("Invalid or duplicate release checksum line")
        expected[match[2]] = match[1]
    if set(expected) != set(expected_names):
        raise ValueError("Release checksum file lists an unexpected asset set")
    if expected.get(APK_NAME) != APK_SHA256 or expected.get(SOURCE_NAME) != SOURCE_SHA256:
        raise ValueError("Release files do not identify the reviewed APK and exact corresponding source")
    if expected.get(GUIDE_NAME) != GUIDE_SHA256:
        raise ValueError("Release picture does not match the reviewed after-insertion guide")
    files = [output / name for name in expected_names] + [output / "SHA256SUMS.txt"]
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size >= LIMIT:
            raise ValueError(f"Unsafe or oversized release asset: {path.name}")
        if path.name in expected and sha256(path) != expected[path.name]:
            raise ValueError(f"Release checksum mismatch: {path.name}")
    return files


def assert_tag_target(target):
    # --target does not move an already-existing tag. Verify lightweight and annotated tags.
    refs = json.loads(gh("api", f"repos/{REPOSITORY}/git/matching-refs/tags/{TAG}"))
    matches = [ref for ref in refs if ref.get("ref") == f"refs/tags/{TAG}"]
    if not matches:
        return
    if len(matches) != 1:
        raise ValueError("Candidate tag lookup was ambiguous")
    obj = matches[0]["object"]
    for _ in range(8):
        if not re.fullmatch(r"[a-f0-9]{40}", obj.get("sha", "")):
            raise ValueError("Candidate tag has an invalid object ID")
        if obj.get("type") == "commit":
            if obj["sha"] != target:
                raise ValueError("Existing candidate tag points to a different commit; it was not moved")
            return
        if obj.get("type") != "tag":
            raise ValueError("Candidate tag does not point to a commit")
        obj = json.loads(gh("api", f"repos/{REPOSITORY}/git/tags/{obj['sha']}"))["object"]
    raise ValueError("Candidate tag has too many annotation layers")


def require_reviewed_draft(release, target):
    if (not release or not release.get("draft") or not release.get("prerelease")
            or release.get("target_commitish") != target):
        observed = {"release_none": release is None, "expected_target": target}
        if release is not None:
            observed.update({key: release.get(key) for key in ["id", "draft", "prerelease", "target_commitish"]})
        raise ValueError("Existing release is not this exact commit's draft prerelease; it was not changed. "
                         + json.dumps(observed, separators=(",", ":")))
    return release


def create_or_resume_draft(output, target):
    if (os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("GITHUB_REPOSITORY") != REPOSITORY
            or os.getenv("GITHUB_REF") != f"refs/heads/{BRANCH}"
            or os.getenv("GITHUB_SHA") != target or not re.fullmatch(r"[a-f0-9]{40}", target)
            or not os.getenv("GH_TOKEN")):
        raise ValueError("Draft uploads require the reviewed branch's normal GitHub Actions runner and scoped GH_TOKEN")
    files = release_assets(output)
    notes = Path(output) / "RELEASE-NOTES.md"
    if not notes.is_file() or not notes.read_text().strip():
        raise ValueError("Concrete release notes are required")
    assert_tag_target(target)

    def existing_release():
        # Explicit pages work with the workspace's gh 2.46 as well as hosted runners.
        matches = []
        for page_number in range(1, 1001):
            page = json.loads(gh("api", f"repos/{REPOSITORY}/releases?per_page=100&page={page_number}"))
            if not isinstance(page, list):
                raise ValueError("GitHub returned an unexpected release list")
            matches.extend(release for release in page if release["tag_name"] == TAG)
            if len(page) < 100:
                break
        else:
            raise ValueError("Release pagination exceeded the review limit")
        if len(matches) > 1:
            raise ValueError("Multiple releases use the candidate tag")
        return matches[0] if matches else None

    release = existing_release()
    if release is None:
        gh("release", "create", TAG, "--repo", REPOSITORY, "--target", target,
           "--draft", "--prerelease", "--title", f"AI Drive Offline {VERSION}: complete Android USB kit",
           "--notes-file", str(notes))
        release = existing_release()
        # GitHub may briefly omit a newly created draft from its release list.
        # Retry absence only; a visible wrong target/flag is never retried or changed.
        for _ in range(5):
            if release is not None:
                break
            time.sleep(2)
            release = existing_release()
    require_reviewed_draft(release, target)
    asset_names = {asset["name"] for asset in release["assets"]}
    if len(asset_names) != len(release["assets"]) or not asset_names.issubset({path.name for path in files}):
        raise ValueError("Draft contains unexpected assets; manual review is required")
    downloaded_and_verified = set()

    def download_and_verify(path):
        with tempfile.TemporaryDirectory(prefix="ai-drive-existing-") as temporary:
            gh("release", "download", TAG, "--repo", REPOSITORY, "--pattern", path.name, "--dir", temporary)
            if sha256(Path(temporary) / path.name) != sha256(path):
                raise ValueError(f"Existing draft asset differs: {path.name}")
        downloaded_and_verified.add(path.name)

    for path in files:
        if path.name in asset_names:
            # A retry resumes the same draft. Never overwrite a same-name asset silently.
            download_and_verify(path)
        else:
            gh("release", "upload", TAG, str(path), "--repo", REPOSITORY)
    final = existing_release()
    require_reviewed_draft(final, target)
    expected = {path.name: path.stat().st_size for path in files}
    received = {asset["name"]: asset["size"] for asset in final["assets"]}
    if (received != expected or not final["draft"] or not final["prerelease"]
            or final["target_commitish"] != target or len(received) != len(final["assets"])):
        raise ValueError("Draft asset read-back did not match the verified local asset set")
    remote_assets = {asset["name"]: asset for asset in final["assets"]}
    for path in files:
        digest = remote_assets[path.name].get("digest")
        if digest:
            if digest != "sha256:" + sha256(path):
                raise ValueError(f"Uploaded draft asset digest differs: {path.name}")
        elif path.name not in downloaded_and_verified:
            # Older GitHub API responses omit digest. Use the ordinary runner's
            # authenticated release download and hash the actual returned bytes.
            download_and_verify(path)
    assert_tag_target(target)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"Complete kit uploaded to **draft** release [{TAG}]({final['html_url']}). "
                         f"The primary download is **{KIT_NAME}**. Nothing was published publicly.\n")
    report = {"draft_url": final["html_url"], "tag": TAG, "assets": received,
              "asset_sha256_verified": True, "complete_zip_sha256": sha256(Path(output) / KIT_NAME),
              "public_release_created": False}
    workflow_notice("Android draft upload verified", report)
    print(json.dumps(report, indent=2))


def workflow_notice(title, report):
    if os.getenv("GITHUB_ACTIONS") == "true":
        message = json.dumps(report, separators=(",", ":")).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title={title}::{message}")


def report_failure(error):
    message = diagnostic_text(f"{type(error).__name__}: {error}")
    if os.getenv("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Android delivery failed::{escaped}")
    # JSON quoting keeps any captured newline/command text on one inert line.
    print("Android delivery failed: " + json.dumps(message), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-profile", type=Path, help="Optional final reviewed 1B profile")
    parser.add_argument("command", choices=["inspect", "assemble", "draft"])
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model-file", type=Path, help="Optional already verified local model; normal CI downloads the pinned public URL")
    parser.add_argument("--target")
    parser.add_argument("--create-draft", action="store_true")
    args = parser.parse_args()
    global PROFILE
    if args.model_profile:
        PROFILE = load_profile(args.model_profile)
        configure_ci(globals(), PROFILE)
    if args.command in {"inspect", "assemble"} and not args.seed:
        parser.error("--seed is required")
    if args.command in {"assemble", "draft"} and not args.output:
        parser.error("--output is required")
    if args.command == "inspect":
        report = inspect_seed(args.seed)
        report.pop("manifest")
        print(json.dumps(report, indent=2))
    elif args.command == "assemble":
        report = assemble(args.seed, args.output, args.model_file)
        workflow_notice("Complete Android kit assembled", report)
        print(json.dumps(report, indent=2))
    else:
        release_assets(args.output)
        if not args.create_draft:
            print("Local release assets verified. No API request, push, run, tag or release write was performed.")
        elif not args.target:
            parser.error("--target is required with --create-draft")
        else:
            create_or_resume_draft(args.output, args.target)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        report_failure(error)
        raise SystemExit(1)
