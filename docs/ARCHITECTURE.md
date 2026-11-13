# Architecture

This is a local, browser-based viewer for IMILAB EC4 cameras.  It is not a
cloud streaming client: the camera is opened through its private relay, then
the Mac negotiates a P2P/ICE session and receives RTP directly over the LAN.

```text
browser (MSE)
     ^
     | H.264 or H.265 fragmented MP4
     |
web/ec4_server.py  <-->  DirectCapture
                             |
                             +--> private relay viewer (bootstrap)
                             +--> cloud signal / ICE negotiation
                             +--> LAN RTP receiver
                             +--> ffmpeg H.265 -> H.264 transcode
                                  or H.265 copy (passthrough)
```

## Cold start vs media

* **Cold start needs internet.**  The helper obtains a stream session through
  the Imilab/Alibaba cloud, opens the camera through the private relay, and
  exchanges ICE candidates through the cloud signal service.
* **Media is local.**  Once the P2P session is established, RTP flows from the
  camera to the Mac over the LAN.  The browser and ffmpeg are local.
* The direct path does **not** use an Android emulator, the Imilab app, or
  Frida.  Those legacy modules are not part of this public repository.

## Camera identity

The camera's `iotId` is an Alibaba cloud device identity.  It is read from the
account's cloud device list when adding a camera.  It is **not** derived from
the LAN address.  The LAN scan maps a known camera to an IP/MAC; it cannot
invent an `iotId`.

## Scope and limitations

* The direct receiver is designed for cameras on the same LAN as the Mac.
  It selects `192.168.*` host candidates and advertises the Mac's LAN IP.
* Remote-home viewing over the internet or Tailscale is not supported by this
  transport path.  Run the helper on a machine at the remote site, or use the
  cloud relay path instead.
* One camera stream is opened at a time.
* The camera list and identity mappings are stored outside the repository in
  `~/.ec4/config.json` and `~/.ec4/camera_hosts.json`.
* App-level client defaults live in `config.example.json`; account tokens,
  identity IDs, camera IDs, and LAN data are not included.
