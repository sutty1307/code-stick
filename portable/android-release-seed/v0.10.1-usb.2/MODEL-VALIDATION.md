# Model validation

The complete `Llama-3.2-3B-Instruct-abliterated.Q4_K_M.gguf` loaded and generated a response using offline CPU inference in the cloud environment.

- Runtime: official llama.cpp b11429, commit d81235049, the binary build linked by stable release v0.6.0.
- Ubuntu x64 release archive SHA-256 verified against GitHub's publisher digest: `f6d25dde8f51133143d1453da4fd5f73b145127177612a283bf7995957af3392`.
- Model size: 2,019,377,472 bytes.
- Exit status: 0.
- Elapsed wall time: 11.071 seconds, including loading and generation.
- Peak resident memory: 3,619,016 KiB, approximately 3.45 GiB.
- Context: 2,048 tokens; maximum output: 64 tokens; CPU threads: 4.
- Reported generation speed: 8.5 tokens/second; prompt processing: 56.3 tokens/second.

Prompt:

> In one short sentence, explain why a USB drive should remain connected while an AI model stored on it is running.

Model response, preserved verbatim:

> A USB drive should remain connected while an AI model stored on it is running to maintain continuous access to the model's data and ensure seamless updates or changes.

The response's reference to updates is generated wording, not a verified capability of the application.

This validates actual local model loading and short generation on Linux x86_64 cloud hardware. It does not validate ChatterUI, Pixel performance, GrapheneOS compatibility, removable-drive permissions, or inference directly from USB hardware.

## Reproduce

```bash
/workspace/android-drive-kit/validation-tools/b11429/llama-b11429/llama-cli \
  --model /workspace/android-drive-kit/Android-Drive-Kit/models/Llama-3.2-3B-Instruct-abliterated.Q4_K_M.gguf \
  --offline --device none --gpu-layers 0 --ctx-size 2048 \
  --threads 4 --threads-batch 4 --batch-size 128 --ubatch-size 128 \
  --predict 64 --temp 0 --seed 42 --single-turn --no-display-prompt \
  --color off --log-colors off \
  --prompt "In one short sentence, explain why a USB drive should remain connected while an AI model stored on it is running."
```

Detailed results: `inference-result.json`, `inference.stdout.txt`, `inference.stderr.txt`, and `runtime-source.json` in this directory. `run-validation.py` bounds the same operation with a 180-second timeout.

Publisher sources:

- https://github.com/ggml-org/llama.cpp/releases/tag/v0.6.0
- https://github.com/ggml-org/llama.cpp/releases/tag/b11429
- https://github.com/ggml-org/llama.cpp/releases/download/b11429/llama-b11429-bin-ubuntu-x64.tar.gz
