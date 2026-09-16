import socket
import struct
import time
import threading
import io
import os
import secrets

import mss
from PIL import Image, ImageTk
import pyautogui
import tkinter as tk
import pynput.mouse as mouse
import pynput.keyboard as keyboard

# --------------------- Config ---------------------
CONFIG_FILE = "proxy_config.txt"
DEFAULT_PROXY_IP = "192.168.160.149"
PROXY_PORT = 5000
QUALITY = 40
FPS = 8
RECONNECT_DELAY = 3
MOUSE_MOVE_MIN_INTERVAL = 0.03   # throttle mouse-move packets (was unthrottled -> flooding)

# A remote-control tool must not be interrupted by PyAutoGUI's corner
# fail-safe: without this, moving the remote cursor to (0,0) raises
# FailSafeException and silently kills the input thread.
pyautogui.FAILSAFE = False

# Map pynput key representations to pyautogui key names so that special
# keys (Enter, Backspace, arrows, etc.) actually work instead of being
# dropped by the old "only single characters" filter.
_PYNPUT_TO_PYAUTOGUI = {
    'Key.enter': 'enter', 'Key.space': 'space', 'Key.tab': 'tab',
    'Key.backspace': 'backspace', 'Key.delete': 'delete', 'Key.esc': 'esc',
    'Key.up': 'up', 'Key.down': 'down', 'Key.left': 'left', 'Key.right': 'right',
    'Key.shift': 'shift', 'Key.shift_r': 'shift', 'Key.ctrl': 'ctrl',
    'Key.ctrl_r': 'ctrl', 'Key.alt': 'alt', 'Key.alt_r': 'alt',
    'Key.caps_lock': 'capslock', 'Key.home': 'home', 'Key.end': 'end',
    'Key.page_up': 'pageup', 'Key.page_down': 'pagedown',
    'Key.f1': 'f1', 'Key.f2': 'f2', 'Key.f3': 'f3', 'Key.f4': 'f4',
    'Key.f5': 'f5', 'Key.f6': 'f6', 'Key.f7': 'f7', 'Key.f8': 'f8',
    'Key.f9': 'f9', 'Key.f10': 'f10', 'Key.f11': 'f11', 'Key.f12': 'f12',
}


def recv_exact(sock, n):
    """Read exactly n bytes or return None if the connection closed early."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def get_proxy_ip():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            ip = f.read().strip()
            if ip:
                return ip
    ip = input(f"Enter Proxy IP (default {DEFAULT_PROXY_IP}): ").strip()
    if not ip:
        ip = DEFAULT_PROXY_IP
    with open(CONFIG_FILE, 'w') as f:
        f.write(ip)
    return ip


def generate_code():
    return str(secrets.randbelow(10 ** 6)).zfill(6)


# ======================== Host mode ========================
def run_host(proxy_ip):
    print("\n[Host] Starting in Host mode...")
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((proxy_ip, PROXY_PORT))
            sock.settimeout(None)
            print(f"[Host] Connected to Proxy at {proxy_ip}:{PROXY_PORT}")
        except Exception as e:
            print(f"[Host] Cannot connect to Proxy: {e}")
            time.sleep(RECONNECT_DELAY)
            continue

        code = generate_code()
        print(f"[Host] Your connection code is: {code}")
        code_bytes = code.encode()
        try:
            sock.sendall(struct.pack('!BI', 0x05, len(code_bytes)) + code_bytes)
            resp = sock.recv(1024)
        except Exception as e:
            print(f"[Host] Registration error: {e}")
            sock.close()
            time.sleep(RECONNECT_DELAY)
            continue

        if b'OK' not in resp:
            print(f"[Host] Registration failed: {resp!r}")
            sock.close()
            time.sleep(RECONNECT_DELAY)
            continue
        print("[Host] Registered. Waiting for viewer...")

        stop_event = threading.Event()
        frame_count = 0

        def send_screen():
            nonlocal frame_count
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while not stop_event.is_set():
                    try:
                        img = sct.grab(monitor)
                        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                        buffer = io.BytesIO()
                        img_pil.save(buffer, format="JPEG", quality=QUALITY, optimize=True)
                        jpeg_data = buffer.getvalue()
                        packet = struct.pack('!BI', 0x01, len(jpeg_data)) + jpeg_data
                        sock.sendall(packet)
                        frame_count += 1
                        if frame_count % 30 == 0:
                            print(f"[Host] Sent {frame_count} frames")
                        time.sleep(1.0 / FPS)
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
                    cmd, length = struct.unpack('!BI', header)
                    data = recv_exact(sock, length)
                    if data is None:
                        print("[Host] Incomplete input packet")
                        break

                    if cmd == 0x02:
                        x, y = struct.unpack('!II', data)
                        pyautogui.moveTo(x, y)
                    elif cmd == 0x03:
                        btn = data[0]
                        pyautogui.click(button='left' if btn == 0 else 'right')
                    elif cmd == 0x04:
                        key_name = data.decode(errors='replace')
                        mapped = _PYNPUT_TO_PYAUTOGUI.get(key_name, key_name)
                        try:
                            if len(mapped) == 1 or mapped in pyautogui.KEYBOARD_KEYS:
                                pyautogui.press(mapped)
                        except Exception as e:
                            print(f"[Host] Could not send key '{key_name}': {e}")
                except Exception as e:
                    print(f"[Host] Error in receive_inputs: {e}")
                    break
            stop_event.set()

        t_send = threading.Thread(target=send_screen, daemon=True)
        t_recv = threading.Thread(target=receive_inputs, daemon=True)
        t_send.start()
        t_recv.start()
        t_send.join()
        t_recv.join()

        try:
            sock.close()
        except Exception:
            pass
        print(f"[Host] Connection closed. Reconnecting in {RECONNECT_DELAY} seconds...")
        time.sleep(RECONNECT_DELAY)


# ======================== Viewer mode ========================
class ViewerApp:
    def __init__(self, root, proxy_ip):
        self.root = root
        self.proxy_ip = proxy_ip
        self.root.title("Remote Viewer")
        self.root.geometry("850x700")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.label = tk.Label(root)
        self.label.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.status = tk.Label(root, text="Enter 6-digit code and click Connect", font=('Arial', 12))
        self.status.pack(pady=5)

        frame = tk.Frame(root)
        frame.pack(pady=10)

        self.code_entry = tk.Entry(frame, font=('Arial', 20), width=10)
        self.code_entry.pack(side=tk.LEFT, padx=5)
        self.code_entry.focus()

        self.connect_btn = tk.Button(frame, text="Connect", command=self.connect_to_host, font=('Arial', 14))
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.sock = None
        self.sock_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.mouse_listener = None
        self.keyboard_listener = None
        self._last_move_sent = 0.0

    def connect_to_host(self):
        code = self.code_entry.get().strip()
        if len(code) != 6 or not code.isdigit():
            self.status.config(text="Code must be 6 digits", fg='red')
            return

        self.status.config(text="Connecting...", fg='blue')
        self.connect_btn.config(state=tk.DISABLED)
        self.stop_event.clear()

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
                self.sock = new_sock

            print(f"[Viewer] Connected to Proxy at {self.proxy_ip}:{PROXY_PORT}")

            code_bytes = code.encode()
            self.sock.sendall(struct.pack('!BI', 0x06, len(code_bytes)) + code_bytes)
            resp = self.sock.recv(1024)

            if b'OK' not in resp:
                self.status.config(text=resp.decode(errors='replace'), fg='red')
                self.sock.close()
                self.sock = None
                self.connect_btn.config(state=tk.NORMAL)
                return

            self.status.config(text="Connected! Receiving screen...", fg='green')
            self.code_entry.pack_forget()
            self.connect_btn.pack_forget()

            threading.Thread(target=self.receive_screen, daemon=True).start()
            self.start_input_listeners()

        except socket.timeout:
            self.status.config(text="Connection timeout", fg='red')
            self.connect_btn.config(state=tk.NORMAL)
        except Exception as e:
            self.status.config(text=f"Connection error: {e}", fg='red')
            self.connect_btn.config(state=tk.NORMAL)

    def receive_screen(self):
        while not self.stop_event.is_set():
            try:
                sock = self.sock
                if sock is None:
                    break
                header = recv_exact(sock, 5)
                if header is None:
                    print("[Viewer] Connection closed")
                    break
                cmd, length = struct.unpack('!BI', header)
                data = recv_exact(sock, length)
                if data is None:
                    print("[Viewer] Incomplete frame")
                    break
                if cmd != 0x01:
                    continue
                img = Image.open(io.BytesIO(data))
                img.thumbnail((800, 600), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(image=img)
                self.label.config(image=imgtk)
                self.label.image = imgtk
            except Exception as e:
                print(f"[Viewer] Error in receive_screen: {e}")
                break

        self.stop_event.set()
        self.status.config(text="Disconnected", fg='red')
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
        self.connect_btn.config(state=tk.NORMAL, text="Reconnect")
        self.connect_btn.pack(pady=5)
        self.code_entry.pack(pady=5)
        self.code_entry.delete(0, tk.END)
        self.code_entry.focus()

    def _send_packet(self, cmd, payload):
        sock = self.sock
        if sock is None or self.stop_event.is_set():
            return
        try:
            sock.sendall(struct.pack('!BI', cmd, len(payload)) + payload)
        except Exception:
            pass

    def start_input_listeners(self):
        def on_move(x, y):
            now = time.time()
            # Throttle: the original code sent a packet on every raw
            # pynput move event, which could flood the link.
            if now - self._last_move_sent < MOUSE_MOVE_MIN_INTERVAL:
                return
            self._last_move_sent = now
            self._send_packet(0x02, struct.pack('!II', max(int(x), 0), max(int(y), 0)))

        def on_click(x, y, button, pressed):
            if pressed:
                btn = 0 if button == mouse.Button.left else 1
                self._send_packet(0x03, bytes([btn]))

        def on_press(key):
            # Send the full pynput key representation (e.g. 'a', 'Key.enter')
            # instead of only single characters, so special keys work.
            k = key.char if hasattr(key, 'char') and key.char else str(key)
            if k:
                self._send_packet(0x04, k.encode())

        self.mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click)
        self.keyboard_listener = keyboard.Listener(on_press=on_press)
        self.mouse_listener.daemon = True
        self.keyboard_listener.daemon = True
        self.mouse_listener.start()
        self.keyboard_listener.start()

    def on_closing(self):
        self.stop_event.set()
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()
        self.root.destroy()


def run_viewer(proxy_ip):
    print("[Viewer] Starting in Viewer mode...")
    root = tk.Tk()
    ViewerApp(root, proxy_ip)
    root.mainloop()


# ======================== Entry point ========================
def main():
    print("\n" + "=" * 40)
    print("     REMOTE DESKTOP APP")
    print("=" * 40)
    print("1. Run as Host (share your screen)")
    print("2. Run as Viewer (watch another screen)")
    choice = input("Select role (1 or 2): ").strip()
    if choice not in ('1', '2'):
        print("Invalid choice. Exiting.")
        return

    proxy_ip = get_proxy_ip()
    print(f"Using Proxy at {proxy_ip}:{PROXY_PORT}")

    if choice == '1':
        run_host(proxy_ip)
    else:
        run_viewer(proxy_ip)


if __name__ == "__main__":
    main()
