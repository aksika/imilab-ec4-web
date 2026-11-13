# Setup

## 1. Requirements

* macOS
* Python 3.10+
* `ffmpeg` with `libx264` for the H.264 path (`brew install ffmpeg`); the
  H.265 passthrough mode only needs ffmpeg’s built-in HEVC muxer
* A browser with HEVC support in Media Source Extensions for H.265 passthrough
* Network access to the camera and hub

The launcher creates `.venv` and installs:

```text
websockets cryptography keyring
```

## 2. Local configuration

The repository ships **app-level client defaults** in `config.example.json`.
Account credentials, tokens, and camera IDs must live outside it:

```sh
mkdir -p ~/.ec4
cp config.example.json ~/.ec4/config.json
chmod 600 ~/.ec4/config.json
```

Fill in the values for your installation:

* `imi.app_id`, `imi.app_secret` — Imilab application identity.  The example
  includes the EU app-level defaults.
* `imi.identity_id` — Alibaba identity (optional; the helper fills it).
* `iot.app_key`, `iot.xca_key` — Alibaba IoT gateway credentials.  The example
  includes the EU app-level defaults.
* `iot.host` — regional IoT API host.
* `openaccount.host` — matching regional OpenAccount host.
* `cameras` — display name → camera `iotId`; starts empty and is filled by
  **Add a camera** in Settings.

The app-level defaults are usable for the same app build/region.  Account
tokens, identity IDs, camera IDs, and LAN data are not included.

> **The Settings form only handles the Imilab account username/password.**
> It does not create app identity or IoT gateway credentials.  The included
> defaults cover those; user-specific values are created after sign-in.

Tokens are written to:

```text
~/.ec4/imi_tokens.json    mode 0600
~/.ec4/iot_creds.json     mode 0600
```

The login password is stored in the macOS Keychain (service `ec4-imilab`).

## 3. Run

```sh
./run_web.sh
```

or double-click `EC4 Web.command`.

Open <http://127.0.0.1:8765/>.  Sign in once at `/settings` with the Imilab
account password.  The helper refreshes the cloud credentials and stores the
password in Keychain.

The top-bar codec selector applies on the next **Start**:

* **Auto codec** chooses H.265 passthrough when the browser reports HEVC
  support in Media Source Extensions, otherwise H.264.
* **H.264** re-encodes with libx264 and caps the stream at 1280×720.
* **H.265 passthrough** copies the camera’s HEVC bitstream without conversion
  or scaling.  The browser must support HEVC in MSE.

## 4. Add and remove cameras

Open `/settings` → **Add a camera**:

* the name and `iotId` are filled from the Imilab cloud device list;
* a LAN IP/MAC is needed before the camera can be started; it can be attached
  from a scan result or entered manually;
* after adding, open the main page and click **Refresh**.

**Your cameras** lists every configured camera with its local-network state
and a **Remove** action.  **Bind IP** changes or sets the saved address; the
scan can also bind a discovered host.  Removing the active camera stops its
capture first.  Removal deletes the entry from `~/.ec4/config.json` and its
address from `~/.ec4/camera_hosts.json`.

A camera with no saved address, or an address outside the Mac’s local
networks, is shown as **NO LAN** or **REMOTE** and cannot be started.  The
main page re-reads `~/.ec4/config.json` on Refresh; no server restart is
needed for a newly added or removed camera.

## 5. LAN scan

**Settings → Cameras on the LAN → Scan local network** does a ping/ARP sweep
and TCP fingerprinting.  It matches saved camera IP/MACs from
`~/.ec4/camera_hosts.json`; unknown camera-like hosts can be remembered.

The **Interface / network** picker lists the Mac’s active private IPv4
interfaces.  Choosing one scans that interface’s subnet (`cidr`); **Automatic**
follows the default route.  The scan is local-only and does not use the cloud
relay.  A battery camera may be asleep and absent from a scan even when it is
local.

Starting a camera whose saved address is outside all of the Mac’s local
networks fails immediately with a rebind message.  This rejects cameras that
belong to another location without waiting for a stream timeout.  The check
uses network membership only and never pings the camera, so a sleeping local
camera still starts normally.

## 6. Important limits

* **Cold start needs internet.**  Starting or switching a camera requires the
  cloud stream query, relay, and signal negotiation.  The media itself is
  local once running.
* The direct path is for cameras on the **same LAN** as the Mac.  Remote-home
  access via Tailscale is not supported by this transport; run a helper on the
  remote LAN instead.
* Only one camera stream is opened at a time.

## Troubleshooting

* No cameras in the page — check `cameras` in `~/.ec4/config.json`.
* Camera shows **NO LAN** or **REMOTE** — it has no saved address or its
  address is outside this Mac’s networks. Use **Bind IP** or scan the correct
  interface, or remove it under **Settings → Your cameras**.
* Cloud list or login failures — run `python tools/imi_login.py --status`, then
  sign in through `/settings`.
* H.265 tile stays black or reports a codec error — the browser/OS lacks HEVC
  support in Media Source Extensions. Choose **Auto codec** or **H.264**.
* Grey/purple blocks — ensure ffmpeg is not using `-fflags nobuffer`.
* Port already in use — set `EC4_PORT=8766 ./run_web.sh`.
