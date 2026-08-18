import threading
import time
from pathlib import Path
import pytest

from agi_logger.tcp_transfer import (
    TcpClientConfig,
    TcpServerConfig,
    receive_file,
    send_file,
)


def test_tcp_transfer_single_file(tmp_path):
    server_dir = tmp_path / "server_data"
    client_dir = tmp_path / "client_data"
    server_dir.mkdir()
    client_dir.mkdir()

    test_file = server_dir / "sample_log.txt"
    test_content = "Agipix Robot Flight Log Data\nLine 2: status OK\n"
    test_file.write_text(test_content)

    port = 16543
    server_cfg = TcpServerConfig(port=port, file_path=str(test_file), host="127.0.0.1", once=True)
    client_cfg = TcpClientConfig(host="127.0.0.1", port=port, destination_path=str(client_dir))

    server_thread = threading.Thread(target=send_file, args=(server_cfg,))
    server_thread.start()
    time.sleep(0.3)

    received_path = receive_file(client_cfg)
    server_thread.join(timeout=3.0)

    assert isinstance(received_path, Path)
    assert received_path.exists()
    assert received_path.name == "sample_log.txt"
    assert received_path.read_text() == test_content


def test_tcp_transfer_directory_bag(tmp_path):
    server_dir = tmp_path / "server_bags"
    client_dir = tmp_path / "client_bags"
    server_dir.mkdir()
    client_dir.mkdir()

    # Create mock ROS 2 bag directory structure
    bag_folder = server_dir / "agi_log_20260818_120000"
    bag_folder.mkdir()
    (bag_folder / "metadata.yaml").write_text("rosbag2_bagfile_information:\n  version: 5\n")
    (bag_folder / "agi_log_0.mcap").write_bytes(b"\x89MCAP\x30\x00" + b"\x00" * 1024)

    port = 16544
    server_cfg = TcpServerConfig(port=port, file_path=str(bag_folder), host="127.0.0.1", once=True)
    client_cfg = TcpClientConfig(host="127.0.0.1", port=port, destination_path=str(client_dir))

    server_thread = threading.Thread(target=send_file, args=(server_cfg,))
    server_thread.start()
    time.sleep(0.3)

    received_path = receive_file(client_cfg)
    server_thread.join(timeout=3.0)

    assert isinstance(received_path, Path)
    assert received_path.exists()
    assert received_path.is_dir()
    assert (received_path / "metadata.yaml").exists()
    assert (received_path / "agi_log_0.mcap").exists()
    assert (received_path / "metadata.yaml").read_text() == "rosbag2_bagfile_information:\n  version: 5\n"
    assert len((received_path / "agi_log_0.mcap").read_bytes()) == 1031


def test_tcp_transfer_multiple_bags_batch(tmp_path):
    server_dir = tmp_path / "server_multi_bags"
    client_dir = tmp_path / "client_multi_bags"
    server_dir.mkdir()
    client_dir.mkdir()

    bag1 = server_dir / "bag_flight_1"
    bag1.mkdir()
    (bag1 / "metadata.yaml").write_text("bag1_info")

    bag2 = server_dir / "bag_flight_2"
    bag2.mkdir()
    (bag2 / "metadata.yaml").write_text("bag2_info")

    bag3 = server_dir / "bag_flight_3"
    bag3.mkdir()
    (bag3 / "metadata.yaml").write_text("bag3_info")

    port = 16545
    server_cfg = TcpServerConfig(
        port=port,
        file_paths=[str(bag1), str(bag2), str(bag3)],
        host="127.0.0.1",
        once=True,
    )
    client_cfg = TcpClientConfig(host="127.0.0.1", port=port, destination_path=str(client_dir))

    server_thread = threading.Thread(target=send_file, args=(server_cfg,))
    server_thread.start()
    time.sleep(0.3)

    received_items = receive_file(client_cfg)
    server_thread.join(timeout=3.0)

    assert isinstance(received_items, list)
    assert len(received_items) == 3

    assert (client_dir / "bag_flight_1" / "metadata.yaml").exists()
    assert (client_dir / "bag_flight_1" / "metadata.yaml").read_text() == "bag1_info"
    assert (client_dir / "bag_flight_2" / "metadata.yaml").exists()
    assert (client_dir / "bag_flight_2" / "metadata.yaml").read_text() == "bag2_info"
    assert (client_dir / "bag_flight_3" / "metadata.yaml").exists()
    assert (client_dir / "bag_flight_3" / "metadata.yaml").read_text() == "bag3_info"
