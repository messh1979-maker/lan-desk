import socket
import struct
import time
import threading
import io
import os
import secrets
import sys

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


# --------------------- Config ---------------------
CONFIG_FILE = "proxy_config.txt"
DEFAULT_PROXY_IP = "192.168.160.149"
PROXY_PORT = 5000

# Image transfer tuning
QUALITY = 30
FPS = 15

# Lower values = faster, less sharp
# Higher values = sharper, heavier
MAX_FRAME_WIDTH = 1280
MAX_FRAME_HEIGHT = 720

MIN_QUALITY = 15
MAX_QUALITY = 45

# JPEG chroma subsampling:
# 2 = 4:2:0, smaller/faster, slightly lower color quality
JPEG_SUBSAMPLING = 2

# Mouse throttle: 0.016 ~= 60 events/sec
MOUSE_MOVE_MIN_INTERVAL = 0.016

RECONNECT_DELAY = 3

# Clipboard
CLIPBOARD_POLL_INTERVAL = 0.45
CLIPBOARD_IGNORE_WINDOW = 0.9

# Viewer display mode:
# "stretch" fills the whole window and usually feels more direct.
# "fit" keeps aspect ratio and may show black bars.
SCALE_MODE = "stretch"

# Safety limit for received packets
MAX_PAYLOAD = 64 * 1024 * 1024

# Socket buffers
SOCKET_BUF_SIZE = 4 * 1024 * 1024

# Remote-control tool must not be interrupted by PyAutoGUI corner failsafe.
# PAUSE=0 is very important for smooth mouse control.
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


try:
    FAST_RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    FAST_RESAMPLE = Image.BILINEAR


# --------------------- Key mapping ---------------------
_KEY_ALIASES = {
    # Enter / Return
    "enter": "enter",
    "return": "enter",
    "kp_enter": "enter",
    "linefeed": "enter",

    # Common
    "space": "space",
    "tab": "tab",
    "backspace": "backspace",
    "delete": "delete",
    "escape": "esc",
    "esc": "esc",

    # Arrows
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",

    # Modifiers
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

    # Lock / special
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

    # Navigation
    "home": "home",
    "end": "end",
    "page_up": "pageup",
    "page_down": "pagedown",
    "prior": "pageup",
    "next": "pagedown",
    "insert": "insert",

    # Function keys
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
    """
    Enable low-latency TCP settings:
    - TCP_NODELAY disables Nagle buffering
    - larger socket buffers help frame transfer
    """
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
    """Read exactly n bytes or return None if the connection closed early."""
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


def _normalize_key(name):
    if name is None:
        return None

    s = str(name).strip()
    if not s:
        return None

    low = s.lower()

    # Support pynput-style names like Key.enter
    if low.startswith("key."):
        low = low[4:]

    mapped = _KEY_ALIASES.get(low)
    if mapped is None:
        mapped = low

    if mapped in pyautogui.KEYBOARD_KEYS:
        return mapped

    if len(mapped) == 1:
        return mapped

    # Let pyautogui decide; exceptions are handled by caller.
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

        clip_lock = threading.Lock()
        clip_state = {
            "last": get_clipboard_safe(),
            "ignore_until": 0.0,
        }

        auto_shift_keys = set()

        def send_packet(cmd, payload=b""):
            try:
                packet = struct.pack("!BI", cmd, len(payload)) + payload
                with host_send_lock:
                    sock.sendall(packet)
                return True
            except Exception:
                stop_event.set()
                return False

        def scale_mouse_to_host(x, y):
            """
            The viewer may receive a downscaled frame, e.g. 1280x720,
            while the real host monitor is 1920x1080.

            Mouse coordinates coming from the viewer must be scaled back
            to the real host resolution.
            """
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

            with mss.mss() as sct:
                monitor = sct.monitors[1]

                monitor_w = monitor["width"]
                monitor_h = monitor["height"]

                with frame_size_lock:
                    frame_info["monitor_w"] = monitor_w
                    frame_info["monitor_h"] = monitor_h

                frame_interval = 1.0 / FPS

                while not stop_event.is_set():
                    start = time.monotonic()

                    try:
                        img = sct.grab(monitor)
                        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

                        ow, oh = img_pil.size

                        # Downscale before JPEG encoding.
                        # This is usually the biggest speed improvement.
                        scale = min(
                            1.0,
                            MAX_FRAME_WIDTH / float(ow),
                            MAX_FRAME_HEIGHT / float(oh),
                        )

                        if scale < 1.0:
                            target_w = max(1, int(ow * scale))
                            target_h = max(1, int(oh * scale))
                            img_pil = img_pil.resize((target_w, target_h), FAST_RESAMPLE)
                        else:
                            target_w, target_h = ow, oh

                        buffer = io.BytesIO()

                        # optimize=False is faster than optimize=True.
                        # subsampling=2 reduces JPEG size noticeably.
                        try:
                            img_pil.save(
                                buffer,
                                format="JPEG",
                                quality=int(quality),
                                optimize=False,
                                subsampling=JPEG_SUBSAMPLING,
                            )
                        except TypeError:
                            # Very old Pillow versions may not accept subsampling.
                            img_pil.save(
                                buffer,
                                format="JPEG",
                                quality=int(quality),
                                optimize=False,
                            )

                        jpeg_data = buffer.getvalue()

                        with frame_size_lock:
                            frame_info["sent_w"] = target_w
                            frame_info["sent_h"] = target_h

                        if not send_packet(CMD_FRAME, jpeg_data):
                            break

                        frame_count += 1
                        if frame_count % 60 == 0:
                            print(
                                f"[Host] Sent {frame_count} frames | "
                                f"quality={quality} | size={target_w}x{target_h}"
                            )

                        elapsed = time.monotonic() - start

                        # Adaptive quality:
                        # If frame processing/sending is slower than FPS target,
                        # reduce JPEG quality. If it is fast, slowly increase quality.
                        if elapsed > frame_interval:
                            quality = max(MIN_QUALITY, quality - 2)
                        elif elapsed < frame_interval * 0.55:
                            quality = min(MAX_QUALITY, quality + 1)

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
                        # Backward compatibility with old 1-byte click packet
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

                        with clip_lock:
                            clip_state["ignore_until"] = time.time() + CLIPBOARD_IGNORE_WINDOW
                            clip_state["last"] = text

                        set_clipboard_safe(text)

                except Exception as e:
                    print(f"[Host] Error in receive_inputs: {e}")
                    break

            stop_event.set()

        def sync_clipboard():
            while not stop_event.is_set():
                time.sleep(CLIPBOARD_POLL_INTERVAL)

                current = get_clipboard_safe()
                with clip_lock:
                    last = clip_state["last"]
                    ignore_until = clip_state["ignore_until"]

                if current != last:
                    with clip_lock:
                        clip_state["last"] = current

                    if time.time() >= ignore_until:
                        payload = current.encode("utf-8", errors="ignore")
                        if len(payload) <= MAX_PAYLOAD:
                            if not send_packet(CMD_CLIPBOARD, payload):
                                stop_event.set()
                                break

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

        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="none")

        self.sock = None
        self.sock_lock = threading.Lock()
        self.send_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.connected = False

        self._last_move_sent = 0.0
        self._last_sent_pos = None

        self._latest_img = None
        self._frame_update_pending = False

        self._canvas_image = None
        self._photo = None

        self.host_size = (1920, 1080)
        self.view_info = {
            "host_w": 1920,
            "host_h": 1080,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0,
            "offset_y": 0,
        }

        self.clip_lock = threading.Lock()
        self.clip_state = {
            "last": "",
            "ignore_until": 0.0,
        }

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

        if not self.status.winfo_ismapped():
            self.status.pack(pady=5)

        if not self.entry_frame.winfo_ismapped():
            self.entry_frame.pack(pady=10)

        self.status.config(text="Disconnected", fg="red")
        self.connect_btn.config(state=tk.NORMAL, text="Reconnect")

        self.code_entry.delete(0, tk.END)
        self.code_entry.focus()

        self.set_fullscreen(False)

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

            self.clip_state = {
                "last": get_clipboard_safe(),
                "ignore_until": 0.0,
            }

            # Switch to fullscreen viewer
            self.status.pack_forget()
            self.entry_frame.pack_forget()

            self.canvas.delete("all")
            self._canvas_image = None
            self._photo = None
            self.canvas.pack(fill=tk.BOTH, expand=True)

            self.set_fullscreen(True)
            self.canvas.focus_set()

            self._bind_controls()

            threading.Thread(target=self.receive_screen, daemon=True).start()
            threading.Thread(target=self.sync_clipboard, daemon=True).start()

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
        self.disconnect()
        self.root.destroy()

    # --------------------- Networking ---------------------
    def _send_packet(self, cmd, payload=b""):
        if not self.connected or self.stop_event.is_set():
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
                    self.host_size = img.size

                    # Keep only the newest frame.
                    # If UI is busy, old frames are dropped instead of queuing.
                    self._latest_img = img

                    if not self._frame_update_pending:
                        self._frame_update_pending = True
                        self.root.after(0, self._process_latest_frame)

                elif cmd == CMD_CLIPBOARD:
                    text = data.decode("utf-8", errors="replace")
                    self._apply_remote_clipboard(text)

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
            with self.clip_lock:
                last = self.clip_state["last"]
                ignore_until = self.clip_state["ignore_until"]

            if current != last:
                with self.clip_lock:
                    self.clip_state["last"] = current

                if time.time() >= ignore_until:
                    payload = current.encode("utf-8", errors="ignore")
                    if len(payload) <= MAX_PAYLOAD:
                        self._send_packet(CMD_CLIPBOARD, payload)

    def _apply_remote_clipboard(self, text):
        with self.clip_lock:
            self.clip_state["ignore_until"] = time.time() + CLIPBOARD_IGNORE_WINDOW
            self.clip_state["last"] = text

        set_clipboard_safe(text)

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

        # Windows/macOS wheel
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        # Linux wheel
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel_fixed(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel_fixed(e, -1))

        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)

    # --------------------- Mouse handlers ---------------------
    def _on_mouse_move(self, event):
        if not self.connected:
            return

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

        # Linux wheel events may appear as button 4/5; handle separately
        if event.num in (4, 5):
            return

        pos = self._map_to_host(event.x, event.y)
        if not pos:
            return

        # Tk: 1=left, 2=middle, 3=right
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
        # Tk modifier masks commonly: Control=0x0004, Alt=0x0008
        ctrl = bool(event.state & 0x0004)
        alt = bool(event.state & 0x0008)

        # Local disconnect hotkey
        if ctrl and alt and event.keysym.lower() == "q":
            # Try to release modifiers on host before closing
            self._send_packet(CMD_KEY_UP, b"ctrl")
            self._send_packet(CMD_KEY_UP, b"alt")
            self.disconnect()
            return True

        # Local fullscreen toggle
        if event.keysym == "F11":
            self.toggle_fullscreen()
            return True

        return False

    def _tk_key_to_name(self, event):
        keysym = event.keysym or ""
        if not keysym:
            return None

        # Modifiers should be sent as down/up
        if keysym in _MODIFIER_KEYSYMS:
            return keysym

        # Normal character keys
        if len(keysym) == 1:
            return keysym.lower()

        # Some symbols may come as keysym 'at', 'numbersign', etc.
        # Prefer actual typed character if printable.
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

        # For modifiers, use down/up so shortcuts like Ctrl+C work.
        if event.keysym in _MODIFIER_KEYSYMS:
            self._send_packet(CMD_KEY_DOWN, payload)
        else:
            # For normal keys, use press so OS auto-repeat still works.
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