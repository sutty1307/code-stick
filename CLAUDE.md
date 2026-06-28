# CLAUDE.md — code-stick

TypeScript/Node.js CLI that installs a portable AI coding agent (opencode + Ollama) onto a USB drive, then starts it on any supported machine without leaving residue on the host.

## Quick commands

```bash
npm install                  # install deps (Node 20+ required)
npm run build                # tsup → dist/cli.js
npm run typecheck            # tsc --noEmit
npm test                     # vitest run (all tests)
npm run test:watch           # vitest watch mode
npm run smoke:docker         # full end-to-end install + launch in Linux Docker
npm run hashes               # recompute SHA256 catalog entries
npm run hashes:check         # verify catalog hashes match live downloads
npm run hashes:bump          # write updated hashes to catalog
```

## Repository layout

```
code-stick/
├── src/
│   ├── cli.ts                  # Entry point: Commander program, all subcommands wired
│   ├── catalog/
│   │   ├── models.ts           # Curated model list (id, ollama tag, num_ctx, size)
│   │   ├── ollama.ts           # Ollama download URLs + SHA256 per target
│   │   ├── opencode.ts         # opencode download URLs + SHA256 per target
│   │   └── targets.ts          # Target enum: windows-x64, windows-arm64, darwin-arm64, etc.
│   ├── commands/               # One file per CLI subcommand
│   │   ├── install.ts          # install — full setup flow
│   │   ├── start.ts            # start — launch Ollama + opencode from USB
│   │   ├── status.ts           # status — show installed state
│   │   ├── doctor.ts           # doctor — live audit (port, process, model store)
│   │   ├── add-model.ts        # add-model — pull additional model to stick
│   │   ├── remove-model.ts     # remove-model — delete model from stick
│   │   ├── add-targets.ts      # add-targets — stage additional OS targets
│   │   ├── upgrade-engine.ts   # upgrade-engine — swap Ollama + opencode binaries
│   │   ├── prune.ts            # prune — reclaim orphaned Ollama blobs
│   │   ├── update.ts           # update — refresh launcher scripts
│   │   └── uninstall.ts        # uninstall — remove all code-stick files
│   ├── core/
│   │   ├── downloader.ts       # Streaming download + SHA256 verification
│   │   ├── extract.ts          # zip / tar / tar.zst extraction
│   │   ├── archive-staging.ts  # Download → extract → place engine binary
│   │   ├── engine-staging.ts   # Coordinate per-target engine staging
│   │   ├── launcher-gen.ts     # EJS template → start-*.bat/.command/.sh
│   │   ├── model-pull.ts       # Spin up temp Ollama, pull model, tear down
│   │   ├── opencode-config.ts  # Write opencode.json to USB config dir
│   │   ├── opencode-prestage.ts # Download + stage opencode binary
│   │   ├── preflight.ts        # USB format check (FAT32 rejects >4 GB files)
│   │   ├── process-manager.ts  # Spawn / track / kill child processes (tree-kill)
│   │   ├── health.ts           # Poll Ollama /api/version until ready
│   │   ├── cleanup.ts          # Remove temp dirs after install
│   │   ├── copy.ts             # Atomic file copy helpers
│   │   ├── macos.ts            # macOS-specific: xattr clear, quarantine
│   │   └── usb.ts              # USB drive discovery via drivelist
│   ├── state/
│   │   └── manifest.ts         # Read/write code-stick.json (v2 schema, v1 migration)
│   └── utils/
│       ├── bug-report.ts       # Structured bug report builder (redacts paths)
│       ├── env.ts              # CODE_STICK_* env var checks
│       ├── exit-guard.ts       # Double Ctrl+C → force exit
│       ├── install-log.ts      # Append-only install log
│       ├── logger.ts           # Chalk-based log.info / log.warn / log.error / log.dim
│       ├── paths.ts            # usbPaths(drivePath) → all well-known USB paths
│       ├── platform.ts         # currentTarget() → host target string
│       ├── prompt.ts           # inquirer wrappers: confirm, select, input
│       └── exit-guard.ts       # Ctrl+C double-tap guard
├── templates/
│   ├── start-windows.bat.ejs   # EJS template for Windows launcher
│   ├── start-mac.command.ejs   # EJS template for macOS launcher
│   └── start-linux.sh.ejs      # EJS template for Linux launcher
├── test/                       # Vitest tests (one file per core/state/utils module)
├── scripts/
│   ├── smoke.mjs               # End-to-end install smoke test (native)
│   ├── smoke.Dockerfile        # Docker-based Linux smoke test
│   ├── compute-hashes.mjs      # Compute SHA256 for all catalog entries
│   ├── check-catalog-hashes.mjs # Verify / update catalog hashes
│   └── verify-publish.mjs      # Pre-publish sanity check
├── docs/                       # Detailed reference docs
│   ├── ARCHITECTURE.md         # Stick layout + runtime behavior
│   ├── COMMANDS.md             # All subcommands + flags
│   ├── MODELS.md               # Model catalog, BYO tags, context windows
│   ├── SECURITY.md             # Trust model + threat boundaries
│   ├── STORAGE.md              # USB sizing guide
│   ├── TROUBLESHOOTING.md      # Gatekeeper, MAX_PATH, port conflicts, bug reports
│   └── TRUST.md                # Supply-chain provenance
├── tsconfig.json
├── tsup.config.ts              # esbuild via tsup, target node20, ESM output
├── vitest.config.ts            # test/**, isolate: true, pool: forks
└── package.json
```

## USB stick layout (after install)

```
<USB>/
├── code-stick.json          manifest (v2 schema)
├── start-windows.bat        launcher
├── start-mac.command        launcher
├── start-linux.sh           launcher
├── engine/<target>/         ollama binary (one dir per staged OS target)
├── opencode/<target>/       opencode binary (one dir per staged OS target)
├── data/                    OLLAMA_MODELS — blobs shared across all OS targets
└── config/opencode/         opencode.json (redirected via XDG_CONFIG_HOME / APPDATA)
```

Supported target strings: `windows-x64`, `windows-arm64`, `darwin-arm64`, `darwin-x64`, `linux-x64`, `linux-arm64`.

## Manifest schema (v2)

`code-stick.json` on the USB root. The `loadManifest` function in `src/state/manifest.ts` does v1→v2 migration on read. Always write v2 via `saveManifest`.

```typescript
interface Manifest {
  version: "2";
  installedAt: string;          // ISO timestamp
  updatedAt?: string;
  models: ManifestModel[];      // all installed models
  defaultModelId: string;       // used by launchers + opencode config
  targets: Target[];            // which OS targets are staged
  ollamaVersions: { host: string; linux: string };  // split because Linux uses a different archive
  opencodeVersion: string;
}
```

`saveManifest` uses a file lock (`code-stick.json.lock`) with stale-lock detection (PID check + `/proc/<pid>/comm` on Linux, mtime age). Never write the manifest file directly — always go through `saveManifest`.

## Key design invariants

- **Loopback only:** Ollama always binds `127.0.0.1:11434`, never `0.0.0.0`. Launchers enforce this via `OLLAMA_HOST`.
- **No host residue:** Launchers redirect `OLLAMA_MODELS`, `XDG_CONFIG_HOME`, and `APPDATA` to USB paths. Nothing is written to `~/.ollama` or the host's opencode config.
- **Kill by PID:** Launchers kill only the Ollama process they spawned, by PID. Never `taskkill /IM ollama.exe` — that would kill a host-installed Ollama.
- **SHA256-pinned downloads:** All catalog entries in `src/catalog/ollama.ts` and `src/catalog/opencode.ts` have SHA256 hashes. `downloader.ts` verifies before extraction. Un-pinned downloads require `CODE_STICK_ALLOW_UNVERIFIED=1`.
- **FAT32 guard:** `preflight.ts` detects FAT32 USB format and bails — FAT32's 4 GB file limit blocks models >4 GB.
- **Temp Ollama for install:** `install`, `add-model`, and `upgrade-engine` each spin up a temporary `ollama serve` process, do their work, then tear it down via `process-manager.ts`. This never modifies a running host Ollama.

## TypeScript conventions

- **ESM only** (`"type": "module"` in package.json). All imports use `.js` extensions even for `.ts` source files.
- Strict mode. No `any`. Prefer discriminated unions over boolean flags.
- Runtime externals (drivelist, got, inquirer, tar, etc.) are left external in `tsup.config.ts` — they interop better when Node resolves them. Don't bundle them.
- `PKG_VERSION` is injected at build time via tsup `define`. Read from the `PKG_VERSION` const, not `package.json` at runtime.
- File author line format: `// Author: <Name> (<handle>) | Sig: <sig>` — preserve when editing existing files, don't add to new ones.

## Testing

- Tests live in `test/` (not colocated). One file per module.
- `vitest.config.ts`: `isolate: true`, `pool: "forks"` — tests stub `fs` and `spawn` heavily; isolation prevents module-level state (manifest warning, install-log handle) from bleeding.
- Run `npm test` for all tests. `npm run test:watch` for watch mode.
- Do not run multiple vitest commands concurrently in the same directory (cache races).
- Snapshot files live in `test/__snapshots__/`. Run `vitest -u` to update.

## Environment variables

| Variable | Effect |
|----------|--------|
| `CODE_STICK_DEBUG=1` | Print full stack traces on errors |
| `CODE_STICK_ALLOW_UNVERIFIED=1` | Allow non-SHA-pinned `--opencode-version` overrides |
| `CODE_STICK_NO_REPORT=1` | Suppress bug-report generation on crash (set by commands on clean exits) |

## Adding a new curated model

1. Add an entry to `src/catalog/models.ts` with `id`, `ollamaTag`, `numCtx`, display name, and size.
2. Update `docs/MODELS.md` with the new row.
3. Test with `code-stick add-model <id>` on a real USB.

## Adding a new OS target

1. Add the target string to `src/catalog/targets.ts`.
2. Add download URL + SHA256 to `src/catalog/ollama.ts` and `src/catalog/opencode.ts`.
3. Add an EJS branch to the appropriate `templates/start-*.ejs` if the launcher logic differs.
4. Update `docs/ARCHITECTURE.md` and `docs/STORAGE.md`.
5. Run `npm run hashes:bump` to update the hash catalog.

## What to avoid

- Do not add `ollama serve` calls outside `core/process-manager.ts` — orphaned processes are a real user pain point.
- Do not write to `~/.ollama` or any host path — all reads/writes must be under the USB root.
- Do not add npm dependencies without checking whether Node stdlib or an existing dep covers it.
- Do not amend published commits or force-push `main`.
- Do not skip SHA256 verification on production downloads without `CODE_STICK_ALLOW_UNVERIFIED=1` opt-in.
