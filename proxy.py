import socket
import threading
import struct
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

PROXY_PORT = 5000
MAX_SESSIONS = 100
SESSION_TIMEOUT = 60
CLEANUP_INTERVAL = 10

sessions = {}
sessions_lock = threading.Lock()

def cleanup_stale_sessions():
    """پاکسازی خودکار نشست‌های منقضی یا قطع‌شده"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        with sessions_lock:
            to_remove = []
            for code, data in sessions.items():
                # انقضای زمان
                if time.time() - data['timestamp'] > SESSION_TIMEOUT:
                    to_remove.append(code)
                    logging.info(f"Session [{code}] expired (timeout)")
                    continue
                # بررسی سوکت Host
                if data.get('host'):
                    try:
                        data['host'].sendall(b'')
                    except:
                        to_remove.append(code)
                        logging.info(f"Session [{code}] removed: Host socket closed")
                        continue
                # اگر Viewer متصل است ولی سوکت آن بسته شده
                if data.get('viewer'):
                    try:
                        data['viewer'].sendall(b'')
                    except:
                        data['viewer'] = None
                        data['viewer_addr'] = None
                        logging.info(f"Session [{code}]: Viewer disconnected, waiting for new viewer")
            for code in to_remove:
                sessions.pop(code, None)
            if to_remove:
                logging.info(f"Cleaned up {len(to_remove)} stale sessions")

def relay_data(src_sock, dst_sock, stop_event, session_code, direction):
    """هدایت داده‌ها بین دو سوکت"""
    while not stop_event.is_set():
        try:
            header = src_sock.recv(5)
            if not header or len(header) < 5:
                logging.info(f"Session [{session_code}] ({direction}): connection closed")
                break
            cmd, length = struct.unpack('!BI', header)
            data = b''
            while len(data) < length:
                chunk = src_sock.recv(length - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) != length:
                logging.info(f"Session [{session_code}] ({direction}): incomplete data")
                break
            dst_sock.sendall(header + data)
        except Exception as e:
            logging.error(f"Session [{session_code}] ({direction}): error - {e}")
            break
    stop_event.set()

def handle_client(client_sock, addr):
    session_code = None
    try:
        logging.info(f"New connection from {addr}")
        header = client_sock.recv(5)
        if not header or len(header) < 5:
            logging.warning(f"Client {addr}: Invalid header")
            return
        cmd, length = struct.unpack('!BI', header)
        code_data = client_sock.recv(length).decode().strip()
        logging.info(f"Client {addr}: cmd={cmd}, code='{code_data}'")
        
        with sessions_lock:
            # پاکسازی نشست‌های مرده قبل از هر اقدام
            for code in list(sessions.keys()):
                data = sessions[code]
                if data.get('host'):
                    try:
                        data['host'].sendall(b'')
                    except:
                        sessions.pop(code, None)
                        logging.info(f"Cleaned up dead session [{code}] (host closed)")
                if data.get('viewer'):
                    try:
                        data['viewer'].sendall(b'')
                    except:
                        data['viewer'] = None
                        data['viewer_addr'] = None
                        logging.info(f"Session [{code}]: Viewer removed")
            
            if cmd == 0x05:  # Host
                # اگر کد موجود است، آن را پاک کن (اجباری برای reconnect)
                if code_data in sessions:
                    logging.warning(f"Client {addr}: Code {code_data} exists, cleaning up old session")
                    old_data = sessions[code_data]
                    if old_data.get('host'):
                        try:
                            old_data['host'].close()
                        except:
                            pass
                    sessions.pop(code_data, None)
                
                if len(sessions) >= MAX_SESSIONS:
                    client_sock.sendall(b'ERROR: Server busy')
                    logging.warning(f"Client {addr}: Server busy")
                    return
                
                sessions[code_data] = {
                    'host': client_sock,
                    'viewer': None,
                    'viewer_addr': None,
                    'stop': threading.Event(),
                    'timestamp': time.time(),
                    'host_addr': addr
                }
                client_sock.sendall(b'OK: Registered')
                logging.info(f"Host [{code_data}] registered from {addr}")
                session_code = code_data
                
                # انتظار برای Viewer (با تایم‌اوت)
                while sessions[code_data]['viewer'] is None:
                    sessions_lock.release()
                    time.sleep(0.5)
                    sessions_lock.acquire()
                    if time.time() - sessions[code_data]['timestamp'] > SESSION_TIMEOUT:
                        logging.info(f"Session [{code_data}] expired (timeout)")
                        sessions.pop(code_data, None)
                        client_sock.sendall(b'ERROR: Session expired')
                        client_sock.close()
                        return
                    if not sessions.get(code_data) or sessions[code_data]['stop'].is_set():
                        break
                
                if sessions.get(code_data) and sessions[code_data]['viewer']:
                    viewer_sock = sessions[code_data]['viewer']
                    viewer_addr = sessions[code_data]['viewer_addr']
                    stop_event = sessions[code_data]['stop']
                    sessions_lock.release()
                    
                    logging.info(f"Session [{code_data}] established: Host {addr} <-> Viewer {viewer_addr}")
                    
                    t1 = threading.Thread(target=relay_data, args=(client_sock, viewer_sock, stop_event, code_data, "H->V"))
                    t2 = threading.Thread(target=relay_data, args=(viewer_sock, client_sock, stop_event, code_data, "V->H"))
                    t1.daemon = True
                    t2.daemon = True
                    t1.start()
                    t2.start()
                    t1.join()
                    t2.join()
                    
                    with sessions_lock:
                        sessions.pop(code_data, None)
                        logging.info(f"Session [{code_data}] ended and cleaned up")
                else:
                    client_sock.close()
                    
            elif cmd == 0x06:  # Viewer
                if code_data not in sessions:
                    client_sock.sendall(b'ERROR: Code not found')
                    logging.warning(f"Client {addr}: Code {code_data} not found")
                    return
                # اگر Viewer قبلاً متصل بوده، آن را قطع کن (برای reconnect)
                if sessions[code_data]['viewer'] is not None:
                    try:
                        sessions[code_data]['viewer'].close()
                    except:
                        pass
                    sessions[code_data]['viewer'] = None
                    sessions[code_data]['viewer_addr'] = None
                    logging.info(f"Session [{code_data}]: Previous viewer removed for reconnect")
                
                sessions[code_data]['viewer'] = client_sock
                sessions[code_data]['viewer_addr'] = addr
                client_sock.sendall(b'OK: Connected to host')
                logging.info(f"Viewer connected to code [{code_data}] from {addr}")
                session_code = code_data
                
                # منتظر پایان نشست
                stop_event = sessions[code_data]['stop']
                while not stop_event.is_set():
                    time.sleep(1)
                with sessions_lock:
                    sessions.pop(code_data, None)
                    logging.info(f"Session [{code_data}] cleaned up by Viewer side")
            else:
                client_sock.sendall(b'ERROR: Invalid command')
                logging.warning(f"Client {addr}: Invalid command {cmd}")
                
    except socket.timeout:
        logging.warning(f"Client {addr}: Connection timeout")
    except ConnectionResetError:
        logging.info(f"Client {addr}: Connection reset")
    except Exception as e:
        logging.error(f"Error handling client {addr}: {e}")
    finally:
        if session_code and session_code in sessions:
            with sessions_lock:
                sessions.pop(session_code, None)
                logging.info(f"Session [{session_code}] cleaned up due to error")
        client_sock.close()

def main():
    # ترد پاکسازی خودکار
    cleaner = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleaner.start()
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', PROXY_PORT))
    server.listen(100)
    logging.info(f"Proxy Server started on port {PROXY_PORT}")
    logging.info(f"Max concurrent sessions: {MAX_SESSIONS}")
    logging.info(f"Session timeout: {SESSION_TIMEOUT} seconds")
    
    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            logging.info("Proxy Server stopping...")
            break
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
    
    server.close()
    logging.info("Proxy Server stopped")

if __name__ == "__main__":
    main()