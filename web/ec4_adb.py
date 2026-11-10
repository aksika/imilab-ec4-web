"""ADB control for the Imilab Home app.

The EC4 feed is produced by the vendor app.  The app's home screen has one
preview tile per camera; tapping the play button on a tile starts the local
P2P stream.  This module reads the tile list with uiautomator and taps the
right tile, so the web UI can switch cameras without the user touching the
emulator.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional

APP_ID = "com.chuangmi.imihome"
SPLASH = f"{APP_ID}/.activity.splash.SplashActivity"
HOME_ACTIVITY = "ImiHomeMainActivity"
UI_PATH = "/sdcard/ec4_ui.xml"

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _adb_candidates() -> list[str]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        os.environ.get("ADB", ""),
        shutil.which("adb") or "",
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
        os.path.join(here, "artifacts", "adb"),
    ]


def find_adb() -> Optional[str]:
    for path in _adb_candidates():
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def parse_bounds(node: ET.Element) -> Optional[tuple[int, int, int, int]]:
    m = BOUNDS_RE.search(node.get("bounds", ""))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def rid(node: ET.Element) -> str:
    value = node.get("resource-id", "")
    return value.rsplit("/", 1)[-1] if "/" in value else value


def descendants(node: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(list(cur))
    return out


def first_with_id(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node.iter():
        if rid(child) == name:
            return child
    return None


class AdbError(RuntimeError):
    pass


class AdbController:
    def __init__(self, log=print) -> None:
        adb = find_adb()
        if not adb:
            raise AdbError("adb not found. Install Android platform-tools or set ADB=/path/to/adb")
        self.adb = adb
        self.log = log
        self._screen: Optional[tuple[int, int]] = None
        self._camera_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._dump_cache: tuple[float, ET.Element | None] = (0.0, None)

    # -- low level ----------------------------------------------------------
    def run(self, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.adb, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb {' '.join(args)} timed out") from exc

    def shell(self, *args: str, timeout: float = 20.0) -> str:
        proc = self.run("shell", *args, timeout=timeout)
        return proc.stdout.decode("utf-8", "replace")

    def exec_out(self, *args: str, timeout: float = 20.0) -> bytes:
        proc = self.run("exec-out", *args, timeout=timeout)
        return proc.stdout

    def device_online(self) -> bool:
        out = self.run("devices", timeout=8.0).stdout.decode("utf-8", "replace")
        return re.search(r"^emulator-\d+\s+device$", out, re.M) is not None

    def screen_size(self) -> tuple[int, int]:
        if self._screen:
            return self._screen
        out = self.shell("wm", "size")
        m = re.search(r"(\d+)x(\d+)", out)
        self._screen = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        return self._screen

    def keyevent(self, code: int) -> None:
        self.shell("input", "keyevent", str(code))

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 350) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def scroll(self, up: bool = True, ms: int = 400) -> None:
        """Scroll the device list.  Use the screen edge: swipes started on a
        video tile are often consumed by the tile's gesture detector."""
        w, h = self.screen_size()
        x = max(1, w - 8)
        if up:
            y1, y2 = int(h * 0.82), int(h * 0.34)
        else:
            y1, y2 = int(h * 0.34), int(h * 0.82)
        self.swipe(x, y1, x, y2, ms)

    def current_activity(self) -> str:
        out = self.shell("dumpsys", "activity", "activities", timeout=15.0)
        for line in out.splitlines():
            if "topResumedActivity" in line or "mResumedActivity" in line:
                m = re.search(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)", line)
                if m:
                    return m.group(1)
        return ""

    # -- app ----------------------------------------------------------------
    def start_app(self) -> None:
        self.log("starting Imilab Home…")
        self.run("shell", "am", "start", "-n", SPLASH, timeout=20.0)
        deadline = time.time() + 25.0
        while time.time() < deadline:
            if HOME_ACTIVITY in self.current_activity():
                return
            time.sleep(1.0)
        raise AdbError("Imilab Home did not reach the home screen")

    def ensure_home(self) -> None:
        if not self.device_online():
            raise AdbError("No Android emulator/device found. Start the emulator first.")
        if HOME_ACTIVITY in self.current_activity():
            return
        for _ in range(3):
            self.keyevent(4)  # back
            time.sleep(0.8)
            if HOME_ACTIVITY in self.current_activity():
                return
        self.start_app()

    def dump_ui(self, force: bool = False, retries: int = 2) -> Optional[ET.Element]:
        now = time.time()
        if not force and self._dump_cache[1] is not None and now - self._dump_cache[0] < 1.0:
            return self._dump_cache[1]
        for attempt in range(retries):
            self.shell("uiautomator", "dump", UI_PATH, timeout=20.0)
            raw = self.exec_out("cat", UI_PATH, timeout=15.0)
            try:
                root = ET.fromstring(raw.decode("utf-8", "replace"))
            except ET.ParseError:
                time.sleep(0.6)
                continue
            self._dump_cache = (time.time(), root)
            return root
        return None

    # -- camera tiles -------------------------------------------------------
    @staticmethod
    def _parse_cameras(root: ET.Element, screen_h: int) -> list[dict[str, Any]]:
        cameras: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in root.iter():
            if rid(node) != "root_device_item":
                continue
            name_node = first_with_id(node, "item_device_name")
            if name_node is None or not (name_node.get("text") or "").strip():
                continue
            name = (name_node.get("text") or "").strip()
            video = first_with_id(node, "video_player_layout_root")
            play = first_with_id(node, "start_play_icon")
            pause = first_with_id(node, "player_toolbar_pause_lite")
            # Hub/gateway cards have a name but no video surface.  Skip them.
            if video is None and play is None and pause is None:
                continue
            if name in seen:
                continue
            seen.add(name)
            card_bounds = parse_bounds(node)
            play_bounds = parse_bounds(play) if play is not None else None
            pause_bounds = parse_bounds(pause) if pause is not None else None
            visible = False
            if card_bounds:
                _, y1, _, y2 = card_bounds
                visible = y1 < screen_h and y2 > 0
            cameras.append(
                {
                    "name": name,
                    "playing": pause_bounds is not None,
                    "play": play_bounds,
                    "pause": pause_bounds,
                    "bounds": card_bounds,
                    "visible": visible,
                }
            )
        # Preserve the order they appear on screen (top to bottom).
        cameras.sort(key=lambda c: (c["bounds"] or (0, 10**9, 0, 0))[1])
        return cameras

    def visible_cameras(self) -> list[dict[str, Any]]:
        root = self.dump_ui(force=True)
        if root is None:
            return []
        _, h = self.screen_size()
        return self._parse_cameras(root, h)

    def scan_cameras(self) -> list[dict[str, Any]]:
        """Scroll the home list once and collect every camera tile."""
        self.ensure_home()
        # Six long edge swipes reach the top; no need for a dump per swipe.
        self.reset_to_top()
        time.sleep(0.25)
        found: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        prev_sig: tuple[str, ...] | None = None
        page = 0
        for _ in range(8):
            visible = self.visible_cameras()
            if not visible and not found:
                time.sleep(0.6)
                visible = self.visible_cameras()
            for cam in visible:
                full = self._fully_visible(cam)
                if cam["name"] in found:
                    # Prefer an occurrence whose play button is fully visible.
                    if full and not found[cam["name"]].get("_full"):
                        cam["page"] = page
                        cam["_full"] = True
                        found[cam["name"]] = cam
                    continue
                cam["page"] = page
                cam["_full"] = full
                order.append(cam["name"])
                found[cam["name"]] = cam
            sig = tuple(cam["name"] for cam in visible)
            self.log(f"scan page {page}: {list(sig)}")
            if sig and sig == prev_sig:
                break
            prev_sig = sig
            page += 1
            time.sleep(0.35)
            self.scroll(up=True)
            time.sleep(0.65)
        for cam in self.visible_cameras():
            if cam["name"] not in found:
                cam["page"] = page
                cam["_full"] = self._fully_visible(cam)
                order.append(cam["name"])
                found[cam["name"]] = cam
        return [found[name] for name in order if name in found]

    def cameras(self, force: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if not force and self._camera_cache[1] and now - self._camera_cache[0] < 300.0:
            return self._camera_cache[1]
        cams = self.scan_cameras()
        if cams:
            self._camera_cache = (now, cams)
            self.log("cameras: " + ", ".join(cam["name"] for cam in cams))
            return cams
        return self._camera_cache[1] or self.visible_cameras()

    def mark_playing(self, name: Optional[str]) -> None:
        ts, cams = self._camera_cache
        for cam in cams:
            cam["playing"] = cam["name"] == name
        self._camera_cache = (ts, cams)

    def active_cameras(self) -> list[str]:
        return [cam["name"] for cam in self.cameras() if cam["playing"]]

    def _button(self, cam: dict[str, Any]) -> Optional[tuple[int, int]]:
        b = cam.get("pause") or cam.get("play")
        return center(b) if b else None

    def _fully_visible(self, cam: dict[str, Any]) -> bool:
        b = cam.get("pause") or cam.get("play")
        if not b:
            return False
        _, h = self.screen_size()
        return b[1] >= 0 and b[3] <= h

    def _nudge_into_view(self, cam: dict[str, Any]) -> None:
        b = cam.get("pause") or cam.get("play")
        if not b:
            return
        _, h = self.screen_size()
        top_margin = 260
        bottom_margin = h - 220
        if b[1] < top_margin:
            self.scroll(up=False, ms=300)
        elif b[3] > bottom_margin:
            self.scroll(up=True, ms=300)
        time.sleep(0.5)

    def find_camera(self, name: str, scroll: bool = True) -> Optional[dict[str, Any]]:
        name = name.strip()
        for cam in self.visible_cameras():
            if cam["name"] == name:
                if self._fully_visible(cam):
                    return cam
                self._nudge_into_view(cam)
                cam = next((c for c in self.visible_cameras() if c["name"] == name), cam)
                return cam
        if not scroll:
            return None
        # Go to the top first.
        for _ in range(4):
            self.scroll(up=False, ms=300)
            time.sleep(0.4)
        for _ in range(9):
            for cam in self.visible_cameras():
                if cam["name"] == name:
                    if self._fully_visible(cam):
                        return cam
                    self._nudge_into_view(cam)
                    return next((c for c in self.visible_cameras() if c["name"] == name), cam)
            self.scroll(up=True)
            time.sleep(0.6)
        return None

    def reset_to_top(self) -> None:
        # Six long edge swipes are enough for this list and are much faster
        # than dumping the hierarchy after every swipe.
        for _ in range(6):
            self.scroll(up=False, ms=260)
            time.sleep(0.25)
        time.sleep(0.35)

    def fast_play(self, name: str, layout: dict[str, Any]) -> dict[str, Any]:
        """Use the page/position recorded by the last scan.

        The final UI dump still verifies the tile and gives us the exact play
        button, so this cannot silently tap the wrong camera.
        """
        self.ensure_home()
        self.reset_to_top()
        page = int(layout.get("page") or 0)
        for _ in range(max(0, page)):
            self.scroll(up=True)
            time.sleep(0.65)
        cam = next((c for c in self.visible_cameras() if c["name"] == name), None)
        if cam is None:
            raise AdbError(f"fast layout missed {name!r}")
        time.sleep(0.25)
        if cam["playing"]:
            self.mark_playing(name)
            return {"ok": True, "camera": name, "already": True, "fast": True}
        point = self._button(cam)
        if not point:
            if cam.get("bounds"):
                x1, y1, x2, y2 = cam["bounds"]
                point = ((x1 + x2) // 2, y1 + max(80, (y2 - y1) // 3))
            else:
                raise AdbError(f"No play control found for {name!r}")
        self.log(f"fast tap {name} at {point[0]},{point[1]} (page {page})")
        self.tap(*point)
        self.mark_playing(name)
        return {"ok": True, "camera": name, "already": False, "fast": True}

    def play(self, name: str) -> dict[str, Any]:
        self.ensure_home()
        cam = self.find_camera(name)
        if cam is None:
            raise AdbError(f"Camera {name!r} is not visible in Imilab Home")
        if cam["playing"]:
            self.mark_playing(name)
            return {"ok": True, "camera": name, "already": True}
        point = self._button(cam)
        if not point:
            # Last resort: tap the middle of the video surface.
            if cam.get("bounds"):
                x1, y1, x2, y2 = cam["bounds"]
                point = ((x1 + x2) // 2, y1 + max(80, (y2 - y1) // 3))
            else:
                raise AdbError(f"No play control found for {name!r}")
        self.log(f"tapping {name} at {point[0]},{point[1]}")
        time.sleep(0.25)
        self.tap(*point)
        self.mark_playing(name)
        # Do not verify by dumping the UI here.  The server waits for a new
        # decoder instance, which is the real confirmation (and is much faster).
        return {"ok": True, "camera": name, "already": False}

    def pause_all(self) -> int:
        """Best-effort: pause every visible preview."""
        self.ensure_home()
        tapped = 0
        for _ in range(4):
            found = False
            for cam in self.visible_cameras():
                if not cam["playing"]:
                    continue
                point = self._button(cam)
                if point:
                    self.tap(*point)
                    tapped += 1
                    found = True
                    time.sleep(0.6)
            if not found:
                break
            self.scroll(up=True)
            time.sleep(0.6)
        self.mark_playing(None)
        return tapped
