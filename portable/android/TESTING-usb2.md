# Android runtime test candidate: 0.10.1-usb.2

This branch is for testing the exact APKs. The existing download branch and main
remain unchanged. This candidate adds a **Choose a model** route to Drive Memory
and chats without a loaded model, and raises the Android versionCode for updating
an existing installation. It retains the original signing certificate.

The matching modified source archive, AGPL license and build/upgrade metadata are
beside the installer. Build and source checks pass. Android startup, update and
interactive checks remain unverified until this branch's runtime job succeeds.
The local cloud emulator could not initialize Android reliably without KVM; its
framework failure does not establish an APK failure.

The CI test does not establish physical USB compatibility, persistence on a real
drive, or Android Llama inference. Do not present this as a tested retail kit.

APK SHA-256: `aab31bcdf39f623cd21c30e02db0b1c16674e4ec27c77fb28f9cef7f5d422c1b`.
Source SHA-256: `3d1885585295e0be811789ff1bf46d6bd19bcf4b2c2fe8210bb35355c3228654`.
