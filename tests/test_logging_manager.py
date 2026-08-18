from pathlib import Path
import pytest

from agi_logger.logging_manager import RecorderManager, RecordingState


def test_build_command_basic(tmp_path):
    config = {
        "agi_logger": {
            "logger": {
                "bag_path": str(tmp_path / "bags"),
                "topics": ["/tf", "/clock"],
                "duration": 2,
                "mcap": True,
                "compress": True,
                "max_bag_size": 1.5,
                "override_qos": False,
            }
        }
    }
    cfg_path = tmp_path / "configs.yaml"
    manager = RecorderManager(config, cfg_path)
    cmd = manager._build_command(str(tmp_path / "bags" / "test_bag"))

    assert cmd[0:3] == ["ros2", "bag", "record"]
    assert "-o" in cmd
    assert "-d" in cmd
    assert cmd[cmd.index("-d") + 1] == "120"  # 2 minutes = 120 seconds
    assert "--storage" in cmd
    assert cmd[cmd.index("--storage") + 1] == "mcap"
    assert "--compression-mode" in cmd
    assert "--max-bag-size" in cmd
    assert "--max-cache-size" in cmd
    assert "/tf" in cmd
    assert "/clock" in cmd


def test_build_command_comma_separated_topics(tmp_path):
    config = {
        "agi_logger": {
            "logger": {
                "bag_path": str(tmp_path / "bags"),
                "topics": "/drone0/imu, /drone0/lidar",
                "duration": 0,
                "mcap": False,
            }
        }
    }
    cfg_path = tmp_path / "configs.yaml"
    manager = RecorderManager(config, cfg_path)
    cmd = manager._build_command(str(tmp_path / "bags" / "test_bag"))

    assert "/drone0/imu" in cmd
    assert "/drone0/lidar" in cmd
    assert "-d" not in cmd  # duration 0 means unlimited


def test_write_metadata(tmp_path):
    bag_dir = tmp_path / "sample_bag_2026"
    bag_dir.mkdir(parents=True)

    config = {
        "agi_logger": {
            "logger": {
                "bag_path": str(tmp_path),
                "topics": ["/clock", "/tf"],
                "mcap": True,
                "compress": False,
            }
        }
    }
    cfg_path = tmp_path / "configs.yaml"
    manager = RecorderManager(config, cfg_path)

    state = RecordingState(
        pid=1234,
        bag_name="sample_bag_2026",
        bag_path=str(bag_dir),
        start_time=100.0,
        command=["ros2", "bag", "record"],
    )
    manager._write_metadata(state)

    meta_file = bag_dir / "metadata.txt"
    assert meta_file.exists()
    content = meta_file.read_text()
    assert "bag_name: sample_bag_2026" in content
    assert "storage: MCAP" in content
    assert "/clock" in content
