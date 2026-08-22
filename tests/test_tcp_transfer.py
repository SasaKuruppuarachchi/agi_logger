import threading
import time
from pathlib import Path
import pytest

from agi_logger.tcp_transfer import (
    TcpClientConfig,
    TcpServerConfig,
    _check_version_compatibility,
    get_host_ips,
    receive_file,
    send_file,
)


def test_get_host_ips():
    ips = get_host_ips()
    assert isinstance(ips, list)
    assert len(ips) > 0
    for ip in ips:
        assert isinstance(ip, str)
        assert len(ip.split(".")) == 4


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


def test_tcp_transfer_mixed_batch_with_nested_directories(tmp_path):
    server_dir = tmp_path / "server_mixed"
    client_dir = tmp_path / "client_mixed"
    server_dir.mkdir()
    client_dir.mkdir()

    # Nested directory bag
    bag = server_dir / "bag_with_subdirs"
    bag.mkdir()
    (bag / "metadata.yaml").write_text("root_meta")
    sub = bag / "subdir"
    sub.mkdir()
    (sub / "chunk.bin").write_bytes(b"binary_chunk_data" * 100)

    # Standalone file
    file_item = server_dir / "summary.csv"
    file_item.write_text("timestamp,topic,messages\n1,a,10\n")

    port = 16546
    server_cfg = TcpServerConfig(
        port=port,
        file_paths=[str(bag), str(file_item)],
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
    assert len(received_items) == 2

    # Check directory bag
    assert (client_dir / "bag_with_subdirs" / "metadata.yaml").read_text() == "root_meta"
    assert (client_dir / "bag_with_subdirs" / "subdir" / "chunk.bin").read_bytes() == b"binary_chunk_data" * 100

    # Check standalone file
    assert (client_dir / "summary.csv").read_text() == "timestamp,topic,messages\n1,a,10\n"


def test_version_compatibility_check(capsys):
    # Same version -> matching
    res = _check_version_compatibility("AGI_LOGGER_VERSION:1.2.0", role="Server")
    assert res == "1.2.0"
    out = capsys.readouterr().out
    assert "Version mismatch detected" not in out

    # Mismatch version -> warns
    res_mismatch = _check_version_compatibility("AGI_LOGGER_VERSION:1.0.0", role="Server")
    assert res_mismatch == "1.0.0"
    out_mismatch = capsys.readouterr().out
    assert "Version mismatch detected" in out_mismatch
    assert "v1.0.0" in out_mismatch

    # Missing version -> warns
    res_none = _check_version_compatibility("UNKNOWN_HEADER", role="Client")
    assert res_none is None
    out_none = capsys.readouterr().out
    assert "did not report an agi-logger version" in out_none


