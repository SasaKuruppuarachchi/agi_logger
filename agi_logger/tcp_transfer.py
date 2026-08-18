from __future__ import annotations

import socket
import tarfile
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


BUFFER_SIZE = 1024 * 64


@dataclass
class TcpServerConfig:
    port: int
    file_path: str
    host: str = "0.0.0.0"


@dataclass
class TcpClientConfig:
    host: str
    port: int
    destination_path: str


class TcpTransferError(RuntimeError):
    pass


def send_file(server: TcpServerConfig) -> None:
    file_path = Path(server.file_path).expanduser().resolve()
    if not file_path.exists():
        raise TcpTransferError(f"Path not found: {file_path}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((server.host, server.port))
        sock.listen(1)
        print(f"Server listening on {server.host}:{server.port} (Ready to serve '{file_path.name}')")

        try:
            while True:
                client_socket, addr = sock.accept()
                with client_socket:
                    print(f"Connected by {addr}")
                    temp_tar_path: Optional[Path] = None
                    try:
                        if file_path.is_dir():
                            print(f"Archiving directory '{file_path.name}' for transfer...")
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

                        client_socket.sendall(metadata.encode())
                        ack = client_socket.recv(1024).decode()
                        if ack != "READY":
                            print("Client not ready. Disconnecting.")
                            continue

                        sent = 0
                        with source_path.open("rb") as handle:
                            while chunk := handle.read(BUFFER_SIZE):
                                client_socket.sendall(chunk)
                                sent += len(chunk)
                                pct = (sent / transfer_size) * 100 if transfer_size > 0 else 100
                                print(f"Sending {file_path.name}: {sent}/{transfer_size} bytes ({pct:.1f}%)", end="\r")

                        print(f"\n{file_path.name} sent successfully to {addr}.")
                    finally:
                        if temp_tar_path and temp_tar_path.exists():
                            temp_tar_path.unlink()
        except KeyboardInterrupt:
            print("\nServer stopped.")


def receive_file(client: TcpClientConfig) -> Path:
    destination = Path(client.destination_path).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((client.host, client.port))
        raw_meta = sock.recv(1024).decode()
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

        sock.sendall(b"READY")

        if is_dir:
            temp_tar = tempfile.NamedTemporaryFile(
                suffix=".tar.gz", prefix=f"recv_{item_name}_", delete=False
            )
            temp_tar_path = Path(temp_tar.name)
            temp_tar.close()

            print(f"Receiving directory archive '{item_name}' ({file_size} bytes)...")
            received = 0
            with temp_tar_path.open("wb") as handle:
                while received < file_size:
                    chunk = sock.recv(min(BUFFER_SIZE, file_size - received))
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    pct = (received / file_size) * 100 if file_size > 0 else 100
                    print(f"Progress: {received}/{file_size} bytes ({pct:.1f}%)", end="\r")

            print(f"\nExtracting archive into '{destination}'...")
            with tarfile.open(temp_tar_path, "r:gz") as tar:
                tar.extractall(path=str(destination))
            temp_tar_path.unlink()

            output_path = destination / item_name
            print(f"Directory '{item_name}' successfully received at: {output_path}")
            return output_path
        else:
            output_path = destination / item_name
            print(f"Receiving file '{item_name}' ({file_size} bytes)...")
            received = 0
            with output_path.open("wb") as handle:
                while received < file_size:
                    chunk = sock.recv(min(BUFFER_SIZE, file_size - received))
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    pct = (received / file_size) * 100 if file_size > 0 else 100
                    print(f"Progress: {received}/{file_size} bytes ({pct:.1f}%)", end="\r")

            print(f"\nFile '{item_name}' successfully received at: {output_path}")
            return output_path
