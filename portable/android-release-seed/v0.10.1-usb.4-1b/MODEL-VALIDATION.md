# Selected 1B model: evidence and scope

Built with Llama.

Selected file: `models/Llama-3.2-1B-Instruct-abliterated.Q4_K_M.gguf`

- Complete size: 955,445,792 bytes.
- SHA-256: `68336c25367576f49b29f01ee9034658b51bbded6a7f215e934d997de96301e0`.
- Publisher: mradermacher; pinned revision `becf36858832feddfea0ace2fabf791797571999`.
- Quantization: Q4_K_M. Declared modified base: huihui-ai/Llama-3.2-1B-Instruct-abliterated.
- Guide settings: Max Context 1024, Threads 2, Batch 16.

The downloaded full file's size and SHA-256 match the pinned publisher metadata.
The saved GGUF header identifies the llama architecture and 1B size label. The
quantizer's pinned README is retained unchanged in `models/MODEL-CARD.md`.
Base lineage is declared there; the base repository's contents/revision were
not independently verified because ordinary public API access returned 401.

An offline Linux x86_64 CPU smoke check with llama.cpp b11429 loaded this exact
file and returned `Hello.` for `Say hello in three words.` The process exited
successfully; its response did not follow the three-word instruction. This is
Linux execution evidence only. It does not establish Android app behavior,
physical phone speed, USB compatibility or general model quality.

The same exact APK and selected 1B model passed native generation and restoration
of that same generated chat in Android API 36 emulator run **38880538406**, job
**116905926139**, tested commit `868c30e2fc84dfc0b6b2a3e826d4724d093cc8d4`.
The real reply was `session inference 590ddeb14a`, with 7 native predicted tokens,
saved at snapshot revision 17. Native prompt processing took 165,209.689 ms and
generation took 9,926.457 ms in the translated emulator; this is not a physical
phone benchmark or an instruction-following quality assessment.

The model loaded in 8.49 seconds. Force-stop/relaunch changed PID `4154` to
`10038`; the same user/assistant records, raw text, swipes, timings and chat
archive survived, with both message bubbles visible. The restart proof took
11.15 seconds and preserved snapshot revision 17 through restored revision 18.
App data and SAF grants were unchanged. This establishes one generated-chat
restore, not migration of every conversation or durability of every media item.

The model file was in public phone Download. Memory used a genuine SAF grant
to a newly created disposable virtual removable disk. A physical phone, physical
USB/SD, direct model loading from USB and physical detach/reconnect are untested.
The earlier API 35/API 36 reply-only comparison remains historical in the app
validation record; its failed restart checks are not relabelled.

`APP-VALIDATION.json` records the actual same-job native output, timings,
SHA-valid snapshot and restart notices for this APK and selected model, together
with the separate install, memory and signed-upgrade checks. Physical Android phone and physical USB operation remain untested.

`models/MODEL-PROVENANCE.json` records pins, source references and the separation
between download integrity, Linux smoke evidence and Android validation.
