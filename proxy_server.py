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
SESSION_TIMEOUT = 60          # seconds a host waits for a viewer before expiring
IDLE_PROBE_INTERVAL = 10      # seconds between dead-socket probes

sessions = {}                 # code -> Session
sessions_lock = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def recv_exact(sock, n):
    """
    Read exactly n bytes from a TCP socket.
    Returns None if the connection closed before n bytes were received.
    This fixes the original bug where a short recv() (which is legal over
    TCP) was misinterpreted as a closed connection.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def safe_send_probe(sock):
    """Return True if the socket still looks alive."""
    try:
        sock.sendall(b'')
        return True
    except Exception:
        return False


class Session:
    """
    Holds everything about one host<->viewer pairing.

    A threading.Event replaces the old busy-wait / manual lock
    release-acquire pattern. That pattern was the source of the
    'release unlocked lock' RuntimeError that fired on every
    successful session in the original code.
    """
    __slots__ = ("host", "host_addr", "viewer", "viewer_addr",
                 "viewer_connected", "stop", "created_at")

    def __init__(self, host_sock, host_addr):
        self.host = host_sock
        self.host_addr = host_addr
        self.viewer = None
        self.viewer_addr = None
        self.viewer_connected = threading.Event()
        self.stop = threading.Event()
        self.created_at = time.time()


# --------------------------------------------------------------------------
# Background cleanup
# --------------------------------------------------------------------------
def cleanup_stale_sessions():
    """Periodically remove sessions whose host/viewer sockets have died,
    or that have been waiting too long for a viewer to show up."""
    while True:
        time.sleep(IDLE_PROBE_INTERVAL)
        with sessions_lock:
            to_remove = []
            for code, s in sessions.items():
                # A session that never got a viewer and has timed out.
                if not s.viewer_connected.is_set() and \
                        time.time() - s.created_at > SESSION_TIMEOUT:
                    to_remove.append(code)
                    s.stop.set()
                    logging.info(f"Session [{code}] expired waiting for viewer")
                    continue

                if not safe_send_probe(s.host):
                    to_remove.append(code)
                    s.stop.set()
                    logging.info(f"Session [{code}] removed: host socket dead")
                    continue

                if s.viewer is not None and not safe_send_probe(s.viewer):
                    s.viewer = None
                    s.viewer_addr = None
                    logging.info(f"Session [{code}]: viewer dropped, host kept alive")

            for code in to_remove:
                sessions.pop(code, None)
            if to_remove:
                logging.info(f"Cleaned up {len(to_remove)} stale session(s)")


# --------------------------------------------------------------------------
# Relay
# --------------------------------------------------------------------------
def relay_data(src_sock, dst_sock, stop_event, session_code, direction):
    while not stop_event.is_set():
        header = recv_exact(src_sock, 5)
        if header is None:
            logging.info(f"Session [{session_code}] ({direction}): connection closed")
            break
        try:
            cmd, length = struct.unpack('!BI', header)
        except struct.error:
            logging.warning(f"Session [{session_code}] ({direction}): bad header")
            break

        data = recv_exact(src_sock, length)
        if data is None:
            logging.info(f"Session [{session_code}] ({direction}): incomplete payload")
            break

        try:
            dst_sock.sendall(header + data)
        except Exception as e:
            logging.error(f"Session [{session_code}] ({direction}): send error - {e}")
            break

    stop_event.set()


# --------------------------------------------------------------------------
# Per-connection handler
# --------------------------------------------------------------------------
def handle_client(client_sock, addr):
    session_code = None
    try:
        client_sock.settimeout(15)
        header = recv_exact(client_sock, 5)
        if header is None:
            logging.warning(f"Client {addr}: no/short header, dropping")
            return
        cmd, length = struct.unpack('!BI', header)

        if length <= 0 or length > 64:
            logging.warning(f"Client {addr}: suspicious code length {length}")
            return

        code_bytes = recv_exact(client_sock, length)
        if code_bytes is None:
            logging.warning(f"Client {addr}: connection closed while reading code")
            return
        code_data = code_bytes.decode(errors='replace').strip()
        client_sock.settimeout(None)
        logging.info(f"New connection from {addr}: cmd={cmd}, code='{code_data}'")

        if cmd == 0x05:  # ---------------- HOST ----------------
            with sessions_lock:
                old = sessions.pop(code_data, None)
                if old is not None:
                    logging.warning(f"Client {addr}: code {code_data} already in use, replacing")
                    old.stop.set()
                    try:
                        old.host.close()
                    except Exception:
                        pass

                if len(sessions) >= MAX_SESSIONS:
                    client_sock.sendall(b'ERROR: Server busy')
                    logging.warning(f"Client {addr}: server busy")
                    return

                session = Session(client_sock, addr)
                sessions[code_data] = session

            client_sock.sendall(b'OK: Registered')
            logging.info(f"Host [{code_data}] registered from {addr}")
            session_code = code_data

            # Block (no lock held, no polling) until a viewer connects or we time out.
            got_viewer = session.viewer_connected.wait(timeout=SESSION_TIMEOUT)

            if not got_viewer or session.stop.is_set():
                logging.info(f"Session [{code_data}] ended before a viewer connected")
                with sessions_lock:
                    sessions.pop(code_data, None)
                try:
                    client_sock.sendall(b'ERROR: Session expired')
                except Exception:
                    pass
                return

            with sessions_lock:
                viewer_sock = session.viewer
                viewer_addr = session.viewer_addr

            logging.info(f"Session [{code_data}] established: host {addr} <-> viewer {viewer_addr}")

            t1 = threading.Thread(target=relay_data, args=(client_sock, viewer_sock, session.stop, code_data, "H->V"), daemon=True)
            t2 = threading.Thread(target=relay_data, args=(viewer_sock, client_sock, session.stop, code_data, "V->H"), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            with sessions_lock:
                sessions.pop(code_data, None)
            logging.info(f"Session [{code_data}] ended and cleaned up")

        elif cmd == 0x06:  # ---------------- VIEWER ----------------
            with sessions_lock:
                session = sessions.get(code_data)
                if session is None:
                    client_sock.sendall(b'ERROR: Code not found')
                    logging.warning(f"Client {addr}: code {code_data} not found")
                    return

                if session.viewer is not None:
                    try:
                        session.viewer.close()
                    except Exception:
                        pass
                    logging.info(f"Session [{code_data}]: previous viewer replaced")

                session.viewer = client_sock
                session.viewer_addr = addr
                session.viewer_connected.set()

            client_sock.sendall(b'OK: Connected to host')
            logging.info(f"Viewer connected to code [{code_data}] from {addr}")
            session_code = code_data

            session.stop.wait()  # blocks without polling; woken as soon as relay ends
            with sessions_lock:
                sessions.pop(code_data, None)
            logging.info(f"Session [{code_data}] cleaned up by viewer side")

        else:
            client_sock.sendall(b'ERROR: Invalid command')
            logging.warning(f"Client {addr}: invalid command {cmd}")

    except socket.timeout:
        logging.warning(f"Client {addr}: connection timeout")
    except ConnectionResetError:
        logging.info(f"Client {addr}: connection reset")
    except Exception as e:
        logging.error(f"Error handling client {addr}: {e}")
    finally:
        if session_code is not None:
            with sessions_lock:
                sessions.pop(session_code, None)
        try:
            client_sock.close()
        except Exception:
            pass


def main():
    threading.Thread(target=cleanup_stale_sessions, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', PROXY_PORT))
    server.listen(100)
    logging.info(f"Proxy server started on port {PROXY_PORT}")
    logging.info(f"Max concurrent sessions: {MAX_SESSIONS}")
    logging.info(f"Host wait-for-viewer timeout: {SESSION_TIMEOUT}s")

    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        logging.info("Proxy server stopping...")
    finally:
        server.close()
        logging.info("Proxy server stopped")


if __name__ == "__main__":
    main()
