import socket
import struct
import time
import threading
import io
import os
import secrets
import sys
import hashlib
import json
import tempfile
import itertools

import mss
from PIL import Image, ImageTk
import pyautogui
import tkinter as tk
import pyperclip


# Improve mouse coordinate accuracy on Windows when DPI scaling is enabled.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


# Optional Windows-only clipboard file support (drag/drop style file copy).
# Falls back to text-only clipboard sync when pywin32 isn't available or
# the platform isn't Windows.
CLIPBOARD_FILE_SUPPORT = False
if sys.platform == "win32":
    try:
        import win32clipboard
        import win32con
        CLIPBOARD_FILE_SUPPORT = True
    except Exception:
        CLIPBOARD_FILE_SUPPORT = False


# --------------------- Config ---------------------
CONFIG_FILE = "proxy_config.txt"
DEFAULT_PROXY_IP = "192.168.160.149"
PROXY_PORT = 5000


# --------------------- Bandwidth / quality adaptation ---------------------
# حداقل پهنای باند فرضی 512kbps است.
# اگر پهنای باند واقعی بیشتر باشد، کیفیت افزایش می‌یابد.
BW_PROBE_INTERVAL = 5.0
BW_INITIAL_PROBE_BYTES = 64 * 1024
BW_MIN_PROBE_BYTES = 16 * 1024
BW_MAX_PROBE_BYTES = 512 * 1024
BW_CHUNK_DATA_SIZE = 16 * 1024
BW_TARGET_DURATION = 1.0
BW_UTILIZATION = 0.75

BW_MIN_TARGET_BPS = 512_000
BW_MAX_TARGET_BPS = 20_000_000
BW_INITIAL_TARGET_BPS = 512_000
BW_PROBE_TIMEOUT = 10.0

MAX_PAYLOAD = 6 * 1024 * 1024

# Adaptive image limits (used only when UNLIMITED_BANDWIDTH_MODE is False)
QUALITY = 45
MIN_QUALITY = 18
MAX_QUALITY = 80

FPS = 5
MIN_FPS = 1
MAX_FPS = 12

MAX_FRAME_WIDTH = 1280
MAX_FRAME_HEIGHT = 800

# JPEG options
JPEG_SUBSAMPLING = 2
JPEG_OPTIMIZE = True

# --------------------- Unlimited bandwidth / best quality mode ---------------------
# When True, bandwidth probing/estimation is ignored for image quality
# purposes: the host always streams at native monitor resolution, top
# JPEG quality, and a fixed high frame rate. Intended for LAN links or
# any connection where bandwidth isn't a constraint. Set to False to
# restore the original adaptive (bandwidth-throttled) behavior.
UNLIMITED_BANDWIDTH_MODE = True
UNLIMITED_QUALITY = 95          # near-lossless JPEG quality
UNLIMITED_FPS = 24              # frame-rate ceiling when unlimited
UNLIMITED_JPEG_SUBSAMPLING = 0  # 4:4:4 - full chroma detail

# --------------------- Clipboard file transfer ---------------------
CLIPBOARD_FILE_CHUNK_SIZE = 256 * 1024
MAX_CLIPBOARD_FILE_TOTAL_SIZE = 200 * 1024 * 1024
MAX_CLIPBOARD_FILE_COUNT = 50

# Change detection
CHANGE_DETECTION = True
CHANGE_BLOCK_W = 96
CHANGE_BLOCK_H = 54
CHANGE_PIXEL_THRESHOLD = 30
KEYFRAME_INTERVAL = 6.0

# Cursor visibility
SHOW_REMOTE_CURSOR = True
CURSOR_MODE = "overlay"

MOUSE_MOVE_MIN_INTERVAL = 0.03
RECONNECT_DELAY = 3

# Clipboard
CLIPBOARD_POLL_INTERVAL = 0.45

# Viewer display
SCALE_MODE = "stretch"

SOCKET_BUF_SIZE = 512 * 1024

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0


# --------------------- Protocol ---------------------
CMD_FRAME = 0x01
CMD_MOUSE_MOVE = 0x02
CMD_MOUSE_BUTTON = 0x03
CMD_KEY_PRESS = 0x04
CMD_HOST_REGISTER = 0x05
CMD_VIEWER_CONNECT = 0x06
CMD_SCROLL = 0x07
CMD_KEY_DOWN = 0x08
CMD_KEY_UP = 0x09
CMD_CLIPBOARD = 0x0A

# Bandwidth probe commands
CMD_BW_REQ = 0x0B       # Viewer -> Host: request bandwidth probe
CMD_BW_START = 0x0C     # Host -> Viewer: probe start
CMD_BW_CHUNK = 0x0D     # Host -> Viewer: probe data chunk
CMD_BW_REPORT = 0x0E    # Viewer -> Host: measured bandwidth

# Clipboard file-copy commands (either direction)
CMD_CLIPBOARD_FILES_BEGIN = 0x0F  # manifest: {"transfer_id": int, "files":[{"name","size"}]}
CMD_CLIPBOARD_FILE_CHUNK = 0x10   # !IH header (transfer_id, file_index) + raw bytes
CMD_CLIPBOARD_FILES_END = 0x11    # !I (transfer_id)


try:
    FAST_RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    FAST_RESAMPLE = Image.BILINEAR

try:
    PROBE_CHUNK_DATA = os.urandom(BW_CHUNK_DATA_SIZE)
except Exception:
    PROBE_CHUNK_DATA = b"A" * BW_CHUNK_DATA_SIZE


# --------------------- Key mapping ---------------------
_KEY_ALIASES = {
    "enter": "enter",
    "return": "enter",
    "kp_enter": "enter",
    "linefeed": "enter",

    "space": "space",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "escape": "esc",
    "esc": "esc",

    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",

    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "control": "ctrl",
    "ctrl": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",
    "super": "win",
    "super_l": "win",
    "super_r": "win",
    "win": "win",
    "win_l": "win",
    "win_r": "win",
    "meta": "win",
    "meta_l": "win",
    "meta_r": "win",
    "cmd": "win",
    "command": "win",

    "caps_lock": "capslock",
    "capslock": "capslock",
    "num_lock": "numlock",
    "scroll_lock": "scrolllock",
    "print": "printscreen",
    "print_screen": "printscreen",
    "sys_req": "printscreen",
    "pause": "pause",
    "menu": "apps",
    "apps": "apps",

    "home": "home",
    "end": "end",
    "page_up": "pageup",
    "page_down": "pagedown",
    "prior": "pageup",
    "next": "pagedown",
    "insert": "insert",

    "f1": "f1",
    "f2": "f2",
    "f3": "f3",
    "f4": "f4",
    "f5": "f5",
    "f6": "f6",
    "f7": "f7",
    "f8": "f8",
    "f9": "f9",
    "f10": "f10",
    "f11": "f11",
    "f12": "f12",
}

_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R",
    "Control_L", "Control_R",
    "Alt_L", "Alt_R",
    "Super_L", "Super_R",
    "Meta_L", "Meta_R",
    "Win_L", "Win_R",
}

_MOUSE_BUTTONS = {
    0: "left",
    1: "right",
    2: "middle",
}


# --------------------- Helpers ---------------------
def set_sock_fast(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUF_SIZE)
    except Exception:
        pass

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUF_SIZE)
    except Exception:
        pass


def recv_exact(sock, n):
    if n == 0:
        return b""

    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:
            return None

        if not chunk:
            return None

        buf.extend(chunk)

    return bytes(buf)


def get_proxy_ip():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            ip = f.read().strip()
            if ip:
                return ip

    ip = input(f"Enter Proxy IP (default {DEFAULT_PROXY_IP}): ").strip()
    if not ip:
        ip = DEFAULT_PROXY_IP

    with open(CONFIG_FILE, "w") as f:
        f.write(ip)

    return ip


def generate_code():
    return str(secrets.randbelow(10 ** 6)).zfill(6)


def get_clipboard_safe():
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def set_clipboard_safe(text):
    try:
        pyperclip.copy(text)
    except Exception:
        pass


def clip_hash(text):
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


# --------------------- Clipboard file transfer helpers ---------------------
def get_clipboard_file_list():
    """Return a list of local file paths currently sitting on the OS
    clipboard (e.g. after Ctrl+C on files in Explorer), or None."""
    if not CLIPBOARD_FILE_SUPPORT:
        return None

    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return None

            paths = list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
            files = [p for p in paths if os.path.isfile(p)]
            return files or None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def set_clipboard_files_safe(file_paths):
    """Put files on the OS clipboard as a CF_HDROP list so the user can
    paste them (e.g. Ctrl+V in Explorer)."""
    if not CLIPBOARD_FILE_SUPPORT or not file_paths:
        return

    try:
        # DROPFILES header: pFiles, pt.x, pt.y, fNC, fWide
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        body = ("\0".join(file_paths) + "\0\0").encode("utf-16-le")
        data = header + body

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        print(f"[Clipboard] Failed to set file clipboard: {e}")


def clip_files_signature(name_size_pairs):
    """Order-independent signature used to detect clipboard echoes."""
    parts = sorted(f"{name}:{size}" for name, size in name_size_pairs)
    return hashlib.sha256("|".join(parts).encode("utf-8", "ignore")).hexdigest()


def _safe_filename(name):
    name = os.path.basename(str(name).replace("\\", "/"))
    name = name.strip().lstrip(".") or "file"
    return name


def send_clipboard_files(send_packet_fn, stop_event, file_paths, transfer_ids):
    """Stream local files to the remote side so it can place them on its
    clipboard. send_packet_fn(cmd, payload) -> bool (same signature the
    host/viewer already use for CMD_FRAME etc.)."""
    files = []
    total = 0

    for p in file_paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue

        files.append((p, size))
        total += size

    if not files:
        return

    if len(files) > MAX_CLIPBOARD_FILE_COUNT or total > MAX_CLIPBOARD_FILE_TOTAL_SIZE:
        print(
            f"[Clipboard] Skipping file transfer: too large "
            f"({total} bytes, {len(files)} files)"
        )
        return

    transfer_id = next(transfer_ids)

    manifest = {
        "transfer_id": transfer_id,
        "files": [{"name": _safe_filename(p), "size": s} for p, s in files],
    }

    if not send_packet_fn(CMD_CLIPBOARD_FILES_BEGIN, json.dumps(manifest).encode("utf-8")):
        return

    for idx, (path, _size) in enumerate(files):
        try:
            with open(path, "rb") as f:
                while True:
                    if stop_event.is_set():
                        return

                    chunk = f.read(CLIPBOARD_FILE_CHUNK_SIZE)
                    if not chunk:
                        break

                    header = struct.pack("!IH", transfer_id, idx)
                    if not send_packet_fn(CMD_CLIPBOARD_FILE_CHUNK, header + chunk):
                        return
        except OSError as e:
            print(f"[Clipboard] Failed to read {path}: {e}")
            return

    send_packet_fn(CMD_CLIPBOARD_FILES_END, struct.pack("!I", transfer_id))
    print(f"[Clipboard] Sent {len(files)} file(s), {total} bytes")


class ClipboardFileReceiver:
    """Reassembles an incoming clipboard file transfer into a temp dir."""

    def __init__(self):
        self.transfer_id = None
        self.tmp_dir = None
        self.files = []
        self.index = 0
        self.fh = None
        self.received = 0
        self.paths = []

    def begin(self, payload):
        self.close_current()

        try:
            manifest = json.loads(payload.decode("utf-8", errors="replace"))
        except Exception:
            return

        self.transfer_id = manifest.get("transfer_id")
        self.files = manifest.get("files", [])
        self.index = 0
        self.paths = []
        try:
            self.tmp_dir = tempfile.mkdtemp(prefix="remote_clip_")
        except OSError:
            self.transfer_id = None
            return

        self._open_next()

    def _open_next(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None

        if self.index >= len(self.files):
            return

        info = self.files[self.index]
        name = _safe_filename(info.get("name", "file"))
        path = os.path.join(self.tmp_dir, name)

        try:
            self.fh = open(path, "wb")
            self.received = 0
            self.paths.append(path)
        except OSError as e:
            print(f"[Clipboard] Failed to create {path}: {e}")
            self.fh = None

    def chunk(self, payload):
        if self.transfer_id is None or len(payload) < 6 or self.fh is None:
            return

        transfer_id, file_index = struct.unpack("!IH", payload[:6])
        if transfer_id != self.transfer_id or file_index != self.index:
            return

        data = payload[6:]

        try:
            self.fh.write(data)
        except OSError:
            return

        self.received += len(data)

        expected = self.files[self.index].get("size", 0)
        if self.received >= expected:
            self.index += 1
            self._open_next()

    def end(self, payload):
        if self.transfer_id is None:
            return None

        if len(payload) >= 4:
            transfer_id = struct.unpack("!I", payload[:4])[0]
            if transfer_id != self.transfer_id:
                return None

        self.close_current()

        paths = [p for p in self.paths if os.path.isfile(p)]
        self.transfer_id = None
        self.files = []
        self.paths = []
        return paths

    def close_current(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None


def _small_diff(a, b):
    if a is None or b is None or len(a) != len(b):
        return 999999

    if a == b:
        return 0

    diff = 0

    for x, y in zip(a, b):
        if abs(x - y) > 8:
            diff += 1

            if diff > CHANGE_PIXEL_THRESHOLD * 2:
                break

    return diff


def _normalize_key(name):
    if name is None:
        return None

    s = str(name).strip()
    if not s:
        return None

    low = s.lower()

    if low.startswith("key."):
        low = low[4:]

    mapped = _KEY_ALIASES.get(low)
    if mapped is None:
        mapped = low

    if mapped in pyautogui.KEYBOARD_KEYS:
        return mapped

    if len(mapped) == 1:
        return mapped

    return mapped


# ======================= Host mode =======================
def run_host(proxy_ip):
    print("\n[Host] Starting in Host mode...")

    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((proxy_ip, PROXY_PORT))
            sock.settimeout(None)
            set_sock_fast(sock)
            print(f"[Host] Connected to Proxy at {proxy_ip}:{PROXY_PORT}")
        except Exception as e:
            print(f"[Host] Cannot connect to Proxy: {e}")
            time.sleep(RECONNECT_DELAY)
            continue

        code = generate_code()
        print(f"[Host] Your connection code is: {code}")
        code_bytes = code.encode()

        try:
            sock.sendall(struct.pack("!BI", CMD_HOST_REGISTER, len(code_bytes)) + code_bytes)
            resp = sock.recv(1024)
        except Exception as e:
            print(f"[Host] Registration error: {e}")
            sock.close()
            time.sleep(RECONNECT_DELAY)
            continue

        if b"OK" not in resp:
            print(f"[Host] Registration failed: {resp!r}")
            sock.close()
            time.sleep(RECONNECT_DELAY)
            continue

        print("[Host] Registered. Waiting for viewer...")

        stop_event = threading.Event()
        host_send_lock = threading.Lock()

        frame_count = 0
        quality = QUALITY

        frame_size_lock = threading.Lock()
        frame_info = {
            "sent_w": 1920,
            "sent_h": 1080,
            "monitor_w": 1920,
            "monitor_h": 1080,
        }

        # Bandwidth adaptation state
        bw_ready = threading.Event()
        bw_lock = threading.Lock()
        bw_state = {
            "target_bps": BW_INITIAL_TARGET_BPS,
            "estimated_bps": None,
        }

        probe_lock = threading.Lock()
        probe_active = threading.Event()

        # Clipboard state with hash to prevent echo
        initial_clip = get_clipboard_safe()
        clip_lock = threading.Lock()
        clip_state = {
            "last": initial_clip,
            "last_hash": clip_hash(initial_clip),
            "remote_hash": None,
            "last_file_sig": None,
            "remote_file_sig": None,
        }

        clip_file_receiver = ClipboardFileReceiver()
        clip_file_transfer_ids = itertools.count(1)

        auto_shift_keys = set()

        def send_packet(cmd, payload=b""):
            if len(payload) > MAX_PAYLOAD:
                return False

            try:
                packet = struct.pack("!BI", cmd, len(payload)) + payload
                with host_send_lock:
                    sock.sendall(packet)
                return True
            except Exception:
                stop_event.set()
                return False

        def apply_bw_report(seq, measured_bps):
            with bw_lock:
                old = bw_state["estimated_bps"]

                if old is None:
                    estimated = int(measured_bps)
                else:
                    estimated = int(0.4 * old + 0.6 * measured_bps)

                bw_state["estimated_bps"] = estimated
                bw_state["target_bps"] = clamp(
                    int(estimated * BW_UTILIZATION),
                    BW_MIN_TARGET_BPS,
                    BW_MAX_TARGET_BPS,
                )

                target = bw_state["target_bps"]

            bw_ready.set()
            print(f"[Host] Bandwidth estimate: {estimated} bps | target bitrate: {target} bps")

        def send_bandwidth_probe(seq, requested_bytes):
            if not probe_lock.acquire(blocking=False):
                return

            def _probe():
                try:
                    probe_active.set()

                    total = clamp(
                        int(requested_bytes),
                        BW_MIN_PROBE_BYTES,
                        BW_MAX_PROBE_BYTES,
                    )

                    if not send_packet(CMD_BW_START, struct.pack("!II", seq, total)):
                        return

                    sent = 0
                    chunk = struct.pack("!I", seq) + PROBE_CHUNK_DATA

                    while sent < total and not stop_event.is_set():
                        if not send_packet(CMD_BW_CHUNK, chunk):
                            break
                        sent += len(chunk)

                finally:
                    probe_active.clear()
                    probe_lock.release()

            threading.Thread(target=_probe, daemon=True).start()

        def scale_mouse_to_host(x, y):
            with frame_size_lock:
                sent_w = frame_info["sent_w"]
                sent_h = frame_info["sent_h"]
                mon_w = frame_info["monitor_w"]
                mon_h = frame_info["monitor_h"]

            if sent_w <= 0 or sent_h <= 0:
                return x, y

            hx = int(x * mon_w / sent_w)
            hy = int(y * mon_h / sent_h)

            hx = max(0, min(mon_w - 1, hx))
            hy = max(0, min(mon_h - 1, hy))

            return hx, hy

        def host_key_press(name):
            key = _normalize_key(name)
            if not key:
                return

            try:
                if len(key) == 1 and key.isupper():
                    pyautogui.keyDown("shift")
                    pyautogui.press(key.lower())
                    pyautogui.keyUp("shift")
                else:
                    pyautogui.press(key)
            except Exception as e:
                print(f"[Host] key press failed: {key!r} -> {e}")

        def host_key_down(name):
            key = _normalize_key(name)
            if not key:
                return

            try:
                if len(key) == 1 and key.isupper():
                    lower = key.lower()
                    pyautogui.keyDown("shift")
                    pyautogui.keyDown(lower)
                    auto_shift_keys.add(lower)
                else:
                    pyautogui.keyDown(key)
            except Exception as e:
                print(f"[Host] key down failed: {key!r} -> {e}")

        def host_key_up(name):
            key = _normalize_key(name)
            if not key:
                return

            try:
                if len(key) == 1 and key.isupper():
                    lower = key.lower()
                    if lower in auto_shift_keys:
                        pyautogui.keyUp(lower)
                        pyautogui.keyUp("shift")
                        auto_shift_keys.discard(lower)
                    else:
                        pyautogui.keyUp(lower)
                else:
                    pyautogui.keyUp(key)
            except Exception as e:
                print(f"[Host] key up failed: {key!r} -> {e}")

        def send_screen():
            nonlocal frame_count, quality

            if UNLIMITED_BANDWIDTH_MODE:
                # No bandwidth throttling: stream at max quality, native
                # resolution and a fixed high fps right away.
                quality = UNLIMITED_QUALITY
                current_fps = UNLIMITED_FPS
                max_w = None
                max_h = None
            else:
                # Wait for first bandwidth report
                if not bw_ready.wait(timeout=8.0):
                    print("[Host] No bandwidth report yet; using initial bitrate")

                with bw_lock:
                    initial_target = bw_state["target_bps"]

                # Choose initial profile based on measured bandwidth
                if initial_target < 700_000:
                    quality = 30
                    current_fps = 3
                    max_w = 800
                    max_h = 600
                elif initial_target < 1_500_000:
                    quality = 45
                    current_fps = 5
                    max_w = 1024
                    max_h = 768
                else:
                    quality = QUALITY
                    current_fps = FPS
                    max_w = MAX_FRAME_WIDTH
                    max_h = MAX_FRAME_HEIGHT

            with mss.mss() as sct:
                monitor = sct.monitors[1]

                monitor_w = monitor["width"]
                monitor_h = monitor["height"]

                with frame_size_lock:
                    frame_info["monitor_w"] = monitor_w
                    frame_info["monitor_h"] = monitor_h

                if UNLIMITED_BANDWIDTH_MODE:
                    # Native resolution - no artificial downscale.
                    max_w = monitor_w
                    max_h = monitor_h

                last_small = None
                last_key_time = time.monotonic()

                while not stop_event.is_set():
                    # Pause normal frames while bandwidth probe is running
                    if probe_active.is_set():
                        time.sleep(0.05)
                        continue

                    frame_interval = 1.0 / max(0.1, current_fps)
                    start = time.monotonic()

                    try:
                        with bw_lock:
                            target_bps = bw_state["target_bps"]

                        img = sct.grab(monitor)
                        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

                        ow, oh = img_pil.size

                        scale = min(
                            1.0,
                            max_w / float(ow),
                            max_h / float(oh),
                        )

                        if scale < 1.0:
                            tw = max(1, int(ow * scale))
                            th = max(1, int(oh * scale))
                            img_pil = img_pil.resize((tw, th), FAST_RESAMPLE)
                        else:
                            tw, th = ow, oh

                        if CHANGE_DETECTION:
                            small_h = max(1, int(CHANGE_BLOCK_H * th / max(1, tw)))

                            small = img_pil.resize(
                                (CHANGE_BLOCK_W, small_h),
                                FAST_RESAMPLE
                            ).convert("L")

                            small_bytes = small.tobytes()
                            diff = _small_diff(last_small, small_bytes)

                            if (
                                diff < CHANGE_PIXEL_THRESHOLD
                                and (time.monotonic() - last_key_time) < KEYFRAME_INTERVAL
                            ):
                                elapsed = time.monotonic() - start
                                if elapsed < frame_interval:
                                    time.sleep(frame_interval - elapsed)
                                continue

                            last_small = small_bytes
                            last_key_time = time.monotonic()

                        # Use grayscale only when throttling for low bandwidth
                        if not UNLIMITED_BANDWIDTH_MODE and target_bps < 700_000:
                            img_pil = img_pil.convert("L")

                        buffer = io.BytesIO()

                        save_kwargs = {
                            "format": "JPEG",
                            "quality": int(quality),
                            "optimize": JPEG_OPTIMIZE,
                        }

                        if img_pil.mode == "RGB":
                            save_kwargs["subsampling"] = (
                                UNLIMITED_JPEG_SUBSAMPLING
                                if UNLIMITED_BANDWIDTH_MODE
                                else JPEG_SUBSAMPLING
                            )

                        try:
                            img_pil.save(buffer, **save_kwargs)
                        except TypeError:
                            img_pil.save(
                                buffer,
                                format="JPEG",
                                quality=int(quality),
                                optimize=JPEG_OPTIMIZE,
                            )

                        jpeg_data = buffer.getvalue()

                        with frame_size_lock:
                            frame_info["sent_w"] = tw
                            frame_info["sent_h"] = th

                        if not send_packet(CMD_FRAME, jpeg_data):
                            break

                        frame_count += 1

                        if frame_count % 30 == 0:
                            print(
                                f"[Host] frames={frame_count} | target={target_bps} bps | "
                                f"q={quality} | fps={current_fps} | size={tw}x{th} | "
                                f"bytes={len(jpeg_data)}"
                            )

                        if not UNLIMITED_BANDWIDTH_MODE:
                            budget = (target_bps / 8.0) / max(0.1, current_fps)
                            encoded_size = len(jpeg_data)

                            if encoded_size > budget * 1.15:
                                quality = max(MIN_QUALITY, quality - 3)

                                if quality <= MIN_QUALITY:
                                    max_w = max(320, int(max_w * 0.85))
                                    max_h = max(240, int(max_h * 0.85))

                                    if current_fps > MIN_FPS:
                                        current_fps = max(MIN_FPS, current_fps - 1)

                            elif encoded_size < budget * 0.60:
                                quality = min(MAX_QUALITY, quality + 1)

                                if quality >= MAX_QUALITY - 5:
                                    max_w = min(MAX_FRAME_WIDTH, int(max_w * 1.05))
                                    max_h = min(MAX_FRAME_HEIGHT, int(max_h * 1.05))

                                    if frame_count % 40 == 0 and current_fps < MAX_FPS:
                                        current_fps = min(MAX_FPS, current_fps + 1)

                        elapsed = time.monotonic() - start
                        sleep_time = frame_interval - elapsed

                        if sleep_time > 0:
                            time.sleep(sleep_time)

                    except Exception as e:
                        print(f"[Host] Error in send_screen: {e}")
                        stop_event.set()
                        break

        def receive_inputs():
            while not stop_event.is_set():
                try:
                    header = recv_exact(sock, 5)
                    if header is None:
                        print("[Host] Connection closed (receive_inputs)")
                        break

                    cmd, length = struct.unpack("!BI", header)

                    if length > MAX_PAYLOAD:
                        print("[Host] Payload too large")
                        break

                    data = recv_exact(sock, length)
                    if data is None:
                        print("[Host] Incomplete input packet")
                        break

                    # Mouse move
                    if cmd == CMD_MOUSE_MOVE:
                        if len(data) >= 8:
                            x, y = struct.unpack("!II", data[:8])
                            x, y = scale_mouse_to_host(x, y)

                            try:
                                pyautogui.moveTo(x, y)
                            except Exception:
                                pass

                    # Mouse button down/up
                    elif cmd == CMD_MOUSE_BUTTON:
                        if len(data) == 1:
                            btn = data[0]
                            try:
                                pyautogui.click(button="left" if btn == 0 else "right")
                            except Exception:
                                pass

                        elif len(data) >= 10:
                            btn, pressed, x, y = struct.unpack("!BBII", data[:10])
                            button = _MOUSE_BUTTONS.get(btn, "left")

                            x, y = scale_mouse_to_host(x, y)

                            try:
                                pyautogui.moveTo(x, y)

                                if pressed:
                                    pyautogui.mouseDown(button=button)
                                else:
                                    pyautogui.mouseUp(button=button)
                            except Exception:
                                pass

                    # Scroll
                    elif cmd == CMD_SCROLL:
                        try:
                            if len(data) >= 8:
                                dy, dx = struct.unpack("!ii", data[:8])
                            elif len(data) >= 4:
                                dy = struct.unpack("!i", data[:4])[0]
                                dx = 0
                            else:
                                continue

                            if dy:
                                pyautogui.scroll(dy)

                            if dx and hasattr(pyautogui, "hscroll"):
                                pyautogui.hscroll(dx)
                        except Exception:
                            pass

                    # Legacy single key press
                    elif cmd == CMD_KEY_PRESS:
                        key_name = data.decode(errors="replace")
                        host_key_press(key_name)

                    # Key down / up for modifiers and combos
                    elif cmd == CMD_KEY_DOWN:
                        key_name = data.decode(errors="replace")
                        host_key_down(key_name)

                    elif cmd == CMD_KEY_UP:
                        key_name = data.decode(errors="replace")
                        host_key_up(key_name)

                    # Clipboard from viewer
                    elif cmd == CMD_CLIPBOARD:
                        text = data.decode("utf-8", errors="replace")
                        h = clip_hash(text)

                        with clip_lock:
                            clip_state["remote_hash"] = h
                            clip_state["last_hash"] = h
                            clip_state["last"] = text

                        set_clipboard_safe(text)

                    # Clipboard file transfer from viewer
                    elif cmd == CMD_CLIPBOARD_FILES_BEGIN:
                        clip_file_receiver.begin(data)

                    elif cmd == CMD_CLIPBOARD_FILE_CHUNK:
                        clip_file_receiver.chunk(data)

                    elif cmd == CMD_CLIPBOARD_FILES_END:
                        paths = clip_file_receiver.end(data)
                        if paths:
                            sig = clip_files_signature(
                                (os.path.basename(p), os.path.getsize(p)) for p in paths
                            )
                            with clip_lock:
                                clip_state["remote_file_sig"] = sig
                                clip_state["last_file_sig"] = sig

                            set_clipboard_files_safe(paths)
                            print(f"[Host] Received {len(paths)} file(s) via clipboard")

                    # Bandwidth probe request from viewer
                    elif cmd == CMD_BW_REQ:
                        if len(data) >= 8:
                            seq, requested_bytes = struct.unpack("!II", data[:8])
                            send_bandwidth_probe(seq, requested_bytes)

                    # Bandwidth measurement report from viewer
                    elif cmd == CMD_BW_REPORT:
                        if len(data) >= 8:
                            seq, measured_bps = struct.unpack("!II", data[:8])
                            apply_bw_report(seq, measured_bps)

                except Exception as e:
                    print(f"[Host] Error in receive_inputs: {e}")
                    break

            stop_event.set()

        def sync_clipboard():
            while not stop_event.is_set():
                time.sleep(CLIPBOARD_POLL_INTERVAL)

                current = get_clipboard_safe()
                h = clip_hash(current)

                with clip_lock:
                    last_hash = clip_state["last_hash"]
                    remote_hash = clip_state["remote_hash"]

                if h != last_hash and h != remote_hash:
                    with clip_lock:
                        clip_state["last_hash"] = h
                        clip_state["last"] = current

                    payload = current.encode("utf-8", errors="ignore")
                    if len(payload) <= MAX_PAYLOAD:
                        if not send_packet(CMD_CLIPBOARD, payload):
                            stop_event.set()
                            break

                if CLIPBOARD_FILE_SUPPORT:
                    files = get_clipboard_file_list()
                    if files:
                        sig = clip_files_signature(
                            (os.path.basename(p), os.path.getsize(p)) for p in files
                        )

                        with clip_lock:
                            last_file_sig = clip_state["last_file_sig"]
                            remote_file_sig = clip_state["remote_file_sig"]

                        if sig != last_file_sig and sig != remote_file_sig:
                            with clip_lock:
                                clip_state["last_file_sig"] = sig

                            send_clipboard_files(
                                send_packet, stop_event, files, clip_file_transfer_ids
                            )

        t_send = threading.Thread(target=send_screen, daemon=True)
        t_recv = threading.Thread(target=receive_inputs, daemon=True)
        t_clip = threading.Thread(target=sync_clipboard, daemon=True)

        t_send.start()
        t_recv.start()
        t_clip.start()

        t_send.join()
        t_recv.join()
        t_clip.join()

        # Try to avoid stuck modifiers/buttons after disconnect
        try:
            for k in ("shift", "ctrl", "alt", "win"):
                pyautogui.keyUp(k)
        except Exception:
            pass

        try:
            for b in ("left", "right", "middle"):
                pyautogui.mouseUp(button=b)
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass

        print(f"[Host] Connection closed. Reconnecting in {RECONNECT_DELAY} seconds...")
        time.sleep(RECONNECT_DELAY)


# ======================= Viewer mode =======================
class ViewerApp:
    def __init__(self, root, proxy_ip):
        self.root = root
        self.proxy_ip = proxy_ip

        self.root.title("Remote Viewer")
        self.root.geometry("850x700")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.status = tk.Label(
            root,
            text=(
                "Enter 6-digit code and click Connect\n"
                "After connect: F11 fullscreen, Ctrl+Alt+Q disconnect"
            ),
            font=("Arial", 11),
        )
        self.status.pack(pady=5)

        self.entry_frame = tk.Frame(root)
        self.entry_frame.pack(pady=10)

        self.code_entry = tk.Entry(self.entry_frame, font=("Arial", 20), width=10)
        self.code_entry.pack(side=tk.LEFT, padx=5)
        self.code_entry.focus()

        self.connect_btn = tk.Button(
            self.entry_frame,
            text="Connect",
            command=self.connect_to_host,
            font=("Arial", 14),
        )
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0)

        self.sock = None
        self.sock_lock = threading.Lock()
        self.send_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.connected = False

        self.session_id = 0

        self._last_move_sent = 0.0
        self._last_sent_pos = None

        self._latest_img = None
        self._frame_update_pending = False

        self._canvas_image = None
        self._photo = None
        self._cursor_items = []

        self.host_size = (1920, 1080)
        self.view_info = {
            "host_w": 1920,
            "host_h": 1080,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0,
            "offset_y": 0,
        }

        # Bandwidth measurement state
        self.bw_seq = 0
        self.bw_lock = threading.Lock()
        self.bw_active = {
            "seq": None,
            "expected": 0,
            "received": 0,
            "start": None,
        }
        self.last_bps = 0
        self.bw_thread = None

        # Clipboard state with hash to prevent echo
        initial_clip = get_clipboard_safe()
        self.clip_lock = threading.Lock()
        self.clip_state = {
            "last": initial_clip,
            "last_hash": clip_hash(initial_clip),
            "remote_hash": None,
            "last_file_sig": None,
            "remote_file_sig": None,
        }
        self.clip_file_receiver = ClipboardFileReceiver()
        self.clip_file_transfer_ids = itertools.count(1)

        self.is_fullscreen = False

    # --------------------- UI helpers ---------------------
    def set_fullscreen(self, value):
        self.is_fullscreen = bool(value)
        self.root.attributes("-fullscreen", self.is_fullscreen)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.is_fullscreen)

    def _restore_ui(self):
        try:
            self.canvas.pack_forget()
        except Exception:
            pass

        self._cursor_items = []

        try:
            if not self.status.winfo_ismapped():
                self.status.pack(pady=5)

            if not self.entry_frame.winfo_ismapped():
                self.entry_frame.pack(pady=10)

            self.status.config(text="Disconnected", fg="red")
            self.connect_btn.config(state=tk.NORMAL, text="Reconnect")

            self.code_entry.delete(0, tk.END)
            self.code_entry.focus()

            self.set_fullscreen(False)
        except Exception:
            pass

    def _setup_cursor_mode(self):
        try:
            if CURSOR_MODE == "system":
                self.canvas.config(cursor="arrow")
            elif CURSOR_MODE == "overlay":
                self.canvas.config(cursor="none")
            else:
                self.canvas.config(cursor="none")
        except Exception:
            pass

    def _update_cursor_overlay(self, wx, wy):
        try:
            if not SHOW_REMOTE_CURSOR or CURSOR_MODE != "overlay":
                return

            if not self._cursor_items:
                outer = self.canvas.create_oval(
                    wx - 9, wy - 9,
                    wx + 9, wy + 9,
                    outline="white",
                    width=2
                )

                inner = self.canvas.create_oval(
                    wx - 5, wy - 5,
                    wx + 5, wy + 5,
                    outline="red",
                    width=2
                )

                self._cursor_items = [outer, inner]
            else:
                outer, inner = self._cursor_items

                self.canvas.coords(
                    outer,
                    wx - 9, wy - 9,
                    wx + 9, wy + 9
                )

                self.canvas.coords(
                    inner,
                    wx - 5, wy - 5,
                    wx + 5, wy + 5
                )

            for item in self._cursor_items:
                self.canvas.tag_raise(item)
        except Exception:
            pass

    def _raise_cursor(self):
        try:
            for item in self._cursor_items:
                self.canvas.tag_raise(item)
        except Exception:
            pass

    # --------------------- Connection ---------------------
    def connect_to_host(self):
        code = self.code_entry.get().strip()
        if len(code) != 6 or not code.isdigit():
            self.status.config(text="Code must be 6 digits", fg="red")
            return

        self.status.config(text="Connecting...", fg="blue")
        self.connect_btn.config(state=tk.DISABLED)

        self.stop_event.clear()
        self.connected = False

        try:
            with self.sock_lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

                new_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                new_sock.settimeout(10)
                new_sock.connect((self.proxy_ip, PROXY_PORT))
                new_sock.settimeout(None)
                set_sock_fast(new_sock)
                self.sock = new_sock

            print(f"[Viewer] Connected to Proxy at {self.proxy_ip}:{PROXY_PORT}")

            code_bytes = code.encode()
            packet = struct.pack("!BI", CMD_VIEWER_CONNECT, len(code_bytes)) + code_bytes

            with self.send_lock:
                self.sock.sendall(packet)

            resp = self.sock.recv(1024)
            if b"OK" not in resp:
                self.status.config(text=resp.decode(errors="replace"), fg="red")

                with self.sock_lock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

                self.connect_btn.config(state=tk.NORMAL)
                return

            self.connected = True
            self.stop_event.clear()

            initial_clip = get_clipboard_safe()
            self.clip_state = {
                "last": initial_clip,
                "last_hash": clip_hash(initial_clip),
                "remote_hash": None,
                "last_file_sig": None,
                "remote_file_sig": None,
            }
            self.clip_file_receiver = ClipboardFileReceiver()
            self.clip_file_transfer_ids = itertools.count(1)

            # Switch to fullscreen viewer
            self.status.pack_forget()
            self.entry_frame.pack_forget()

            self.canvas.delete("all")
            self._canvas_image = None
            self._photo = None
            self._cursor_items = []
            self._latest_img = None
            self._frame_update_pending = False
            self._last_sent_pos = None

            self.canvas.pack(fill=tk.BOTH, expand=True)

            self.set_fullscreen(True)
            self.canvas.focus_set()

            self._setup_cursor_mode()
            self._bind_controls()

            threading.Thread(target=self.receive_screen, daemon=True).start()
            threading.Thread(target=self.sync_clipboard, daemon=True).start()

            # Start bandwidth monitor
            self.session_id += 1
            self.bw_thread = threading.Thread(
                target=self.bandwidth_monitor,
                args=(self.session_id,),
                daemon=True,
            )
            self.bw_thread.start()

        except socket.timeout:
            self.status.config(text="Connection timeout", fg="red")
            self.connect_btn.config(state=tk.NORMAL)

            with self.sock_lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

        except Exception as e:
            self.status.config(text=f"Connection error: {e}", fg="red")
            self.connect_btn.config(state=tk.NORMAL)

            with self.sock_lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

    def disconnect(self):
        if not self.connected:
            return

        self.connected = False
        self.stop_event.set()

        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

        self._restore_ui()

    def _handle_remote_disconnect(self):
        if not self.connected:
            return

        self.connected = False
        self.stop_event.set()

        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

        self._restore_ui()

    def on_closing(self):
        try:
            self.disconnect()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass

    # --------------------- Networking ---------------------
    def _send_packet(self, cmd, payload=b""):
        if not self.connected or self.stop_event.is_set():
            return False

        if len(payload) > MAX_PAYLOAD:
            return False

        with self.sock_lock:
            sock = self.sock

        if sock is None:
            return False

        try:
            packet = struct.pack("!BI", cmd, len(payload)) + payload
            with self.send_lock:
                sock.sendall(packet)
            return True
        except Exception:
            self.stop_event.set()
            try:
                self.root.after(0, self._handle_remote_disconnect)
            except Exception:
                pass
            return False

    def bandwidth_monitor(self, session_id):
        if UNLIMITED_BANDWIDTH_MODE:
            # Quality isn't throttled by measured bandwidth in this mode,
            # so there's no point spending bandwidth on probes.
            return

        time.sleep(0.5)

        while not self.stop_event.is_set() and session_id == self.session_id:
            with self.bw_lock:
                active_seq = self.bw_active["seq"]
                start = self.bw_active["start"]
                last = self.last_bps

            if active_seq is not None:
                if start is not None and time.monotonic() - start > BW_PROBE_TIMEOUT:
                    with self.bw_lock:
                        self.bw_active.update({
                            "seq": None,
                            "expected": 0,
                            "received": 0,
                            "start": None,
                        })
                else:
                    time.sleep(0.5)
                    continue

            if last > 0:
                probe_bytes = int((last / 8.0) * BW_TARGET_DURATION)
            else:
                probe_bytes = BW_INITIAL_PROBE_BYTES

            probe_bytes = clamp(
                probe_bytes,
                BW_MIN_PROBE_BYTES,
                BW_MAX_PROBE_BYTES,
            )

            self.bw_seq = (self.bw_seq + 1) & 0x7FFFFFFF
            seq = self.bw_seq

            if not self._send_packet(CMD_BW_REQ, struct.pack("!II", seq, probe_bytes)):
                break

            time.sleep(BW_PROBE_INTERVAL)

    def receive_screen(self):
        with self.sock_lock:
            sock = self.sock

        while not self.stop_event.is_set():
            try:
                if sock is None:
                    break

                header = recv_exact(sock, 5)
                if header is None:
                    print("[Viewer] Connection closed")
                    break

                cmd, length = struct.unpack("!BI", header)

                if length > MAX_PAYLOAD:
                    print("[Viewer] Payload too large")
                    break

                data = recv_exact(sock, length)
                if data is None:
                    print("[Viewer] Incomplete packet")
                    break

                if cmd == CMD_FRAME:
                    img = Image.open(io.BytesIO(data))
                    img.load()

                    self.host_size = img.size

                    self._latest_img = img

                    if not self._frame_update_pending:
                        self._frame_update_pending = True
                        self.root.after(0, self._process_latest_frame)

                elif cmd == CMD_CLIPBOARD:
                    text = data.decode("utf-8", errors="replace")
                    self._apply_remote_clipboard(text)

                elif cmd == CMD_CLIPBOARD_FILES_BEGIN:
                    self.clip_file_receiver.begin(data)

                elif cmd == CMD_CLIPBOARD_FILE_CHUNK:
                    self.clip_file_receiver.chunk(data)

                elif cmd == CMD_CLIPBOARD_FILES_END:
                    paths = self.clip_file_receiver.end(data)
                    if paths:
                        self._apply_remote_files(paths)

                # Bandwidth probe start
                elif cmd == CMD_BW_START:
                    if len(data) >= 8:
                        seq, total = struct.unpack("!II", data[:8])

                        with self.bw_lock:
                            self.bw_active.update({
                                "seq": seq,
                                "expected": total,
                                "received": 0,
                                "start": time.monotonic(),
                            })

                # Bandwidth probe chunk
                elif cmd == CMD_BW_CHUNK:
                    if len(data) >= 4:
                        seq = struct.unpack("!I", data[:4])[0]

                        report_seq = None
                        report_bps = 0

                        with self.bw_lock:
                            if self.bw_active["seq"] == seq:
                                self.bw_active["received"] += length

                                if self.bw_active["received"] >= self.bw_active["expected"]:
                                    duration = time.monotonic() - self.bw_active["start"]
                                    duration = max(duration, 0.02)

                                    report_bps = int((self.bw_active["received"] * 8) / duration)
                                    report_seq = seq

                                    self.last_bps = report_bps

                                    self.bw_active.update({
                                        "seq": None,
                                        "expected": 0,
                                        "received": 0,
                                        "start": None,
                                    })

                        if report_seq is not None:
                            print(f"[Viewer] Measured bandwidth: {report_bps} bps")
                            self._send_packet(
                                CMD_BW_REPORT,
                                struct.pack("!II", report_seq, report_bps),
                            )

            except Exception as e:
                print(f"[Viewer] Error in receive_screen: {e}")
                break

        try:
            self.root.after(0, self._handle_remote_disconnect)
        except Exception:
            pass

    # --------------------- Clipboard ---------------------
    def sync_clipboard(self):
        while not self.stop_event.is_set():
            time.sleep(CLIPBOARD_POLL_INTERVAL)

            current = get_clipboard_safe()
            h = clip_hash(current)

            with self.clip_lock:
                last_hash = self.clip_state["last_hash"]
                remote_hash = self.clip_state["remote_hash"]

            if h != last_hash and h != remote_hash:
                with self.clip_lock:
                    self.clip_state["last_hash"] = h
                    self.clip_state["last"] = current

                payload = current.encode("utf-8", errors="ignore")
                if len(payload) <= MAX_PAYLOAD:
                    self._send_packet(CMD_CLIPBOARD, payload)

            if CLIPBOARD_FILE_SUPPORT:
                files = get_clipboard_file_list()
                if files:
                    sig = clip_files_signature(
                        (os.path.basename(p), os.path.getsize(p)) for p in files
                    )

                    with self.clip_lock:
                        last_file_sig = self.clip_state["last_file_sig"]
                        remote_file_sig = self.clip_state["remote_file_sig"]

                    if sig != last_file_sig and sig != remote_file_sig:
                        with self.clip_lock:
                            self.clip_state["last_file_sig"] = sig

                        send_clipboard_files(
                            self._send_packet,
                            self.stop_event,
                            files,
                            self.clip_file_transfer_ids,
                        )

    def _apply_remote_clipboard(self, text):
        h = clip_hash(text)

        with self.clip_lock:
            self.clip_state["remote_hash"] = h
            self.clip_state["last_hash"] = h
            self.clip_state["last"] = text

        set_clipboard_safe(text)

    def _apply_remote_files(self, paths):
        sig = clip_files_signature(
            (os.path.basename(p), os.path.getsize(p)) for p in paths
        )

        with self.clip_lock:
            self.clip_state["remote_file_sig"] = sig
            self.clip_state["last_file_sig"] = sig

        set_clipboard_files_safe(paths)
        print(f"[Viewer] Received {len(paths)} file(s) via clipboard")

    # --------------------- Rendering ---------------------
    def _process_latest_frame(self):
        self._frame_update_pending = False

        img = self._latest_img
        self._latest_img = None

        if img is not None:
            self._display_frame(img)

    def _display_frame(self, img):
        if not self.connected:
            return

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()

        if cw <= 1 or ch <= 1:
            self.canvas.update_idletasks()
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()

        if cw <= 1 or ch <= 1:
            return

        host_w, host_h = img.size

        if SCALE_MODE == "stretch":
            target_w, target_h = cw, ch
            offset_x, offset_y = 0, 0
            scale_x = cw / float(host_w)
            scale_y = ch / float(host_h)
        else:
            scale = min(cw / float(host_w), ch / float(host_h))
            target_w = max(1, int(host_w * scale))
            target_h = max(1, int(host_h * scale))
            offset_x = (cw - target_w) // 2
            offset_y = (ch - target_h) // 2
            scale_x = scale_y = scale

        if (target_w, target_h) != img.size:
            img = img.resize((target_w, target_h), FAST_RESAMPLE)

        self._photo = ImageTk.PhotoImage(image=img)

        if self._canvas_image is None:
            self._canvas_image = self.canvas.create_image(
                offset_x, offset_y,
                anchor=tk.NW,
                image=self._photo
            )
        else:
            self.canvas.coords(self._canvas_image, offset_x, offset_y)
            self.canvas.itemconfig(self._canvas_image, image=self._photo)

        self.view_info = {
            "host_w": host_w,
            "host_h": host_h,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "offset_x": offset_x,
            "offset_y": offset_y,
        }

        self._raise_cursor()

    def _map_to_host(self, x, y):
        vi = self.view_info

        if vi["scale_x"] <= 0 or vi["scale_y"] <= 0:
            return None

        hx = (x - vi["offset_x"]) / vi["scale_x"]
        hy = (y - vi["offset_y"]) / vi["scale_y"]

        hx = max(0, min(vi["host_w"] - 1, int(hx)))
        hy = max(0, min(vi["host_h"] - 1, int(hy)))

        return hx, hy

    # --------------------- Input bindings ---------------------
    def _bind_controls(self):
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<ButtonPress>", self._on_button_press)
        self.canvas.bind("<ButtonRelease>", self._on_button_release)

        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        self.canvas.bind("<Button-4>", lambda e: self._on_wheel_fixed(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel_fixed(e, -1))

        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)

    # --------------------- Mouse handlers ---------------------
    def _on_mouse_move(self, event):
        if not self.connected:
            return

        self._update_cursor_overlay(event.x, event.y)

        now = time.monotonic()
        if now - self._last_move_sent < MOUSE_MOVE_MIN_INTERVAL:
            return

        pos = self._map_to_host(event.x, event.y)
        if not pos:
            return

        if pos == self._last_sent_pos:
            return

        self._last_sent_pos = pos
        self._last_move_sent = now

        self._send_packet(CMD_MOUSE_MOVE, struct.pack("!II", pos[0], pos[1]))

    def _on_button_press(self, event):
        if not self.connected:
            return

        self._update_cursor_overlay(event.x, event.y)

        if event.num in (4, 5):
            return

        pos = self._map_to_host(event.x, event.y)
        if not pos:
            return

        btn_map = {1: 0, 2: 2, 3: 1}
        btn = btn_map.get(event.num)
        if btn is None:
            return

        self._send_packet(
            CMD_MOUSE_BUTTON,
            struct.pack("!BBII", btn, 1, pos[0], pos[1])
        )

    def _on_button_release(self, event):
        if not self.connected:
            return

        self._update_cursor_overlay(event.x, event.y)

        if event.num in (4, 5):
            return

        pos = self._map_to_host(event.x, event.y)
        if not pos:
            return

        btn_map = {1: 0, 2: 2, 3: 1}
        btn = btn_map.get(event.num)
        if btn is None:
            return

        self._send_packet(
            CMD_MOUSE_BUTTON,
            struct.pack("!BBII", btn, 0, pos[0], pos[1])
        )

    def _on_mouse_wheel(self, event):
        if not self.connected:
            return

        if event.delta == 0:
            return

        dy = event.delta // 120
        if dy == 0:
            dy = 1 if event.delta > 0 else -1

        self._send_packet(CMD_SCROLL, struct.pack("!ii", dy, 0))

    def _on_wheel_fixed(self, event, dy):
        if not self.connected:
            return

        self._send_packet(CMD_SCROLL, struct.pack("!ii", dy, 0))

    # --------------------- Keyboard handlers ---------------------
    def _is_local_hotkey(self, event):
        ctrl = bool(event.state & 0x0004)
        alt = bool(event.state & 0x0008)

        if ctrl and alt and event.keysym.lower() == "q":
            self._send_packet(CMD_KEY_UP, b"ctrl")
            self._send_packet(CMD_KEY_UP, b"alt")
            self.disconnect()
            return True

        if event.keysym == "F11":
            self.toggle_fullscreen()
            return True

        return False

    def _tk_key_to_name(self, event):
        keysym = event.keysym or ""
        if not keysym:
            return None

        if keysym in _MODIFIER_KEYSYMS:
            return keysym

        if len(keysym) == 1:
            return keysym.lower()

        if event.char and event.char.isprintable() and event.char != "":
            return event.char

        return keysym

    def _on_key_press(self, event):
        if not self.connected:
            return

        if self._is_local_hotkey(event):
            return

        name = self._tk_key_to_name(event)
        if not name:
            return

        payload = name.encode("utf-8", errors="ignore")

        if event.keysym in _MODIFIER_KEYSYMS:
            self._send_packet(CMD_KEY_DOWN, payload)
        else:
            self._send_packet(CMD_KEY_PRESS, payload)

    def _on_key_release(self, event):
        if not self.connected:
            return

        if event.keysym in _MODIFIER_KEYSYMS:
            name = self._tk_key_to_name(event)
            if not name:
                return

            payload = name.encode("utf-8", errors="ignore")
            self._send_packet(CMD_KEY_UP, payload)


def run_viewer(proxy_ip):
    print("[Viewer] Starting in Viewer mode...")
    root = tk.Tk()
    ViewerApp(root, proxy_ip)
    root.mainloop()


# ======================= Entry point =======================
def main():
    print("\n" + "=" * 50)
    print("     REMOTE DESKTOP APP")
    print("=" * 50)
    print("1. Run as Host (share your screen)")
    print("2. Run as Viewer (watch/control another screen)")

    choice = input("Select role (1 or 2): ").strip()
    if choice not in ("1", "2"):
        print("Invalid choice. Exiting.")
        return

    proxy_ip = get_proxy_ip()
    print(f"Using Proxy at {proxy_ip}:{PROXY_PORT}")

    if choice == "1":
        run_host(proxy_ip)
    else:
        run_viewer(proxy_ip)


if __name__ == "__main__":
    main()