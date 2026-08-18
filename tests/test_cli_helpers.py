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
