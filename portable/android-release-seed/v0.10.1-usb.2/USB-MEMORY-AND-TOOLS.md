# AI Drive Offline — storage and tools

This is a modified ChatterUI application, installed separately as `com.codestick.aidrive`. Its corresponding source and build instructions are included in `sources/`. It needs an Android phone; the installer does not run on iPhone.

Select the `Android-Drive-Kit` folder on removable storage in **Drive Memory**. Android grants access to that folder. The grant is specific to the phone: another phone must select the same folder again. Internal phone folders and cloud document providers are rejected.

## What survives a conversation or app restart

The app automatically saves complete chat records, alternate replies, profiles, preferences, facts, notes, author notes, lorebooks, and personal picture/attachment assets in the selected USB folder. The SQLite database and personal settings operate in RAM; assets use temporary phone cache while the app is running. Only the selected-folder grant is saved in the phone's settings. Model weights can be selected directly from the drive; manually importing weights into phone storage is a separate option.

Explicit self-statements such as a name, preference, or “remember” request can become editable memory entries. This is a deterministic capture rule, not a guarantee that every implied fact will be extracted. Complete conversation text remains saved even when it does not create a memory entry. The model receives a bounded selection of saved memory plus recent messages, subject to the model's active context limit. Saving the complete history does not mean every historic message fits into one model request.

Each save alternates between `drive-memory-a.json` and `drive-memory-b.json`. A checksum and an exact read-back must match before the app reports a verified save. The prior revision can recover a corrupt latest revision. Android does not expose a filesystem flush guarantee, so read-back verification is not a guarantee against sudden power loss. Use **Save and disconnect** and wait for completion before removing the drive.

The files are **plaintext**, not encrypted. Anyone with the drive can read them. Checksums detect corruption; they do not authenticate files or prevent tampering. Keep the drive private. A failed drive read or write disables the session and clears cached private state; there is no automatic phone or cloud fallback.

The snapshot limit is 33,554,432 JavaScript string characters (about 32 million), not a byte count. Memory entries have a 2,000-character limit and a 1,000-entry limit. A separate safety limit allows 100,000 captured-message markers. Exceeding a limit fails visibly; the app does not silently truncate conversation history. Pictures and attachments consume snapshot space too.

Forgetting a memory entry replaces both recovery snapshots and retains a processed-message marker so rescanning an old chat does not recreate it. The original statement can still exist in its separate conversation. Deleting that conversation removes its chat records and archive copies; this is not secure erasure of flash-media sectors or separate copies made outside the app.

## Network behavior

The modified APK has no `android.permission.INTERNET` or `ACCESS_NETWORK_STATE` permission. Remote mode stays disabled even if restored preferences request it. App updates and Android speech-provider calls are disabled. The app's own chat engine cannot open an internet connection when the phone is online. User-invoked exports, the operating system, keyboard, and other apps have their own behavior and permissions.

## Offline toolkit

Use the dedicated **Offline Toolkit** screen, or type an explicit command in chat:

```text
/calc (12 + 8) * 3
/remember I prefer brief answers
/memories
/forget ID
/note Shopping | Milk, bread, apples
/notes
/read-note ID
/edit-note ID | Updated note text
/delete-note ID
/help
```

Calculation uses a bounded arithmetic parser, without `eval`, shell execution, or network requests. Memory and note commands save through the same verified USB service. Commands found in model replies or saved text do not execute tools.

## Validation limits

Pure storage, arithmetic, command, settings, and prompt-budget tests run in the cloud. TypeScript, APK signature, manifest, and archive checks are performed separately. Model inference has been tested with the bundled GGUF on a cloud CPU. Installation, local inference, USB reconnect/detach, storage grants, and long-term reliability still need validation on physical Android devices before selling these drives.
