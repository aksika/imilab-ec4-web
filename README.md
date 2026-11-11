# IMILAB EC4 Web

Low-latency local viewer for IMILAB EC4 cameras.  The media path is entirely
on the Mac and on the LAN: the Mac opens the camera through the private relay,
negotiates the P2P/ICE session, receives RTP directly from the camera, and
serves H.264 fragmented MP4 to the browser through Media Source Extensions.

> **Unofficial project.** Not affiliated with or endorsed by IMILAB. Use only
> with cameras and accounts you own. The helper talks to private/undocumented
> services that may change or break; there is no warranty. You must supply
> your own local configuration — no app keys, tokens, or account data are
> included in this repository. Compliance with IMILAB's terms is your
> responsibility.

There is **no Android emulator and no app requirement at runtime**, and media
does not go through the cloud relay.

## What is in this repository

| Path | Purpose |
|---|---|
| `web/ec4_server.py` | local HTTP server, camera picker, `/stream/direct` |
| `web/ec4_direct.py` | pure-Mac capture backend (relay viewer + RTP + ffmpeg) |
| `web/ec4_capture.py` | ffmpeg H.265 → H.264 fMP4 encoder |
| `web/ec4_mac.py` | RTP receiver, keyframe assembly, legacy app fallback |
| `web/static/` | browser UI (MSE player, camera grid, settings) |
| `tools/direct_signal.py` | stream query, relay trigger, signal/ICE negotiation |
| `tools/rtmp_private_viewer.py` | private `glive` relay viewer |
| `tools/iot_auth.py` | Alibaba IoT API auth/stream query |
| `tools/imi_auth.py` | Imilab session token refresh |
| `tools/imi_login.py` | Imilab password login + OpenAccount/IoT exchange |
| `tools/mac_keychain.py` | macOS Keychain storage for the login helper |

## Configuration (required)

The repository intentionally contains **no app keys, account IDs, camera IDs,
LAN addresses, tokens, or passwords**.  They live outside the repository.

```sh
mkdir -p ~/.ec4
cp config.example.json ~/.ec4/config.json
chmod 600 ~/.ec4/config.json
```

Fill in:

* `imi.app_id`, `imi.app_secret` — the Imilab application identity used by the
  login exchange.
* `imi.identity_id` — the Alibaba identity associated with the Imilab account
  (optional; the helper fills it after login).
* `iot.app_key`, `iot.xca_key` — the Alibaba IoT gateway credentials.
* `iot.host` — the regional IoT API host.
* `openaccount.host` — the matching regional OpenAccount host.
* `cameras` — display name → camera `iotId`.

The helper stores its runtime state outside the repository as well:

```text
~/.ec4/config.json        configuration, mode 0600
~/.ec4/imi_tokens.json    Imilab access/refresh tokens, mode 0600
~/.ec4/iot_creds.json     Alibaba IoT token/refresh token, mode 0600
```

The Imilab password is stored in the **macOS Keychain** (service
`ec4-imilab`), never in a cookie, localStorage, or a repository file.

## Requirements

* macOS
* Python 3.10+
* `ffmpeg` with `libx264` (`brew install ffmpeg`)
* Local network access to the camera and hub

The launcher creates `.venv` and installs:

```text
websockets cryptography keyring
```

Frida is **not** required by the direct Mac path.  Only the legacy
app/emulator fallback, if explicitly enabled, needs the optional `frida`
package and Frida server.

## Run

```sh
./run_web.sh
```

or double-click `EC4 Web.command`.

Open <http://127.0.0.1:8765/>.  Camera names come from `cameras` in
`~/.ec4/config.json`.

Sign in once at <http://127.0.0.1:8765/settings> with the Imilab account
password.  This populates the Keychain and creates the Alibaba IoT credential
pair.  After that the helper refreshes the IoT token and falls back to a
Keychain re-login when the refresh token expires.

Useful helper commands:

```sh
python tools/imi_login.py --status
python tools/imi_login.py --bootstrap
python tools/imi_login.py --refresh
```

### Adding a camera

Open `/settings` → **Add a camera**.  The camera list is fetched from the
Imilab cloud; selecting one fills in its **name and IoT ID** automatically
(the IoT ID is an Alibaba cloud device identity, not a LAN address).  You can
attach the LAN IP/MAC from a scan result, then click **Add camera**.  The new
camera appears on the main page after you click **Refresh**.

### LAN camera scan

Open `/settings` and click **Scan local network**.  The scanner:

1. Does a fast ping sweep of the local `/24` and reads the ARP table.
2. Probes a small set of camera/device ports.
3. Matches saved camera IP/MACs from `~/.ec4/camera_hosts.json`.
4. Marks other camera-like hosts (`:8000`, without `:80/:443/:1883`) as
   possible cameras.

For an existing configured camera, choose its name and click **Remember** in
the scan result.  **Use IP** fills the LAN fields in the Add Camera form.  The
mapping is saved outside the repository in `~/.ec4/camera_hosts.json` (mode
0600).  The scan is local-only; it does not use the cloud relay.

## Media path

```text
camera --LAN P2P--> Mac relay viewer
                        |
                        +--> signal/ICE negotiation
                        +--> RTP (AES keyframe fragments)
                        +--> H.265 access-unit assembly
                        +--> ffmpeg (H.264 fMP4)
                        +--> browser MSE
```

Only one camera is opened at a time.  Switching cameras tears down the old
viewer/receiver/encoder and starts a new session.

### Video pipeline note

Do not add ffmpeg's `-fflags nobuffer` to the input.  With
`-use_wallclock_as_timestamps 1` it drops HEVC reference frames and produces
grey/purple macroblocks, often at the bottom of the picture.  The encoder also
loops on `stdin.write()` so a large keyframe is never partially written.

## Security

* No credentials or private identifiers are committed.
* `.gitignore` excludes local caches, sessions, logs, captures, media, and the
  virtualenv.
* The browser receives only a local `ec4_session` HttpOnly cookie for the
  helper UI; cloud credentials stay in the helper.
* The server binds to `127.0.0.1` by default.  `--host 0.0.0.0` exposes it on
  the LAN without authentication, so use it only on a trusted network.

## Troubleshooting

* **No cameras in the page** — check `cameras` in `~/.ec4/config.json`.
* **IoT credential error** — run `python tools/imi_login.py --status`; sign in
  through `/settings` to refresh the credential chain.
* **Grey/purple blocks** — verify the ffmpeg command does not contain
  `-fflags nobuffer`.
* **Port already in use** — stop the old server or start with
  `EC4_PORT=8766 ./run_web.sh`.
