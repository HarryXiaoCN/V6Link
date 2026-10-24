# V6Link

English | [简体中文](README.md)

Mirrors: [GitHub](https://github.com/HarryXiaoCN/V6Link) · [Gitee](https://gitee.com/harryxiaocn/V6Link)

[![CI](https://github.com/HarryXiaoCN/V6Link/actions/workflows/ci.yml/badge.svg)](https://github.com/HarryXiaoCN/V6Link/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)

**Peer-to-peer, end-to-end encrypted messaging over IPv6.** Messages and files travel directly between two devices — no central server, no account.

![V6Link main window](screenshots/main-window.png)

V6Link uses Python + asyncio for networking and PySide6 for the desktop UI. Each contact can hold multiple IPv6 endpoints. Connections try recently working addresses first, and a pinned Ed25519 identity fingerprint guarantees that a changed address is still the same person.

---

## Features

- **Contacts with multiple IPv6 endpoints**
  - Store several `IPv6:port` endpoints per contact.
  - Successfully used addresses are retried first.
  - After authentication, V6Link learns the TCP source address and the current IPv6 addresses advertised in the encrypted hello; contacts refresh immediately when the peer's address changes.
  - The peer's identity is pinned to an Ed25519 public-key fingerprint, so an address change cannot impersonate a contact.
- **Local IPv6 listener**
  - Listens on TCP `::` (default port `4455`) for incoming encrypted sessions.
  - The port can be changed in Settings without restarting the app.
- **Tray residency**
  - Minimizing hides the window to the system tray while V6Link keeps listening and receiving in the background.
  - Double-click the tray icon to restore the window; the tray menu can show the window or quit.
  - While hidden, new messages and friend requests raise system notifications; the tray tooltip shows the unread count.
- **Text and files**
  - Text messages have delivery acknowledgement, retry and friend-request states.
  - Files support multi-select, drag and drop, progress and speed; received files are saved automatically to a configurable download folder, and both sides can open the containing folder.
  - **Receiver consent**: the local encrypted container is created only after the receiver accepts.
  - **Cancel and pause**: cancel deletes partial chunks and forbids resume; network loss or app restart pauses the transfer, keeps chunks, and resumes from the acknowledged chunk bitmap.
  - Parallel encrypted transfer: 4 independent data channels by default (1–8), raw chunks with TCP backpressure and no per-chunk ACK.
- **All application data encrypted**
  - Transport: Ed25519 identity, a fresh X25519 key exchange per connection, HKDF-SHA256, AES-256-GCM.
  - Local: contacts, messages, addresses, the identity private key and file keys live in an AES-256-GCM vault; on Windows the master key is wrapped with DPAPI for the current user.
  - Incoming files are written to an encrypted container with a per-file random key; a plaintext copy is exported to the download folder on completion and the encrypted copy is kept in the vault.
- **Peer-to-peer app updates**
  - A newer instance can push its own running program to an older, already-approved contact.
  - The payload travels over the existing encrypted direct channel and is signed with the sender's Ed25519 identity key; the receiver verifies the signature and SHA-256 before staging.
  - The receiver confirms twice (accept the offer, then restart to install); a Windows single-file exe is backed up, replaced and restarted automatically.
- **Dark, polished UI**
  - Drop overlay, message bubbles, file cards, friend-request cards, security settings and encrypted contact cards.
  - Paste a contact card to fill in nickname, fingerprint and addresses automatically.

See [SECURITY.md](SECURITY.md) for the full threat model and cryptography notes (currently in Chinese).

---

## Requirements

- Windows 10/11 (primary target; Linux/macOS run but have no DPAPI — the master key file is protected with `0600` permissions).
- Python **3.11+**.
- Both peers need mutually reachable IPv6 addresses. Public direct connections usually require allowing inbound TCP on the chosen port.
- Both sides need IPv6; pure IPv4 networks cannot connect.

### Windows firewall

Run PowerShell as administrator and allow the default port (change it if you changed the listening port):

```powershell
New-NetFirewallRule -DisplayName "V6Link TCP 4455" -Direction Inbound -Protocol TCP -LocalPort 4455 -Action Allow
```

Check that the machine has IPv6:

```powershell
ipconfig
ping -6 ::1
```

---

## Install and run

### Option 1: double-click (Windows)

Double-click `run.bat` once: it creates `.venv` and installs dependencies. Double-click it again afterwards to start V6Link.

### Option 2: manual

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Use `python.exe` rather than `pythonw.exe` while debugging so tracebacks are visible.

### Option 3: single-file exe (for people without Python)

`dist\V6Link.exe` is a 64-bit Windows one-file build produced with PyInstaller (**v0.3.0**, about 51 MB). Send that single file; the receiver does not need Python or any dependency. Both sides must run the same protocol version.

- Double-click to run. The first start extracts the bundled runtime and may take a few seconds.
- The program does not write next to the exe; contacts and messages stay in `%LOCALAPPDATA%\V6Link`.
- The build is unsigned and self-compiled, so SmartScreen may warn. If you trust the source, choose **More info → Run anyway**.
- Allow the TCP port in Windows Firewall as described above (default `4455`).
- Do not share the data directory: it contains the local encrypted identity and chats.

To rebuild the exe, see [Building the Windows exe](#building-the-windows-exe).

On other platforms:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Optionally install as a package:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
v6link
```

---

## Local two-instance test

Run two instances with separate data directories on one Windows desktop to verify discovery, friend requests, encrypted messages and file transfer:

```powershell
# terminal 1
.\.venv\Scripts\python.exe main.py --data-dir .\demo-alice --port 4455

# terminal 2
.\.venv\Scripts\python.exe main.py --data-dir .\demo-bob --port 4456
```

In Alice, copy the card from **My encrypted card**; in Bob, click **+ → Paste card**. Then paste Bob's card into Alice. You can also add manually:

- Alice adds Bob's endpoint: `::1`, port `4456`
- Bob adds Alice's endpoint: `::1`, port `4455`

The first message creates a friend request; once accepted, both sides can send. Loopback tests do not need firewall rules.

---

## Usage

### 1. Exchange encrypted contact cards

Click **Me** at the bottom left or the card icon in the navigation bar to open **My encrypted card**:

- Copy the full card and send it to your contact.
- The card contains the nickname, identity fingerprint, globally routable IPv6 addresses and the listening port. `::1` loopback and `fe80::` link-local addresses are not included.
- The fingerprint is a public-key fingerprint; it contains no private key and is safe to share.

> The card lists addresses generated on this machine. What actually works across devices is decided by reachable endpoints.

### 2. Add a contact

Click **+** next to the **Messages** header:

- Enter a nickname, an optional identity fingerprint and IPv6 endpoints manually.
- Or copy a contact's encrypted card to the clipboard and click **Paste card**.
- Endpoints accept `240e:abcd::1`, `[240e:abcd::1]:4455` and `[fe80::1%Ethernet]:4455`.

The first connection uses **TOFU (Trust On First Use)**: if you did not provide a fingerprint, the first successful handshake pins the actual one. Later fingerprint changes block the connection and raise a security alert.

### 3. First message and friend request

Sending the first message to a new contact:

1. Sends an encrypted friend request.
2. The peer reviews the fingerprint in the request list and accepts.
3. The accepting side sends an encrypted "accepted" notification.
4. The requester automatically retries messages that were waiting.

If the peer does not accept promptly, the message stays in a waiting state and can be retried manually.

### 4. Sending files

- Click the paperclip next to the composer, or drag one or more files into the chat.
- The receiver confirms each file; only then is the encrypted container created and the transfer starts. Rejection creates no file.
- Either side can **Cancel** during a transfer. Cancel means "do not keep this file": the receiver deletes partial chunks and neither side can resume that transfer.
- Network glitches, an offline peer or app shutdown automatically **pause** the transfer and keep chunks. When the sender clicks **Resume**, the receiver verifies the same file fingerprint and chunk bitmap, and only missing chunks are sent.
- Pause state survives restarts. If the receiver already accepted the file, a matching resume continues automatically without another confirmation.
- Completed files are decrypted automatically into the configured download folder (the system Downloads folder by default); no manual "Save as" is needed.
- **Open** opens the local file in the download folder; **Show in folder** opens its location; **Save as** is only for writing an extra copy elsewhere.
- The sender's file card also has **Show in folder** to locate the original file.
- The sender keeps the original path. If the source file is moved, deleted or changed, the paused transfer cannot continue; send it again.

### 5. Settings

- **Nickname**: announced to contacts in the next encrypted handshake.
- **Listening port**: restarts the IPv6 listener immediately without restarting the app. A port conflict reports an error and restores the previous listener.
- **Parallel streams**: 1–8; higher values help on high-latency or large-file links, while a LAN can use fewer. Default is 4.
- **Download folder**: where completed files are saved; defaults to `~/Downloads`. Any writable folder works.

### 6. Minimize to tray

- Clicking minimize hides V6Link to the system tray while it keeps listening and receiving. Contacts can still reach you.
- Double-click the tray icon, or right-click → **Show main window**, to restore. The previous maximized state is kept.
- While hidden, new messages and friend requests raise system notifications; the tray tooltip shows the unread count.
- Right-click → **Quit V6Link** really exits. Closing the main window (×) still exits as before.
- If the desktop has no usable system tray (some minimal systems or remote sessions), minimize behaves as a normal window minimize.

### 7. Peer-to-peer updates

A newer instance can push its own program to an older authenticated contact, so the other side does not have to find an installer.

**Sender (newer)**

1. Right-click a contact and choose **Push version update…**.
2. V6Link reads the running program (the single-file exe, or a source archive when running from a checkout), computes SHA-256, signs the manifest with the identity key and sends the offer over the encrypted channel.
3. If the peer is not older, it reports that no update is needed.
4. If the peer accepts, it pulls the data on demand; the chat stays usable.

**Receiver (older)**

1. A dialog shows the version, size, SHA-256 and the sender's notes.
2. **Accept and update** downloads the payload with progress and speed; **Cancel** deletes the partial download.
3. After download, SHA-256 and the signature are verified again, and **Restart and update** becomes available.
4. After confirmation, V6Link generates a PowerShell script that waits for the current process to exit, backs up the old exe as `V6Link.exe.old-<version>`, writes the new exe and restarts it. Failures roll back automatically.

**Security boundaries**

- Only authenticated contacts can push. The signature is verified against the contact's pinned Ed25519 key; modified content is rejected.
- Receiving is just download and verification; installation requires a second explicit confirmation.
- There is no "official publisher" check: trust in an update equals trust in that contact. Do not accept forwarded files from unknown sources.

> When running from source (`python main.py`) there is no exe to replace: the receiver only saves the package under `<data dir>\updates\pending\` and shows the path. Automatic replacement and restart are currently Windows single-file exe only.

---

## Security design summary

### Transport

- Each installation generates a long-term Ed25519 identity key pair. A contact's identity fingerprint is `SHA-256(public key)`, pinned and shown to the user.
- Every TCP connection performs a fresh handshake:
  - the client sends a signed Client Hello;
  - the server returns a signed Server Hello binding both ephemeral public keys and nonces;
  - both sides derive a shared secret with X25519 and session keys with HKDF-SHA256;
  - the client then sends its first AES-256-GCM frame, proving possession of the private key.
- All application data (text, file chunks, control messages) uses AES-256-GCM frames with per-connection directional keys, monotonically increasing nonces and AAD bound to the full handshake transcript.

### Updates

- An update offer (`update_offer`) contains version, size, SHA-256, filename and notes, signed with the sender's long-term Ed25519 key over a canonical encoding of `offer_id / version / size / sha256 / kind / filename / notes`.
- The receiver verifies the signature against the pinned contact key before showing the offer, and verifies SHA-256 and the signature again after download.
- Only verified bytes are atomically moved from `updates/incoming/*.part` to `updates/pending/`; installation re-checks size, SHA-256 and the PE header (`MZ`).
- Updates never pass through a server and there is no official update source; trust comes from the sender's fingerprint.

### Local storage

- State lives in a single AES-256-GCM vault, `vault.v6`, written with atomic replacement.
- On Windows the random master key is wrapped with DPAPI (current-user scope) and stored in `vault.key`; no raw master key is left on disk.
- Default data directory: `%LOCALAPPDATA%\V6Link`.
- Received files use an independent random 256-bit key and are written to an encrypted container in fixed-size chunks; content is authenticated per chunk with AES-GCM.
- Filenames, sizes, file keys, contacts and messages all live inside the encrypted vault.

### Encryption boundaries

The following are outside "application data encryption":

- **Network metadata**: peer addresses, ports, traffic volume and timing are visible to network observers. V6Link does not provide anonymity or traffic obfuscation.
- **Sender's source file**: a file you drag in is not copied into the vault; it already exists on your disk. Only the transfer is encrypted.
- **Plaintext in the download folder**: after a receive completes, a plaintext copy appears there. It is no longer protected by the vault and is not deleted on exit.
- **Open / export**: opening a file that was not auto-saved decrypts it to the system temp directory; "Save as" writes a plaintext copy where you choose.
- **Clipboard**: copying cards, fingerprints or messages puts them on the system clipboard. Paste, screenshots and screen recording are outside V6Link's control.
- **System notifications**: while minimized, message previews and friend requests go through the OS notification center and may persist in notification history, lock screens, screen shares or recordings.
- **Operating system**: DPAPI binds the key to the current Windows user. Other processes of the same user, unlocked sessions, malware or memory scrapers can still read decrypted data. V6Link does not defend a compromised OS.
- **No password mode**: the current version asks for no extra password; unlocking relies on the Windows user credentials. If you need a vault that requires a password after shutdown, wait for a future version or use full-disk encryption.

> The protocol is a custom implementation and has not been independently audited. Treat it as a transparent engineering implementation, not a certified security product.

---

## Performance

The transfer path deliberately avoids common bottlenecks:

- Binary raw frames, no Base64, no compression.
- 1 MiB chunks per connection, asynchronous `drain()` backpressure, no per-chunk ACK.
- 4 independent TCP + encrypted data connections pull file chunks in parallel by default.
- The receiver writes chunks at their offset directly into the encrypted container; no plaintext staging or rewrite.
- AES-256-GCM authenticates every frame; completion verifies chunk count, lengths and total size.

On a local IPv6 loopback test (Windows 11, 4 streams), a 64 MiB encrypted file (transport encryption plus encrypted local writes) takes about 0.6 seconds. Real throughput depends on the IPv6 path, disk, CPU and firewall. The stream count is configurable in Settings.

---

## Project layout

```
v6link/
  crypto.py       Ed25519 identity, X25519 handshake, AES-GCM frames
  keyvault.py     DPAPI / permission-protected master key
  vault.py        AES-256-GCM encrypted local state
  filestore.py    random-access encrypted attachment container
  netutil.py      IPv6 parsing, address discovery, filename safety
  sharecard.py    contact-card parsing (no Qt dependency)
  updates.py      version comparison, update signing/verification, staging, PowerShell install
  engine/         networking engine (facade + services)
    core.py       ChatEngine facade, lifecycle, cross-service orchestration
    connections.py / contacts.py / messaging.py / updates.py
    files/        file transfer: inbound / outbound / exports / service
    router.py     inbound command dispatch
    events.py / tasks.py / transport.py / models.py
  runtime.py      background asyncio thread and thread-safe API
  ui/
    app.py        entry point, single-instance lock
    bridge.py     queued events from the backend thread to Qt
    theme.py      palette and QSS
    icons.py      runtime vector icons and avatars
    widgets.py    contact list, message bubbles, drag and drop, composer
    dialogs.py    add contact, settings, contact card, friend requests
    tray.py       system tray, desktop notifications and quit
    main_window.py  main window
requirements.txt  runtime dependencies
requirements-build.txt  build dependencies (PyInstaller)
tests/            crypto, storage and IPv6 end-to-end tests
assets/           Windows exe icon
packaging/        version info and build script
V6Link.spec       PyInstaller one-file build
main.py           entry point
run.bat           Windows one-click launcher (ASCII only)
dist/V6Link.exe   build output (generated, shareable)
```

---

## Building the Windows exe

The output is a single `dist\V6Link.exe` with no console window.

Install the build tools once:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1
```

Or call PyInstaller directly:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean V6Link.spec
```

Build configuration:

- [V6Link.spec](V6Link.spec): onefile, no console, icon, version info, unused Qt modules excluded.
- [assets/V6Link.ico](assets/V6Link.ico): Windows icon (16–256 px).
- [packaging/version_info.txt](packaging/version_info.txt): exe version info.
- [packaging/build_exe.ps1](packaging/build_exe.ps1): build script.

The build is unsigned. For public distribution, use a code-signing certificate to reduce SmartScreen and antivirus false positives.

To inspect which file a one-file build would replace during a peer-pushed update:

```powershell
.\dist\V6Link.exe --diagnose
```

It prints `sys.executable`, `_MEIPASS` and `persisted_executable` (the file that would actually be replaced) and writes `%TEMP%\v6link-diagnose.json`.

---

## Tests

Covered by the current suite:

- handshake, identity pinning, rejection of a wrong fingerprint;
- no message plaintext in captured bytes;
- vault and attachment container encryption and tamper detection;
- friend requests and bidirectional text;
- file offer consent and no file written after rejection;
- automatic save to a configurable download folder with the path recorded;
- download-folder setting persisted across restarts;
- opening the containing folder after export;
- learning a changed IPv6 source address and addresses announced in hello;
- cancel from either side deletes partial chunks;
- pause after network loss or restart, then resume from the chunk bitmap with identical content;
- parallel bidirectional file transfer and decryption consistency;
- version comparison, update signatures bound to identity, rejection of tampered or malformed fields;
- full peer-to-peer update flow: offer, staging and verification, decline notification, refusal when the receiver is not older, and cleanup when a download is cancelled.

Run:

```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The IPv6 end-to-end suites skip automatically when the host has no IPv6 loopback (as on GitHub-hosted runners). Set `V6LINK_SKIP_E2E=1` to skip them explicitly.

---

## Troubleshooting

### "Peer identity fingerprint mismatch"

V6Link pins each contact's Ed25519 fingerprint. The warning means the key presented now differs from the stored one. Common causes: the peer reinstalled or cleared the data directory, switched Windows user or machine, or the address now points at another V6Link instance.

If the identity change is genuine, verify the new fingerprint out of band and exchange contact cards again. Since 0.3.0, the card contains only globally routable IPv6 addresses.

### Replies fail after the peer's IP changes

There is no central server or address directory. The local side learns a new address only when the peer initiates a connection (message, file or friend request); V6Link records both the TCP source address and the IPv6 addresses announced in the encrypted hello, and refreshes the contact immediately.

If the peer changed networks without contacting you, there is no way to learn the new address. Ask them to send a message or a new card, and make sure their new network allows inbound TCP.

### The two machines still cannot connect

1. Both machines need a mutually reachable IPv6 address. Check `ipconfig` for a global address (not `fe80::`).
2. Try `ping -6 <peer-global-ipv6>` from the other machine.
3. Allow inbound TCP on the receiving machine (default `4455`); both sides need it if both should accept connections.
4. On IPv4-only networks, or when the router/firewall blocks inbound IPv6, V6Link cannot connect. It has no relay and does not do NAT traversal.
5. Cards never contain `fe80::` addresses: they only work on the same link and the scope is machine-specific. For same-link debugging, enter a scoped fe80 address manually.

---

## Known limitations

- Both sides need reachable IPv6; no NAT traversal, relay or offline messages.
- Without out-of-band fingerprint verification, the first contact is vulnerable to a man-in-the-middle; verify fingerprints through another channel.
- No group chat, voice/video, message editing or deletion.
- Resume requires the same `transfer_id` and SHA-256; a changed source file cannot resume.
- Auto-download keeps two copies (vault and download folder), using twice the disk space; the plaintext copy is not vault-protected.
- Cancel deletes partial chunks and cannot resume; only automatic pauses (network loss, offline, restart) can resume.
- Older clients cannot connect to this version (the application protocol has changed).
- Single instance per data directory; the data directory cannot be decrypted by another user or machine (DPAPI).
- V6Link does not hide IP addresses, ports, traffic volume or timing.
- Messages to a contact that has not accepted the friend request stay "waiting" and retry automatically; they are not delivered until the request is accepted.

---

## Data directory and backup

Default data directory:

- Windows: `%LOCALAPPDATA%\V6Link`
- macOS: `~/Library/Application Support/V6Link`
- Linux: `$XDG_DATA_HOME/v6link` or `~/.local/share/v6link`

Use `--data-dir` to override it.

**Backup warning**: back up `vault.v6` and `vault.key` together, and note that `vault.key` is protected by the current Windows user's DPAPI. Copying the data to another machine or user usually makes it unreadable. There is no cross-machine migration or password export yet.

---

## License

V6Link is released under the [MIT License](LICENSE), Copyright (c) 2026 HarryXiaoCN.

Third-party licenses and binary distribution notes (PySide6/Qt LGPL, cryptography, psutil) are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (Chinese).

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) (Chinese) before submitting; participation implies agreement with [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Please do **not** report security vulnerabilities in public issues; follow [SECURITY.md](SECURITY.md) (Chinese) for the private channel.

## Author

HarryXiaoCN

## Acknowledgements

Thanks to PySide6/Qt, cryptography, psutil and the other open-source projects, and to everyone who filed issues and tested the app.
