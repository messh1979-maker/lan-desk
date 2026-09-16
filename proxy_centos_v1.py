#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy Server for Remote Desktop Application
Target OS : CentOS 7
Server IP : 192.168.90.250
Port      : 8899  (no conflict with iperf3 on 443)

Firewall setup (run once on CentOS):
    sudo firewall-cmd --permanent --add-port=8899/tcp
    sudo firewall-cmd --reload

SELinux (if enforcing):
    sudo semanage port -a -t http_port_t -p tcp 8899
    # or temporarily:  sudo setenforce 0
"""

import socket
import threading
import struct
import time
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# --------------------- Config ---------------------
BIND_IP = "0.0.0.0"              # Listen on all interfaces
PROXY_PORT = 8899                # Safe: iperf3=443, no CentOS conflict
MAX_SESSIONS = 100
SESSION_TIMEOUT = 60
IDLE_PROBE_INTERVAL = 10
SOCKET_BUF_SIZE = 512 * 1024
MAX_RELAY_PAYLOAD = 10 * 1024 * 1024
MAX_INITIAL_CODE_LENGTH = 64

sessions = {}
sessions_lock = threading.Lock()
shutdown_event = threading.Event()


# --------------------- Helpers ---------------------
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


def safe_send_probe(sock):
    try:
        sock.sendall(b"")
        return True
    except Exception:
        return False


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


class Session:
    __slots__ = (
        "host",
        "host_addr",
        "viewer",
        "viewer_addr",
        "viewer_connected",
        "stop",
        "created_at",
    )

    def __init__(self, host_sock, host_addr):
        self.host = host_sock
        self.host_addr = host_addr
        self.viewer = None
        self.viewer_addr = None
        self.viewer_connected = threading.Event()
        self.stop = threading.Event()
        self.created_at = time.time()


# --------------------- Background cleanup ---------------------
def cleanup_stale_sessions():
    while not shutdown_event.is_set():
        time.sleep(IDLE_PROBE_INTERVAL)
        with sessions_lock:
            to_remove = []
            for code, s in sessions.items():
                if (
                    not s.viewer_connected.is_set()
                    and (time.time() - s.created_at) > SESSION_TIMEOUT
                ):
                    to_remove.append(code)
                    s.stop.set()
                    s.viewer_connected.set()
                    logging.info(f"Session [{code}] expired waiting for viewer")
                    continue
                if not safe_send_probe(s.host):
                    to_remove.append(code)
                    s.stop.set()
                    s.viewer_connected.set()
                    logging.info(f"Session [{code}] removed: host socket dead")
                    continue
                if s.viewer is not None and not safe_send_probe(s.viewer):
                    try:
                        s.viewer.close()
                    except Exception:
                        pass
                    s.viewer = None
                    s.viewer_addr = None
                    if s.viewer_connected.is_set():
                        s.stop.set()
                    logging.info(f"Session [{code}]: viewer socket dead")
            for code in to_remove:
                session = sessions.pop(code, None)
                if session is None:
                    continue
                session.stop.set()
                session.viewer_connected.set()
                try:
                    session.host.close()
                except Exception:
                    pass
                if session.viewer is not None:
                    try:
                        session.viewer.close()
                    except Exception:
                        pass
            if to_remove:
                logging.info(f"Cleaned up {len(to_remove)} stale session(s)")


# --------------------- Relay ---------------------
def relay_data(src_sock, dst_sock, stop_event, session_code, direction):
    while not stop_event.is_set():
        header = recv_exact(src_sock, 5)
        if header is None:
            logging.info(f"Session [{session_code}] ({direction}): connection closed")
            break
        try:
            cmd, length = struct.unpack("!BI", header)
        except struct.error:
            logging.warning(f"Session [{session_code}] ({direction}): bad header")
            break
        if length > MAX_RELAY_PAYLOAD:
            logging.warning(
                f"Session [{session_code}] ({direction}): "
                f"payload too large: {length} bytes"
            )
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


# --------------------- Per-connection handler ---------------------
def handle_client(client_sock, addr):
    session_code = None
    session_obj = None
    try:
        set_sock_fast(client_sock)
        client_sock.settimeout(15)
        header = recv_exact(client_sock, 5)
        if header is None:
            logging.warning(f"Client {addr}: no/short header, dropping")
            return
        try:
            cmd, length = struct.unpack("!BI", header)
        except struct.error:
            logging.warning(f"Client {addr}: bad header")
            return
        if length <= 0 or length > MAX_INITIAL_CODE_LENGTH:
            logging.warning(f"Client {addr}: suspicious code length {length}")
            try:
                client_sock.sendall(b"ERROR: Invalid code length")
            except Exception:
                pass
            return
        code_bytes = recv_exact(client_sock, length)
        if code_bytes is None:
            logging.warning(f"Client {addr}: connection closed while reading code")
            return
        code_data = code_bytes.decode(errors="replace").strip()
        client_sock.settimeout(None)
        logging.info(f"New connection from {addr}: cmd={cmd}, code='{code_data}'")

        # ---------------- HOST ----------------
        if cmd == 0x05:
            with sessions_lock:
                old = sessions.pop(code_data, None)
                if old is not None:
                    logging.warning(
                        f"Client {addr}: code {code_data} already in use, replacing"
                    )
                    old.stop.set()
                    old.viewer_connected.set()
                    try:
                        old.host.close()
                    except Exception:
                        pass
                    if old.viewer is not None:
                        try:
                            old.viewer.close()
                        except Exception:
                            pass
                if len(sessions) >= MAX_SESSIONS:
                    try:
                        client_sock.sendall(b"ERROR: Server busy")
                    except Exception:
                        pass
                    logging.warning(f"Client {addr}: server busy")
                    return
                session = Session(client_sock, addr)
                sessions[code_data] = session
                session_obj = session
            try:
                client_sock.sendall(b"OK: Registered")
            except Exception:
                logging.warning(f"Host [{code_data}]: failed to send registration OK")
                return
            logging.info(f"Host [{code_data}] registered from {addr}")
            session_code = code_data
            got_viewer = session.viewer_connected.wait(timeout=SESSION_TIMEOUT)
            if not got_viewer or session.stop.is_set():
                logging.info(f"Session [{code_data}] ended before a viewer connected")
                with sessions_lock:
                    if sessions.get(code_data) is session:
                        sessions.pop(code_data, None)
                try:
                    client_sock.sendall(b"ERROR: Session expired")
                except Exception:
                    pass
                return
            with sessions_lock:
                viewer_sock = session.viewer
                viewer_addr = session.viewer_addr
            if viewer_sock is None:
                logging.error(f"Session [{code_data}]: viewer socket is None")
                with sessions_lock:
                    if sessions.get(code_data) is session:
                        sessions.pop(code_data, None)
                try:
                    client_sock.sendall(b"ERROR: No viewer socket")
                except Exception:
                    pass
                return
            logging.info(
                f"Session [{code_data}] established: "
                f"host {addr} <-> viewer {viewer_addr}"
            )
            t1 = threading.Thread(
                target=relay_data,
                args=(client_sock, viewer_sock, session.stop, code_data, "H->V"),
                daemon=True,
            )
            t2 = threading.Thread(
                target=relay_data,
                args=(viewer_sock, client_sock, session.stop, code_data, "V->H"),
                daemon=True,
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            with sessions_lock:
                if sessions.get(code_data) is session:
                    sessions.pop(code_data, None)
            logging.info(f"Session [{code_data}] ended and cleaned up")

        # ---------------- VIEWER ----------------
        elif cmd == 0x06:
            with sessions_lock:
                session = sessions.get(code_data)
                if session is None:
                    try:
                        client_sock.sendall(b"ERROR: Code not found")
                    except Exception:
                        pass
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
                session_obj = session
            try:
                client_sock.sendall(b"OK: Connected to host")
            except Exception:
                logging.warning(f"Viewer [{code_data}]: failed to send connection OK")
                return
            logging.info(f"Viewer connected to code [{code_data}] from {addr}")
            session_code = code_data
            session.stop.wait()
            with sessions_lock:
                if sessions.get(code_data) is session:
                    sessions.pop(code_data, None)
            logging.info(f"Session [{code_data}] cleaned up by viewer side")
        else:
            try:
                client_sock.sendall(b"ERROR: Invalid command")
            except Exception:
                pass
            logging.warning(f"Client {addr}: invalid command {cmd}")
    except socket.timeout:
        logging.warning(f"Client {addr}: connection timeout")
    except ConnectionResetError:
        logging.info(f"Client {addr}: connection reset")
    except Exception as e:
        logging.error(f"Error handling client {addr}: {e}")
    finally:
        if session_code is not None and session_obj is not None:
            with sessions_lock:
                if sessions.get(session_code) is session_obj:
                    sessions.pop(session_code, None)
        try:
            client_sock.close()
        except Exception:
            pass


# --------------------- Main ---------------------
def main():
    # Graceful shutdown on CentOS (systemctl stop / kill)
    def _signal_handler(sig, frame):
        logging.info("Received shutdown signal, stopping...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    threading.Thread(target=cleanup_stale_sessions, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((BIND_IP, PROXY_PORT))
    except OSError as e:
        logging.error(f"Cannot bind to {BIND_IP}:{PROXY_PORT} -> {e}")
        logging.error("Check:  ss -tlnp | grep 8899")
        sys.exit(1)

    server.listen(100)
    server.settimeout(1.0)

    logging.info("=" * 55)
    logging.info(f"  Proxy server listening on {BIND_IP}:{PROXY_PORT}")
    logging.info(f"  Server IP : 192.168.90.250")
    logging.info(f"  iperf3    : port 443  (NO CONFLICT)")
    logging.info(f"  Max sessions : {MAX_SESSIONS}")
    logging.info(f"  Session timeout : {SESSION_TIMEOUT}s")
    logging.info(f"  Max relay payload : {MAX_RELAY_PAYLOAD} bytes")
    logging.info("=" * 55)

    try:
        while not shutdown_event.is_set():
            try:
                conn, addr = server.accept()
                threading.Thread(
                    target=handle_client,
                    args=(conn, addr),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        server.close()
        logging.info("Proxy server stopped")


if __name__ == "__main__":
    main()