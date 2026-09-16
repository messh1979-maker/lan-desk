import socket
import struct
import time
import threading
import mss
from PIL import Image, ImageTk
import io
import pyautogui
import secrets
import os
import tkinter as tk
from tkinter import messagebox
import pynput.mouse as mouse
import pynput.keyboard as keyboard

# --------------------- تنظیمات ---------------------
CONFIG_FILE = "proxy_config.txt"
DEFAULT_PROXY_IP = "192.168.160.149"
PROXY_PORT = 5000
QUALITY = 40
FPS = 8
RECONNECT_DELAY = 3

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
    return str(secrets.randbelow(10**6)).zfill(6)

# ======================== بخش Host (با reconnect خودکار) ========================
def run_host(proxy_ip):
    print("\n[Host] Starting in Host mode...")
    while True:  # حلقه اصلی reconnect
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((proxy_ip, PROXY_PORT))
            sock.settimeout(None)
            print(f"[Host] Connected to Proxy at {proxy_ip}:{PROXY_PORT}")
        except Exception as e:
            print(f"[Host] ❌ Cannot connect to Proxy: {e}")
            time.sleep(RECONNECT_DELAY)
            continue

        code = generate_code()
        print(f"[Host] ✅ Your connection code is: {code}")
        cmd = 0x05
        code_bytes = code.encode()
        try:
            sock.sendall(struct.pack('!BI', cmd, len(code_bytes)) + code_bytes)
            resp = sock.recv(1024)
            print(f"[Host] Proxy response: {resp}")
        except Exception as e:
            print(f"[Host] ❌ Registration error: {e}")
            if sock:
                sock.close()
            time.sleep(RECONNECT_DELAY)
            continue

        if b'OK' not in resp:
            print(f"[Host] ❌ Registration failed: {resp.decode()}")
            if sock:
                sock.close()
            time.sleep(RECONNECT_DELAY)
            continue
        print("[Host] ✅ Registered. Waiting for viewer...")

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
                    header = sock.recv(5)
                    if not header or len(header) < 5:
                        print("[Host] No header in receive_inputs, connection closed")
                        break
                    cmd, length = struct.unpack('!BI', header)
                    data = sock.recv(length)
                    if len(data) != length:
                        break
                    if cmd == 0x02:
                        x, y = struct.unpack('!II', data)
                        pyautogui.moveTo(x, y)
                    elif cmd == 0x03:
                        btn = data[0]
                        pyautogui.click(button='left' if btn == 0 else 'right')
                    elif cmd == 0x04:
                        key = data.decode()
                        pyautogui.press(key)
                except Exception as e:
                    print(f"[Host] Error in receive_inputs: {e}")
                    break
            stop_event.set()

        t_send = threading.Thread(target=send_screen, daemon=True)
        t_recv = threading.Thread(target=receive_inputs, daemon=True)
        t_send.start()
        t_recv.start()

        # منتظر بمان تا ارتباط قطع شود
        while not stop_event.is_set():
            time.sleep(0.5)

        # پاکسازی
        if sock:
            try:
                sock.close()
            except:
                pass
        print("[Host] Connection closed. Reconnecting in {} seconds...".format(RECONNECT_DELAY))
        time.sleep(RECONNECT_DELAY)
        # حلقه ادامه می‌یابد و دوباره تلاش می‌کند

# ======================== بخش Viewer (با reconnect دستی) ========================
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
        self.stop_event = threading.Event()
        self.mouse_listener = None
        self.keyboard_listener = None

    def connect_to_host(self):
        code = self.code_entry.get().strip()
        if len(code) != 6 or not code.isdigit():
            self.status.config(text="❌ Code must be 6 digits", fg='red')
            return

        self.status.config(text="🔄 Connecting...", fg='blue')
        self.connect_btn.config(state=tk.DISABLED)

        try:
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None

            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.proxy_ip, PROXY_PORT))
            self.sock.settimeout(None)
            print(f"[Viewer] Connected to Proxy at {self.proxy_ip}:{PROXY_PORT}")

            cmd = 0x06
            code_bytes = code.encode()
            self.sock.sendall(struct.pack('!BI', cmd, len(code_bytes)) + code_bytes)
            resp = self.sock.recv(1024)
            print(f"[Viewer] Proxy response: {resp}")

            if b'OK' not in resp:
                self.status.config(text="❌ " + resp.decode(), fg='red')
                self.sock.close()
                self.sock = None
                self.connect_btn.config(state=tk.NORMAL)
                return

            self.status.config(text="✅ Connected! Receiving screen...", fg='green')
            self.code_entry.pack_forget()
            self.connect_btn.pack_forget()

            # شروع ترد دریافت
            threading.Thread(target=self.receive_screen, daemon=True).start()
            self.start_input_listeners()

        except socket.timeout:
            self.status.config(text="❌ Connection timeout", fg='red')
            self.connect_btn.config(state=tk.NORMAL)
        except Exception as e:
            self.status.config(text=f"❌ Connection error: {e}", fg='red')
            self.connect_btn.config(state=tk.NORMAL)

    def receive_screen(self):
        while not self.stop_event.is_set():
            try:
                header = self.sock.recv(5)
                if not header or len(header) < 5:
                    print("[Viewer] No header received")
                    break
                cmd, length = struct.unpack('!BI', header)
                if cmd != 0x01:
                    continue
                data = b''
                while len(data) < length:
                    chunk = self.sock.recv(length - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) != length:
                    print("[Viewer] Incomplete frame")
                    break
                img = Image.open(io.BytesIO(data))
                img.thumbnail((800, 600), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(image=img)
                self.label.config(image=imgtk)
                self.label.image = imgtk
            except Exception as e:
                print(f"[Viewer] Error in receive_screen: {e}")
                break
        # قطع شدن
        self.stop_event.set()
        self.status.config(text="❌ Disconnected", fg='red')
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        # نمایش دکمه reconnect
        self.connect_btn.config(state=tk.NORMAL, text="Reconnect")
        self.connect_btn.pack(pady=5)
        self.code_entry.pack(pady=5)
        self.code_entry.delete(0, tk.END)
        self.code_entry.focus()

    def start_input_listeners(self):
        def on_move(x, y):
            if self.sock and not self.stop_event.is_set():
                try:
                    packet = struct.pack('!BI', 0x02, 8) + struct.pack('!II', int(x), int(y))
                    self.sock.sendall(packet)
                except:
                    pass

        def on_click(x, y, button, pressed):
            if pressed and self.sock and not self.stop_event.is_set():
                try:
                    btn = 0 if button == mouse.Button.left else 1
                    packet = struct.pack('!BI', 0x03, 1) + bytes([btn])
                    self.sock.sendall(packet)
                except:
                    pass

        def on_press(key):
            if self.sock and not self.stop_event.is_set():
                try:
                    k = key.char if hasattr(key, 'char') and key.char else str(key)
                    if k and len(k) == 1:
                        packet = struct.pack('!BI', 0x04, len(k)) + k.encode()
                        self.sock.sendall(packet)
                except:
                    pass

        self.mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click)
        self.keyboard_listener = keyboard.Listener(on_press=on_press)
        self.mouse_listener.daemon = True
        self.keyboard_listener.daemon = True
        self.mouse_listener.start()
        self.keyboard_listener.start()

    def on_closing(self):
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()
        self.root.destroy()

def run_viewer(proxy_ip):
    print("[Viewer] Starting in Viewer mode...")
    root = tk.Tk()
    app = ViewerApp(root, proxy_ip)
    root.mainloop()

# ======================== منوی اصلی ========================
def main():
    print("\n" + "="*40)
    print("     REMOTE DESKTOP APP")
    print("="*40)
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