from __future__ import annotations

import select
import socket
import sys
import tarfile
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from . import __version__

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
LIGHT_GRAY = "\033[90m"

BUFFER_SIZE = 1024 * 64


@dataclass
class TcpServerConfig:
    port: int
    file_path: Optional[str] = None
    file_paths: Optional[List[str]] = None
    host: str = "0.0.0.0"
    once: bool = False


@dataclass
class TcpClientConfig:
    host: str
    port: int
    destination_path: str


class TcpTransferError(RuntimeError):
    pass


def _send_line(sock: socket.socket, text: str) -> None:
    sock.sendall((text.strip() + "\n").encode("utf-8"))


def _recv_line(sock: socket.socket) -> str:
    buf = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            break
        if b == b"\n":
            break
        buf.extend(b)
    return buf.decode("utf-8").strip()


def _check_version_compatibility(peer_line: str, role: str) -> Optional[str]:
    """Validates remote peer version against local version and warns if mismatch."""
    peer_version = None
    if peer_line.startswith("AGI_LOGGER_VERSION:"):
        peer_version = peer_line.split(":", 1)[1].strip()

    if not peer_version:
        print(
            f"{BOLD}{YELLOW}[WARNING] {role} did not report an agi-logger version (received: '{peer_line}').\n"
            f"          Remote may be running an incompatible version. Transfer may be defective!{RESET}"
        )
        return None

    if peer_version != __version__:
        print(
            f"{BOLD}{YELLOW}[WARNING] Version mismatch detected! Local agi-logger is v{__version__}, but {role} is v{peer_version}.\n"
            f"          Transfer may be defective due to protocol/version differences!{RESET}"
        )
    return peer_version


def _get_dir_uncompressed_size(dir_path: Path) -> int:
    """Calculates total uncompressed byte size of all files in a directory."""
    return sum(f.stat().st_size for f in dir_path.rglob("*") if f.is_file())


class _ChunkedSocketWriter:
    """File-like stream wrapper that writes chunked tar stream frames directly to a socket with live progress."""

    def __init__(self, sock: socket.socket, total_size: int, item_name: str, prefix: str = "") -> None:
        self._sock = sock
        self._total_size = total_size
        self._item_name = item_name
        self._prefix = prefix
        self._sent = 0

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        header = f"{len(data):x}\n".encode("ascii")
        self._sock.sendall(header + data)
        self._sent += len(data)
        pct = (self._sent / self._total_size) * 100 if self._total_size > 0 else 100
        print(f"{self._prefix}Streaming {self._item_name}: {self._sent}/{self._total_size} bytes ({pct:.1f}%)", end="\r")
        return len(data)

    def finish(self) -> None:
        """Sends terminal chunk to signal end of stream."""
        self._sock.sendall(b"0\n")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _ChunkedSocketReader:
    """File-like stream wrapper that reads chunked stream frames from a socket with live progress."""

    def __init__(self, sock: socket.socket, total_size: int, item_name: str, prefix: str = "") -> None:
        self._sock = sock
        self._total_size = total_size
        self._item_name = item_name
        self._prefix = prefix
        self._current_chunk_remain = 0
        self._eof = False
        self._received = 0

    def _read_chunk_header(self) -> int:
        line = bytearray()
        while True:
            b = self._sock.recv(1)
            if not b or b == b"\n":
                break
            line.extend(b)
        line_str = line.decode("ascii").strip()
        if not line_str:
            return 0
        return int(line_str, 16)

    def read(self, size: int = -1) -> bytes:
        if self._eof:
            return b""
        if size is None or size < 0:
            size = BUFFER_SIZE

        buf = bytearray()
        while len(buf) < size and not self._eof:
            if self._current_chunk_remain == 0:
                chunk_len = self._read_chunk_header()
                if chunk_len == 0:
                    self._eof = True
                    break
                self._current_chunk_remain = chunk_len

            to_read = min(size - len(buf), self._current_chunk_remain)
            chunk = self._sock.recv(to_read)
            if not chunk:
                self._eof = True
                break
            buf.extend(chunk)
            self._current_chunk_remain -= len(chunk)
            self._received += len(chunk)
            pct = (self._received / self._total_size) * 100 if self._total_size > 0 else 100
            print(f"{self._prefix}Receiving {self._item_name}: {self._received}/{self._total_size} bytes ({pct:.1f}%)", end="\r")

        return bytes(buf)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _send_single_item(client_socket: socket.socket, file_path: Path, item_prefix: str = "") -> None:
    if file_path.is_dir():
        transfer_size = _get_dir_uncompressed_size(file_path)
        metadata = f"DIR:{file_path.name}:{transfer_size}"
        _send_line(client_socket, metadata)
        ack = _recv_line(client_socket)
        if ack != "READY":
            raise TcpTransferError(f"Client rejected item '{file_path.name}' (ack: {ack})")

        writer = _ChunkedSocketWriter(client_socket, transfer_size, file_path.name, item_prefix)
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            tar.add(str(file_path), arcname=file_path.name)
        writer.finish()

        print(f"\n{item_prefix}Sent directory '{file_path.name}' ({transfer_size} uncompressed bytes) successfully.")
    else:
        transfer_size = file_path.stat().st_size
        metadata = f"FILE:{file_path.name}:{transfer_size}"
        _send_line(client_socket, metadata)
        ack = _recv_line(client_socket)
        if ack != "READY":
            raise TcpTransferError(f"Client rejected item '{file_path.name}' (ack: {ack})")

        sent = 0
        with file_path.open("rb") as handle:
            while chunk := handle.read(BUFFER_SIZE):
                client_socket.sendall(chunk)
                sent += len(chunk)
                pct = (sent / transfer_size) * 100 if transfer_size > 0 else 100
                print(f"{item_prefix}Sending {file_path.name}: {sent}/{transfer_size} bytes ({pct:.1f}%)", end="\r")

        print(f"\n{item_prefix}Sent '{file_path.name}' ({transfer_size} bytes) successfully.")


def get_host_ips() -> List[str]:
    """Returns detected IP addresses of the host machine."""
    ips: List[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            if primary_ip and primary_ip not in ips:
                ips.append(primary_ip)
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    return ips or ["127.0.0.1"]


def send_file(server: TcpServerConfig) -> str:
    raw_paths: List[str] = []
    if server.file_paths:
        raw_paths = list(server.file_paths)
    elif server.file_path:
        raw_paths = [server.file_path]

    if not raw_paths:
        raise TcpTransferError("No files or bag paths configured to send")

    valid_paths: List[Path] = []
    for p in raw_paths:
        path_obj = Path(p).expanduser().resolve()
        if not path_obj.exists():
            raise TcpTransferError(f"Path not found: {path_obj}")
        valid_paths.append(path_obj)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((server.host, server.port))
        except OSError as exc:
            if server.host not in ("0.0.0.0", ""):
                raise TcpTransferError(
                    f"Failed to bind to host '{server.host}:{server.port}' ({exc}). "
                    f"If the host IP cannot be bound, fallback to Bind Host: 0.0.0.0"
                ) from exc
            raise TcpTransferError(f"Failed to bind to {server.host}:{server.port}: {exc}") from exc
        sock.listen(1)
        sock.settimeout(0.2)

        host_ips = get_host_ips()
        host_ip_str = ", ".join(host_ips)
        primary_ip = host_ips[0] if host_ips else server.host
        names_summary = ", ".join(p.name for p in valid_paths[:3])
        if len(valid_paths) > 3:
            names_summary += f", ... (+{len(valid_paths) - 3} more)"
        print(f"\n{BOLD}{CYAN}Server Host IP:{RESET} {BOLD}{GREEN}{host_ip_str}{RESET}")
        print(f"Server listening on {server.host}:{server.port} (Ready to serve {len(valid_paths)} item(s): {names_summary})")
        print(f"{LIGHT_GRAY}Connect from client using: agi-logger tcp receive --host {primary_ip} --port {server.port}{RESET}")
        print(f"\n{LIGHT_GRAY}[Listening] Press 'm' for main menu | 'q' to quit | or wait for connection...{RESET}\n")

        fd = None
        old_settings = None
        if sys.stdin.isatty():
            try:
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                fd = None
                old_settings = None

        try:
            while True:
                # Check for keyboard inputs 'm' or 'q'
                if fd is not None:
                    readable, _, _ = select.select([sys.stdin], [], [], 0.0)
                    if readable:
                        char = sys.stdin.read(1)
                        if char.lower() == "m":
                            print("\nReturning to main menu...")
                            return "menu"
                        elif char.lower() in ("q", "\x03"):
                            print("\nExiting...")
                            return "exit"

                try:
                    client_socket, addr = sock.accept()
                except socket.timeout:
                    continue

                client_socket.settimeout(60.0)
                # Temporarily restore terminal during active socket transfer
                if fd is not None and old_settings is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass

                with client_socket:
                    print(f"\nConnected by {addr}")
                    # Protocol handshake: Exchange and verify agi-logger versions
                    _send_line(client_socket, f"AGI_LOGGER_VERSION:{__version__}")
                    client_ver_line = _recv_line(client_socket)
                    _check_version_compatibility(client_ver_line, role="Client")

                    if len(valid_paths) == 1:
                        _send_single_item(client_socket, valid_paths[0])
                    else:
                        batch_header = f"BATCH:{len(valid_paths)}"
                        _send_line(client_socket, batch_header)
                        ack = _recv_line(client_socket)
                        if ack != "READY":
                            print(f"Client rejected batch transfer (ack: {ack}). Disconnecting.")
                            continue

                        for idx, item_path in enumerate(valid_paths, start=1):
                            prefix = f"[{idx}/{len(valid_paths)}] "
                            _send_single_item(client_socket, item_path, item_prefix=prefix)
                            done_ack = _recv_line(client_socket)
                            if done_ack != "ITEM_DONE":
                                print(f"Warning: Unexpected item ACK from client: {done_ack}")

                    print(f"All {len(valid_paths)} item(s) sent successfully to {addr}.")

                if server.once:
                    return "ok"

                # Re-enter cbreak mode for next connection wait
                if fd is not None:
                    try:
                        tty.setcbreak(fd)
                    except Exception:
                        pass
                print(f"\n{LIGHT_GRAY}[Listening] Press 'm' for main menu | 'q' to quit | or wait for next connection...{RESET}\n")

        except KeyboardInterrupt:
            print("\nServer stopped.")
            return "menu"
        finally:
            if fd is not None and old_settings is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

    return "ok"


def _receive_single_item(sock: socket.socket, destination: Path, raw_meta: str, item_prefix: str = "") -> Path:
    if raw_meta.startswith("ERROR"):
        raise TcpTransferError(raw_meta)

    parts = raw_meta.split(":")
    if len(parts) == 3:
        item_type, item_name, size_str = parts
        is_dir = (item_type == "DIR")
        file_size = int(size_str)
    elif len(parts) == 2:
        item_name, size_str = parts
        is_dir = False
        file_size = int(size_str)
    else:
        raise TcpTransferError(f"Invalid metadata received: {raw_meta}")

    _send_line(sock, "READY")

    if is_dir:
        reader = _ChunkedSocketReader(sock, file_size, item_name, item_prefix)
        with tarfile.open(fileobj=reader, mode="r|*") as tar:
            tar.extractall(path=str(destination))

        output_path = destination / item_name
        print(f"\n{item_prefix}Directory '{item_name}' successfully received at: {output_path}")
        return output_path
    else:
        output_path = destination / item_name
        print(f"{item_prefix}Receiving file '{item_name}' ({file_size} bytes)...")
        received = 0
        with output_path.open("wb") as handle:
            while received < file_size:
                chunk = sock.recv(min(BUFFER_SIZE, file_size - received))
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                pct = (received / file_size) * 100 if file_size > 0 else 100
                print(f"{item_prefix}Progress: {received}/{file_size} bytes ({pct:.1f}%)", end="\r")

        print(f"\n{item_prefix}File '{item_name}' successfully received at: {output_path}")
        return output_path


def receive_file(client: TcpClientConfig) -> Union[Path, List[Path]]:
    destination = Path(client.destination_path).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(60.0)
        sock.connect((client.host, client.port))

        # Protocol handshake: Exchange and verify agi-logger versions
        server_ver_line = _recv_line(sock)
        _check_version_compatibility(server_ver_line, role="Server")
        _send_line(sock, f"AGI_LOGGER_VERSION:{__version__}")

        raw_meta = _recv_line(sock)
        if raw_meta.startswith("ERROR"):
            raise TcpTransferError(raw_meta)

        if raw_meta.startswith("BATCH:"):
            total_items = int(raw_meta.split(":")[1])
            print(f"Server is sending a batch of {total_items} items.")
            _send_line(sock, "READY")
            received_items: List[Path] = []

            for i in range(1, total_items + 1):
                item_meta = _recv_line(sock)
                prefix = f"[{i}/{total_items}] "
                out_p = _receive_single_item(sock, destination, item_meta, item_prefix=prefix)
                received_items.append(out_p)
                _send_line(sock, "ITEM_DONE")

            print(f"\nAll {len(received_items)} items successfully received in '{destination}'.")
            return received_items
        else:
            return _receive_single_item(sock, destination, raw_meta)
