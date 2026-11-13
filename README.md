# IMILAB EC4 Web

Low-latency local viewer for IMILAB EC4 cameras.  The media path is entirely
local and on the LAN: the helper opens the camera through the private relay,
negotiates the P2P/ICE session, receives RTP directly from the camera, and
serves fragmented MP4 to the browser through Media Source Extensions.  The
output is H.264 by default, or H.265 passthrough when the browser supports HEVC.

> **Unofficial project.** Not affiliated with or endorsed by IMILAB. Use only
> with cameras and accounts you own. The helper talks to private/undocumented
> services that may change or break; there is no warranty. The repository
> includes app-level client defaults in `config.example.json`; no account
> tokens, camera IDs, or user data are included. Compliance with IMILAB's terms
> is your responsibility.

Media does not go through the cloud relay.

## What is in this repository

| Path | Purpose |
|---|---|
| `web/ec4_server.py` | local HTTP server, camera picker, `/stream/direct` |
| `web/ec4_direct.py` | direct capture backend (relay viewer + RTP + ffmpeg) |
| `web/ec4_encoder.py` | ffmpeg H.264 encoder or H.265 passthrough to fMP4 |
| `web/ec4_rtp.py` | RTP receiver, keyframe assembly, H.265 access units |
| `web/static/` | browser UI (MSE player, camera grid, settings) |
| `tools/direct_signal.py` | stream query, relay trigger, signal/ICE negotiation |
| `tools/rtmp_private_viewer.py` | private `glive` relay viewer |
| `tools/iot_auth.py` | Alibaba IoT API auth/stream query |
| `tools/imi_auth.py` | Imilab session token refresh |
| `tools/imi_login.py` | Imilab password login + OpenAccount/IoT exchange |
| `tools/credentials.py` | OS credential-store storage for the login helper |
| `run_web.py` | cross-platform launcher (venv, config, server) |
| `docs/ARCHITECTURE.md` | public-safe architecture and limitations |
| `docs/SETUP.md` | configuration, running, camera adding, troubleshooting |

## Configuration (required)

`config.example.json` contains the **app-level client defaults** for the EU
region.  Account tokens, camera IDs, and user data are never included.  The
launcher copies this file to `~/.ec4/config.json` on first run.

```sh
mkdir -p ~/.ec4
cp config.example.json ~/.ec4/config.json
chmod 600 ~/.ec4/config.json
```

Defaults included:

* `imi.app_id`, `imi.app_secret` — Imilab application identity.
* `iot.app_key`, `iot.xca_key` — Alibaba IoT gateway credentials.
* `iot.host`, `openaccount.host` — EU regional endpoints.

You only need to change the host fields if your account is in another region.
`imi.identity_id` is optional; the helper fills it after login.  Cameras are
added from the cloud list in `/settings`; `cameras` starts empty.
* `cameras` — display name → camera `iotId`.

The cloud list is discovery only: an account may contain cameras at another
location.  Settings marks each camera as local, outside the LAN, or unbound,
and lets you **Remove** any configured camera; removing the active one stops
the stream first.  Only cameras whose saved address belongs to one of the
host’s local networks get a working Start control.  Remote or unbound cameras
show **REMOTE** or **NO LAN** and stay disabled until you bind their address
with a scan or **Bind IP**.

The start guard checks network membership only; it never pings the camera, so
a sleeping battery camera still follows the normal relay-wake path.  LAN
binding is therefore useful even when a scan cannot see a sleeping camera.

The helper stores its runtime state outside the repository as well:

```text
~/.ec4/config.json        configuration, mode 0600
~/.ec4/imi_tokens.json    Imilab access/refresh tokens, mode 0600
~/.ec4/iot_creds.json     Alibaba IoT token/refresh token, mode 0600
```

The Imilab password is stored in the **operating system credential store**
through `keyring` (Keychain on macOS, Credential Manager on Windows, Secret
Service/KWallet on Linux), never in a cookie, localStorage, or a repository
file.

## Requirements

* Python 3.10+
* `ffmpeg` with `libx264` for the H.264 path; H.265 passthrough only needs
  ffmpeg’s built-in HEVC muxer
* A working `keyring` backend for the login helper
* A browser with HEVC support in Media Source Extensions for H.265 passthrough
* Local network access to the camera and hub

On Linux, install `iproute2` and `iputils-ping` if they are not already
present.  Debian/Ubuntu also need `python3-venv` for the launcher.  The H.264
path needs an ffmpeg build with libx264; H.265 passthrough does not.

`run_web.sh` creates `.venv` and installs:

```text
websockets cryptography keyring
```

If you start `web/ec4_server.py` directly, install those packages yourself.

## Platform support

The capture, relay, RTP, and ffmpeg pipeline are Python/socket based and are not
tied to a particular desktop OS.

* **macOS** — primary development platform, including the `EC4 Web.command`
  convenience launcher.
* **Linux** — minimal support. Interface discovery uses `ip -4 -o addr show`
  (falling back to `ifconfig`), the LAN scan uses `ip neigh` and GNU `ping`,
  browser opening uses `webbrowser`, and the viewer log uses the system temp
  directory. Start it with `python3 run_web.py`.
* **Windows** — not yet supported. The remaining work is Windows ARP/ping
  parsing (`arp -a`, `ping -n/-w`), interface discovery via `ipconfig`, and
  launcher/firewall testing.

H.265 passthrough itself is portable: it is ffmpeg `-c:v copy` plus Python
sockets. H.264 output only needs an ffmpeg build with libx264. Linux has the
adapters implemented but still needs testing on real camera hardware; macOS
remains the reference platform.

## Run

```sh
python3 run_web.py
```

The launcher creates `.venv`, installs dependencies, seeds
`~/.ec4/config.json`, and starts the server.  On a headless Linux host, use
`python3 run_web.py --no-open`; the server binds `127.0.0.1` by default.  On
macOS/Linux the shell launcher is equivalent:

```sh
./run_web.sh
```

After installing the requirements, the server can also be started directly:

```sh
python web/ec4_server.py
```

Open <http://127.0.0.1:8765/>.  Camera names come from `cameras` in
`~/.ec4/config.json`.

The viewer starts **off**.  Pick a camera tile or press **Start**; **Stop**
tears down the relay viewer, P2P receiver, and encoder.  Opening or reloading
the page does not start a camera by itself.

The top-bar codec selector controls the next start:

* **Auto codec** — uses H.265 passthrough when the browser’s Media Source
  Extensions reports HEVC support, otherwise H.264.
* **H.264** — decodes and re-encodes with libx264, capped at 1280×720.
* **H.265 passthrough** — remuxes the camera’s HEVC bitstream without
  re-encoding or scaling; the browser must support HEVC in MSE.

Changing the selector while a camera is live applies after **Stop** and
**Start**.  If the browser cannot decode the resulting codec, the tile reports
the MSE error instead of silently switching.

Each tile also shows the camera’s last cloud-reported **battery percentage**
and voltage (on hover).  The helper caches battery properties for about 90
seconds and falls back to the last known value when the cloud is unavailable.
This uses the IoT property API, not the video stream.

Sign in once at <http://127.0.0.1:8765/settings> with the Imilab account
password.  This populates the OS credential store and creates the Alibaba IoT
credential pair.  After that the helper refreshes the IoT token and falls back
to a credential-store re-login when the refresh token expires.

Useful helper commands:

```sh
python tools/imi_login.py --status
python tools/imi_login.py --bootstrap
python tools/imi_login.py --refresh
```

### Adding a camera

Open `/settings` → **Add a camera**.  The camera list is fetched from the
Imilab cloud; selecting one fills in its **name and IoT ID** automatically
(the IoT ID is an Alibaba cloud device identity, not a LAN address).  A camera
needs a LAN IP on one of the host’s local networks before it can start, so
attach the LAN IP/MAC from a scan result or enter it manually.  The new camera
appears on the main page after you click **Refresh**.  A camera without a LAN
address is added but shown as **NO LAN** and disabled.

### LAN camera scan

Open `/settings` and click **Scan local network**.  The **Interface / network**
picker lists active private IPv4 interfaces; choose the one that reaches the
camera, or leave **Automatic** to use the default route.  The scanner:

1. Pings the selected subnet and reads the ARP table.
2. Probes a small set of camera/device ports.
3. Matches saved camera IP/MACs from `~/.ec4/camera_hosts.json`.
4. Marks other camera-like hosts (`:8000`, without `:80/:443/:1883`) as
   possible cameras.

For an existing configured camera, choose its name and click **Remember** in
the scan result, or use **Bind IP** in **Your cameras**.  **Use IP** fills the
LAN fields in the Add Camera form.  The mapping is saved outside the
repository in `~/.ec4/camera_hosts.json` (mode 0600).  The scan is local-only;
it does not use the cloud relay.

## Media path

```text
camera --LAN P2P--> local relay viewer
                        |
                        +--> signal/ICE negotiation
                        +--> RTP (AES keyframe fragments)
                        +--> H.265 access-unit assembly
                        +--> ffmpeg (H.264 transcode or H.265 copy)
                        +--> browser MSE
```

Only one camera is opened at a time.  Switching cameras tears down the old
viewer/receiver/encoder and starts a new session.

### Video pipeline note

Do not add ffmpeg's `-fflags nobuffer` to the input.  With
`-use_wallclock_as_timestamps 1` it drops HEVC reference frames and produces
grey/purple macroblocks, often at the bottom of the picture.  The encoder also
loops on `stdin.write()` so a large keyframe is never partially written.
H.265 passthrough does not scale or re-encode, so it preserves the camera’s
native resolution and uses much less CPU than the H.264 path.

## Security

* No account tokens, camera IDs, or user data are committed; app-level client
  defaults are in `config.example.json`.
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
* **Port already in use** — stop the old server or set `EC4_PORT=8766` before
  starting it.
