from pathlib import Path
import pytest

from agi_logger.cli import (
    _format_display_value,
    _parse_value,
    build_parser,
)


def test_parse_values():
    assert _parse_value("true") is True
    assert _parse_value("False") is False
    assert _parse_value("null") is None
    assert _parse_value("123") == 123
    assert _parse_value("12.34") == 12.34
    assert _parse_value("hello") == "hello"

    # List parsing
    assert _parse_value("/tf, /clock", existing_value=["/old"]) == ["/tf", "/clock"]
    assert _parse_value("['/tf', '/clock']") == ["/tf", "/clock"]


def test_format_display_value():
    assert _format_display_value(10) == "10"
    assert _format_display_value(True) == "True"
    assert _format_display_value(["/a", "/b"]) == "[/a, /b]"
    assert "4 items" in _format_display_value(["/a", "/b", "/c", "/d"])


def test_build_parser():
    parser = build_parser()
    args = parser.parse_args(["record", "start", "--background"])
    assert args.command == "record"
    assert args.record_cmd == "start"
    assert args.background is True

    tcp_args = parser.parse_args(["tcp", "send", "--file", "/tmp/bag", "--port", "7000"])
    assert tcp_args.command == "tcp"
    assert tcp_args.tcp_cmd == "send"
    assert tcp_args.file == ["/tmp/bag"]
    assert tcp_args.port == 7000

    # Test playback --clock defaults and flags
    play_args = parser.parse_args(["play"])
    assert play_args.clock is True
    assert play_args.topics is None

    play_no_clock_args = parser.parse_args(["play", "--no-clock", "--topics", "/tf", "/clock"])
    assert play_no_clock_args.clock is False
    assert play_no_clock_args.topics == ["/tf", "/clock"]

    bag_play_args = parser.parse_args(["bag", "play", "/path/to/bag", "--topics", "/drone0/livox/lidar"])
    assert bag_play_args.clock is True
    assert bag_play_args.topics == ["/drone0/livox/lidar"]

    bag_play_no_clock_args = parser.parse_args(["bag", "play", "/path/to/bag", "--no-clock"])
    assert bag_play_no_clock_args.clock is False


def test_bag_play_command_build():
    from unittest.mock import patch
    from agi_logger.cli import _bag_play
    import argparse

    with patch("agi_logger.cli._run_command", return_value=0) as mock_run:
        # Default clock True
        args = argparse.Namespace(bag="/path/to/bag", clock=True, rate=1.0, loop=False, read_ahead_queue_size=10000, topics=None)
        _bag_play(args)
        mock_run.assert_called_with(["ros2", "bag", "play", "/path/to/bag", "--clock", "--rate", "1.0", "--read-ahead-queue-size", "10000"])

        # Clock False and topics filter
        args_topics = argparse.Namespace(bag="/path/to/bag", clock=False, rate=None, loop=True, read_ahead_queue_size=5000, topics=["/tf", "/imu"])
        _bag_play(args_topics)
        mock_run.assert_called_with(["ros2", "bag", "play", "/path/to/bag", "--topics", "/tf", "/imu", "--loop", "--read-ahead-queue-size", "5000"])


def test_get_bag_topics(tmp_path):
    from agi_logger.cli import _get_bag_topics

    bag_dir = tmp_path / "test_bag"
    bag_dir.mkdir()
    metadata = bag_dir / "metadata.yaml"
    metadata.write_text("""
rosbag2_bagfile_information:
  topics_with_message_count:
    - topic_metadata:
        name: /drone0/livox/lidar
        type: sensor_msgs/msg/PointCloud2
      message_count: 1500
    - topic_metadata:
        name: /drone0/px4_imu
        type: sensor_msgs/msg/Imu
      message_count: 12000
""")

    topics = _get_bag_topics(bag_dir)
    assert len(topics) == 2
    assert topics[0] == ("/drone0/livox/lidar", "sensor_msgs/msg/PointCloud2", 1500)
    assert topics[1] == ("/drone0/px4_imu", "sensor_msgs/msg/Imu", 12000)


def test_load_topics_catalogue(tmp_path):
    from agi_logger.cli import _load_topics_catalogue

    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("dummy")
    topics_file = tmp_path / "topics_of_interest.yaml"
    topics_file.write_text(
        "topics:\n  mode:\n    name: /fmu/out/vehicle_control_mode\n    type: px4_msgs/msg/VehicleControlMode\n"
    )

    catalogue = _load_topics_catalogue(cfg_file, ["/clock", "/tf"])
    names = [c[0] for c in catalogue]
    assert "/fmu/out/vehicle_control_mode" in names
    assert "/clock" in names
    assert "/tf" in names
