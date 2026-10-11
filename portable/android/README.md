# Android downloads: AI Drive Offline

This preview is for an ARM64 Android phone (Android 7 or newer). It includes
local Llama inference, automatic USB/SD memory, and offline calculator and note
tools. It has no Internet permission. Android requires you to install and open
the app; plugging in a USB drive does not automatically launch it.

## Downloads

- [Android installer — 67.7 MB](https://github.com/sutty1307/code-stick/raw/refs/heads/portable/android-usb-download/portable/android/AI-Drive-Offline_v0.10.1-usb.1.apk)
- [Llama 3.2 3B Instruct Abliterated model — 2.02 GB](https://huggingface.co/QuantFactory/Llama-3.2-3B-Instruct-abliterated-GGUF/resolve/120d74373f679f9d9e0463b435dfd3fb802d9eb0/Llama-3.2-3B-Instruct-abliterated.Q4_K_M.gguf?download=true)
- [Four-page picture guide](Android-AI-Drive-Picture-Guide.pdf)
- [Corresponding modified application source](AI-Drive-Offline-v0.10.1-usb.1-source.zip)
- [Application license](AI-Drive-Offline-AGPL-3.0.txt)
- [Llama license](Llama-3.2-Community-License.txt) and [required attribution](Llama-3.2-NOTICE.txt)
- [File checksums](DOWNLOADS-SHA256.json)
- [Detailed USB memory and tools documentation](USB-MEMORY-AND-TOOLS.md)
- [Installer build provenance](AI-Drive-Offline_v0.10.1-usb.1.build.json)

The installer does not contain the 2 GB model. Download both files and place the
GGUF model on your USB/SD drive. The illustrated guide begins after you connect
the drive: install/open AI Drive Offline, choose a removable memory folder,
select the GGUF model, and start a local conversation. Start with a 2,048-token
context. If the drive is unavailable or a save fails, follow the visible error
and reconnect it before disconnecting the session.

## What is remembered

Full chats, alternate replies, preferences, explicit facts, notes, profiles,
and personal assets automatically save to the selected removable folder.
Settings and the conversation database operate in RAM; personal attachments use
disposable cache mirrored into USB snapshots. You can reconnect the same drive
on another compatible phone and select its memory folder again. Model file
permissions are specific to each phone and may need to be selected again.

Memory files are unencrypted. Visible storage limits apply, and there is no
automatic phone or cloud fallback. The complete stored history is distinct
from the limited context that can fit into a model prompt.

## Validation and source

149 software tests and the TypeScript check passed. The installer signature,
alignment, and absence of Internet permission were checked. The full model was
verified against its publisher digest and ran on a cloud CPU. Physical Android
installation, USB detach/reconnect, inference performance, and retail readiness
remain untested.

The APK contains newly compiled application source with the verified matching
publisher native runtime. Full native recompilation was blocked by Maven
transport errors. Both build paths, the prominent modification notice dated
2026-10-11, and corresponding application source are in the source archive.
Private signing keys are excluded. See the build provenance for exact inputs.

Installer SHA-256:
`937b9d098130525d72dbbbf428868c4a66794927a801fe55d6a8899278cb28e2`.

Model SHA-256:
`e1f2b428e316d8bd1cb5ca13e9649329e299d951fbcee6b3cd0e9f1d646c9fa7`.

Built with Llama.

Llama 3.2 is licensed under the Llama 3.2 Community License,
Copyright © Meta Platforms, Inc. All Rights Reserved.

These files are on a dedicated distribution branch of `sutty1307/code-stick`;
the original `main` application files have not been changed. The complete 2.1 GB
USB kit exists in the development environment but is not a hosted release
asset. The two direct downloads above provide the Android app and full model.
