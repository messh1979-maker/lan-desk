import socket
import struct
import time
import threading
import mss
from PIL import Image
import io
import pyautogui
import secrets
import sys

# تنظیمات
PROXY_IP = "192.168.160.149"  # آدرس سرور Proxy
PROXY_PORT = 5000
QUALITY = 40  # کیفیت JPEG (۳۰ تا ۵۰ برای پهنای باند کم)
FPS = 8       # فریم در ثانیه

def generate_code():
    return str(secrets.randbelow(10**6)).zfill(6)  # کد ۶ رقمی

def send_screen(sock, stop_event):
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # مانیتور اصلی
        while not stop_event.is_set():
            try:
                # ضبط صفحه
                img = sct.grab(monitor)
                img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                
                # تغییر سایز برای کاهش پهنای باند (اختیاری)
                # img_pil = img_pil.resize((1024, 768), Image.Resampling.LANCZOS)
                
                # فشرده‌سازی به JPEG
                buffer = io.BytesIO()
                img_pil.save(buffer, format="JPEG", quality=QUALITY, optimize=True)
                jpeg_data = buffer.getvalue()
                
                # ساخت هدر و ارسال
                cmd = 0x01
                length = len(jpeg_data)
                packet = struct.pack('!BI', cmd, length) + jpeg_data
                sock.sendall(packet)
                
                time.sleep(1.0 / FPS)
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception as e:
                print(f"Send error: {e}")
                break
    stop_event.set()

def receive_inputs(sock, stop_event):
    """دریافت ورودی‌ها از Proxy و اعمال روی سیستم"""
    while not stop_event.is_set():
        try:
            header = sock.recv(5)
            if not header or len(header) < 5:
                break
            cmd, length = struct.unpack('!BI', header)
            data = sock.recv(length)
            
            if cmd == 0x02:  # حرکت ماوس
                x, y = struct.unpack('!II', data)
                pyautogui.moveTo(x, y)
            elif cmd == 0x03:  # کلیک
                btn = data[0]
                pyautogui.click(button='left' if btn == 0 else 'right')
            elif cmd == 0x04:  # کیبورد
                key = data.decode()
                pyautogui.press(key)
        except:
            break
    stop_event.set()

def main():
    # اتصال به Proxy
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((PROXY_IP, PROXY_PORT))
    
    # تولید و ارسال کد
    code = generate_code()
    print(f"✅ Your connection code is: {code}")
    cmd = 0x05
    code_bytes = code.encode()
    sock.sendall(struct.pack('!BI', cmd, len(code_bytes)) + code_bytes)
    
    # دریافت پاسخ
    resp = sock.recv(1024)
    if b'OK' not in resp:
        print("❌ Registration failed:", resp.decode())
        sock.close()
        return
    print("✅ Registered on proxy. Waiting for viewer...")
    
    stop_event = threading.Event()
    # ترد ارسال تصویر
    t_send = threading.Thread(target=send_screen, args=(sock, stop_event))
    # ترد دریافت ورودی
    t_recv = threading.Thread(target=receive_inputs, args=(sock, stop_event))
    t_send.daemon = True
    t_recv.daemon = True
    t_send.start()
    t_recv.start()
    
    try:
        t_send.join()
        t_recv.join()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sock.close()
        print("Host closed.")

if __name__ == "__main__":
    main()