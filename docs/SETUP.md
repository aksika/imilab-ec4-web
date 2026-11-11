# Setup

## 1. Requirements

* macOS
* Python 3.10+
* `ffmpeg` with `libx264` (`brew install ffmpeg`)
* Network access to the camera and hub

The launcher creates `.venv` and installs:

```text
websockets cryptography keyring
```

Frida is not required for the direct path.

## 2. Local configuration

The repository contains **no credentials**.  They must live outside it:

```sh
mkdir -p ~/.ec4
cp config.example.json ~/.ec4/config.json
chmod 600 ~/.ec4/config.json
```

Fill in the values for your installation:

* `imi.app_id`, `imi.app_secret` — Imilab application identity.
* `imi.identity_id` — Alibaba identity (optional; the helper fills it).
* `iot.app_key`, `iot.xca_key` — Alibaba IoT gateway credentials.
* `iot.host` — regional IoT API host.
* `openaccount.host` — matching regional OpenAccount host.
* `cameras` — display name → camera `iotId`.

These values are not distributed with the project.  You must supply the
credentials used by your own camera installation.

> **The Settings form only handles the Imilab account username/password.**
> It cannot create the app identity or IoT gateway credentials.  Those must
> already be present in `~/.ec4/config.json` before the first sign-in; otherwise
> the helper cannot sign the SDK login or call the IoT cloud APIs.

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

## 4. Add cameras

Open `/settings` → **Add a camera**:

* the name and `iotId` are filled from the Imilab cloud device list;
* optional LAN IP/MAC can be attached from a scan result;
* after adding, open the main page and click **Refresh**.

The main page re-reads `~/.ec4/config.json` on Refresh; no server restart is
needed for a newly added camera.

## 5. LAN scan

**Settings → Cameras on the LAN → Scan local network** does a ping/ARP sweep
and TCP fingerprinting.  It matches saved camera IP/MACs from
`~/.ec4/camera_hosts.json`; unknown camera-like hosts can be remembered.

The scan is local-only and does not use the cloud relay.

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
* Cloud list or login failures — run `python tools/imi_login.py --status`, then
  sign in through `/settings`.
* Grey/purple blocks — ensure ffmpeg is not using `-fflags nobuffer`.
* Port already in use — set `EC4_PORT=8766 ./run_web.sh`.
