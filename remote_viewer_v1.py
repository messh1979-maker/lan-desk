#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remote_viewer_final.py

Remote Desktop - Viewer Only
Target OS   : Windows
Proxy IP    : 192.168.160.149
Proxy Port  : 5000

This file runs only as VIEWER.
It does NOT ask proxy IP from user.
"""

import socket
import struct
import time
import threading
import io
import os
import sys
import hashlib
import json
import tempfile
import itertools

from PIL import Image, ImageTk
import tkinter as tk
import pyperclip


# Improve DPI awareness for accurate mouse coordinates on Windows.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


# Optional Windows-only clipboard file support.
CLIPBOARD_FILE_SUPPORT = False
if sys.platform == "win32":
    try:
        import win32clipboard
        import win32con
        CLIPBOARD_FILE_SUPPORT = True
    except Exception:
        CLIPBOARD_FILE_SUPPORT = False


# --------------------- Fixed Proxy Config ---------------------
PROXY_IP = "192.168.160.149"
PROXY_PORT = 5000


# --------------------- Bandwidth / quality adaptation ---------------------
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


# --------------------- Unlimited bandwidth / best quality mode ---------------------
UNLIMITED_BANDWIDTH_MODE = True


# --------------------- Clipboard file transfer ---------------------
CLIPBOARD_FILE_CHUNK_SIZE = 256 * 1024
MAX_CLIPBOARD_FILE_TOTAL_SIZE = 200 * 1024 * 1024
MAX_CLIPBOARD_FILE_COUNT = 50


# --------------------- Cursor / UI ---------------------
SHOW_REMOTE_CURSOR = True
CURSOR_MODE = "overlay"
MOUSE_MOVE_MIN_INTERVAL = 0.03
CLIPBOARD_POLL_INTERVAL = 0.45
SCALE_MODE = "stretch"
SOCKET_BUF_SIZE = 512 * 1024


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

CMD_BW_REQ = 0x0B
CMD_BW_START = 0x0C
CMD_BW_CHUNK = 0x0D
CMD_BW_REPORT = 0x0E

CMD_CLIPBOARD_FILES_BEGIN = 0x0F
CMD_CLIPBOARD_FILE_CHUNK = 0x10
CMD_CLIPBOARD_FILES_END = 0x11


try:
    FAST_RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    FAST_RESAMPLE = Image.BILINEAR


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
    if not CLIPBOARD_FILE_SUPPORT or not file_paths:
        return
    try:
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
    parts = sorted(f"{name}:{size}" for name, size in name_size_pairs)
    return hashlib.sha256("|".join(parts).encode("utf-8", "ignore")).hexdigest()


def _safe_filename(name):
    name = os.path.basename(str(name).replace("\\", "/"))
    name = name.strip().lstrip(".") or "file"
    return name


def send_clipboard_files(send_packet_fn, stop_event, file_paths, transfer_ids):
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


# ======================= Viewer mode =======================
class ViewerApp:
    def __init__(self, root):
        self.root = root
        self.proxy_ip = PROXY_IP

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

        modifier_keys = {
            "Shift_L", "Shift_R",
            "Control_L", "Control_R",
            "Alt_L", "Alt_R",
            "Super_L", "Super_R",
            "Meta_L", "Meta_R",
            "Win_L", "Win_R",
        }

        if keysym in modifier_keys:
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

        modifier_keys = {
            "Shift_L", "Shift_R",
            "Control_L", "Control_R",
            "Alt_L", "Alt_R",
            "Super_L", "Super_R",
            "Meta_L", "Meta_R",
            "Win_L", "Win_R",
        }

        if event.keysym in modifier_keys:
            self._send_packet(CMD_KEY_DOWN, payload)
        else:
            self._send_packet(CMD_KEY_PRESS, payload)

    def _on_key_release(self, event):
        if not self.connected:
            return

        modifier_keys = {
            "Shift_L", "Shift_R",
            "Control_L", "Control_R",
            "Alt_L", "Alt_R",
            "Super_L", "Super_R",
            "Meta_L", "Meta_R",
            "Win_L", "Win_R",
        }

        if event.keysym in modifier_keys:
            name = self._tk_key_to_name(event)
            if not name:
                return

            payload = name.encode("utf-8", errors="ignore")
            self._send_packet(CMD_KEY_UP, payload)


def run_viewer():
    print("[Viewer] Starting in Viewer mode...")
    print(f"[Viewer] Using Proxy at {PROXY_IP}:{PROXY_PORT} (hard-coded)")

    root = tk.Tk()
    ViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_viewer()