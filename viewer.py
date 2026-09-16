import socket
import struct
import threading
import tkinter as tk
from PIL import Image, ImageTk
import io
import pynput.mouse as mouse
import pynput.keyboard as keyboard

PROXY_IP = "192.168.160.2"
PROXY_PORT = 5000

class ViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Remote Viewer")
        self.label = tk.Label(root)
        self.label.pack(fill=tk.BOTH, expand=True)
        self.sock = None
        self.stop_event = threading.Event()
        
        # ورودی کد
        self.code_entry = tk.Entry(root, font=('Arial', 20), width=10)
        self.code_entry.pack(pady=10)
        self.code_entry.focus()
        
        btn = tk.Button(root, text="Connect", command=self.connect_to_host, font=('Arial', 14))
        btn.pack(pady=5)
        
        self.status = tk.Label(root, text="Enter code and click Connect")
        self.status.pack()
        
        # کنترل‌کننده ورودی‌ها (فعال پس از اتصال)
        self.mouse_listener = None
        self.keyboard_listener = None
        
    def connect_to_host(self):
        code = self.code_entry.get().strip()
        if len(code) != 6:
            self.status.config(text="❌ Code must be 6 digits")
            return
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((PROXY_IP, PROXY_PORT))
            
            # ارسال درخواست اتصال (Viewer)
            cmd = 0x06
            code_bytes = code.encode()
            self.sock.sendall(struct.pack('!BI', cmd, len(code_bytes)) + code_bytes)
            
            resp = self.sock.recv(1024)
            if b'OK' not in resp:
                self.status.config(text="❌ " + resp.decode())
                self.sock.close()
                return
            
            self.status.config(text="✅ Connected! Receiving screen...")
            self.code_entry.pack_forget()
            btn = self.root.winfo_children()[-2]  # دکمه کانکت
            btn.pack_forget()
            
            # شروع ترد دریافت تصویر
            threading.Thread(target=self.receive_screen, daemon=True).start()
            
            # فعال‌سازی ارسال ورودی‌ها
            self.start_input_listeners()
            
        except Exception as e:
            self.status.config(text=f"❌ Connection error: {e}")
    
    def receive_screen(self):
        while not self.stop_event.is_set():
            try:
                header = self.sock.recv(5)
                if not header or len(header) < 5:
                    break
                cmd, length = struct.unpack('!BI', header)
                if cmd != 0x01:  # فقط تصویر
                    continue
                
                data = b''
                while len(data) < length:
                    chunk = self.sock.recv(length - len(data))
                    if not chunk:
                        break
                    data += chunk
                
                # نمایش تصویر
                img = Image.open(io.BytesIO(data))
                # تغییر سایز متناسب با پنجره
                img.thumbnail((800, 600), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(image=img)
                self.label.config(image=imgtk)
                self.label.image = imgtk
                
            except Exception as e:
                print(f"Receive error: {e}")
                break
        self.stop_event.set()
        self.status.config(text="❌ Disconnected")
    
    def start_input_listeners(self):
        # ارسال ورودی‌ها به Proxy (که به Host می‌رسد)
        def on_move(x, y):
            if self.sock:
                try:
                    packet = struct.pack('!BI', 0x02, 8) + struct.pack('!II', int(x), int(y))
                    self.sock.sendall(packet)
                except:
                    pass
        
        def on_click(x, y, button, pressed):
            if pressed and self.sock:
                try:
                    btn = 0 if button == mouse.Button.left else 1
                    packet = struct.pack('!BI', 0x03, 1) + bytes([btn])
                    self.sock.sendall(packet)
                except:
                    pass
        
        def on_press(key):
            if self.sock:
                try:
                    k = key.char if hasattr(key, 'char') and key.char else str(key)
                    if k:
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
            self.sock.close()
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.keyboard_listener:
            self.keyboard_listener.stop()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.geometry("800x650")
    root.mainloop()