from __future__ import annotations

import select
import socket
import sys
import tarfile
import tempfile
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
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


def _send_single_item(client_socket: socket.socket, file_path: Path, item_prefix: str = "") -> None:
    temp_tar_path: Optional[Path] = None
    try:
        if file_path.is_dir():
            print(f"{item_prefix}Archiving directory '{file_path.name}' for transfer...")
            temp_tar = tempfile.NamedTemporaryFile(
                suffix=".tar.gz", prefix=f"{file_path.name}_", delete=False
            )
            temp_tar_path = Path(temp_tar.name)
            temp_tar.close()

            with tarfile.open(temp_tar_path, "w:gz") as tar:
                tar.add(str(file_path), arcname=file_path.name)

            transfer_size = temp_tar_path.stat().st_size
            metadata = f"DIR:{file_path.name}:{transfer_size}"
            source_path = temp_tar_path
        else:
            transfer_size = file_path.stat().st_size
            metadata = f"FILE:{file_path.name}:{transfer_size}"
            source_path = file_path

        _send_line(client_socket, metadata)
        ack = _recv_line(client_socket)
        if ack != "READY":
            raise TcpTransferError(f"Client rejected item '{file_path.name}' (ack: {ack})")

        sent = 0
        with source_path.open("rb") as handle:
            while chunk := handle.read(BUFFER_SIZE):
                client_socket.sendall(chunk)
                sent += len(chunk)
                pct = (sent / transfer_size) * 100 if transfer_size > 0 else 100
                print(f"{item_prefix}Sending {file_path.name}: {sent}/{transfer_size} bytes ({pct:.1f}%)", end="\r")

        print(f"\n{item_prefix}Sent '{file_path.name}' ({transfer_size} bytes) successfully.")
    finally:
        if temp_tar_path and temp_tar_path.exists():
            temp_tar_path.unlink()


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
        temp_tar = tempfile.NamedTemporaryFile(
            suffix=".tar.gz", prefix=f"recv_{item_name}_", delete=False
        )
        temp_tar_path = Path(temp_tar.name)
        temp_tar.close()

        print(f"{item_prefix}Receiving directory archive '{item_name}' ({file_size} bytes)...")
        received = 0
        with temp_tar_path.open("wb") as handle:
            while received < file_size:
                chunk = sock.recv(min(BUFFER_SIZE, file_size - received))
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                pct = (received / file_size) * 100 if file_size > 0 else 100
                print(f"{item_prefix}Progress: {received}/{file_size} bytes ({pct:.1f}%)", end="\r")

        print(f"\n{item_prefix}Extracting archive into '{destination}'...")
        with tarfile.open(temp_tar_path, "r:gz") as tar:
            tar.extractall(path=str(destination))
        temp_tar_path.unlink()

        output_path = destination / item_name
        print(f"{item_prefix}Directory '{item_name}' successfully received at: {output_path}")
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
