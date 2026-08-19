import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from agi_logger.system_monitor import (
    CRITICAL_RAM_GB,
    CRITICAL_STORAGE_GB,
    WARN_RAM_GB,
    WARN_STORAGE_GB,
    check_system_resources,
    get_system_resources,
)
from agi_logger.cli import run_manual_recording, _record_start
from agi_logger.logging_manager import RecordingState


def test_system_monitor_ok(tmp_path, capsys):
    with patch("agi_logger.system_monitor.get_system_resources") as mock_res:
        mock_res.return_value = (16.0, 32.0, 50.0, 100.0, 500.0, 20.0)
        status, metrics = check_system_resources(str(tmp_path), print_output=True)
        assert status == "ok"
        assert metrics[0] == 16.0
        captured = capsys.readouterr().out
        assert "[SYSTEM MONITOR]" in captured


def test_system_monitor_warning(tmp_path, capsys):
    with patch("agi_logger.system_monitor.get_system_resources") as mock_res:
        # Storage < 10.0 GB triggers warning
        mock_res.return_value = (16.0, 32.0, 50.0, 8.0, 500.0, 1.6)
        status, metrics = check_system_resources(str(tmp_path), print_output=True)
        assert status == "warning"
        captured = capsys.readouterr().out
        assert "[LOW RESOURCE WARNING]" in captured


def test_system_monitor_critical(tmp_path, capsys):
    with patch("agi_logger.system_monitor.get_system_resources") as mock_res:
        # RAM < 0.3 GB triggers critical
        mock_res.return_value = (0.2, 32.0, 0.6, 100.0, 500.0, 20.0)
        status, metrics = check_system_resources(str(tmp_path), print_output=True)
        assert status == "critical"
        captured = capsys.readouterr().out
        assert "[CRITICAL RESOURCE ALERT]" in captured


def test_manual_recording_already_recording(tmp_path, capsys):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    with patch("agi_logger.cli.RecorderManager") as mock_cls:
        mock_mgr = mock_cls.return_value
        mock_mgr.is_recording.return_value = True

        result = run_manual_recording(cfg_file)
        assert result == "exit"
        captured = capsys.readouterr().out
        assert "Recording already active" in captured


def test_manual_recording_press_q(tmp_path):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    with patch("agi_logger.cli.RecorderManager") as mock_cls, \
         patch("agi_logger.cli.sys.stdin.isatty", return_value=True), \
         patch("agi_logger.cli.termios.tcgetattr", return_value=[]), \
         patch("agi_logger.cli.termios.tcsetattr"), \
         patch("agi_logger.cli.tty.setcbreak"), \
         patch("agi_logger.cli.select.select", return_value=([True], [], [])), \
         patch("agi_logger.cli.sys.stdin.read", return_value="q"):
        
        mock_mgr = mock_cls.return_value
        mock_mgr.is_recording.side_effect = [False, True, True, False]
        mock_mgr.start_recording.return_value = RecordingState(
            pid=12345,
            bag_name="agi_log_test",
            bag_path="/tmp/test_bags/agi_log_test",
            start_time=100.0,
            command=["ros2", "bag", "record"],
        )

        result = run_manual_recording(cfg_file)
        assert result == "exit"
        mock_mgr.stop_recording.assert_called()


def test_manual_recording_press_m(tmp_path):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    with patch("agi_logger.cli.RecorderManager") as mock_cls, \
         patch("agi_logger.cli.sys.stdin.isatty", return_value=True), \
         patch("agi_logger.cli.termios.tcgetattr", return_value=[]), \
         patch("agi_logger.cli.termios.tcsetattr"), \
         patch("agi_logger.cli.tty.setcbreak"), \
         patch("agi_logger.cli.select.select", return_value=([True], [], [])), \
         patch("agi_logger.cli.sys.stdin.read", return_value="m"):
        
        mock_mgr = mock_cls.return_value
        mock_mgr.is_recording.side_effect = [False, True, True, False]
        mock_mgr.start_recording.return_value = RecordingState(
            pid=12345,
            bag_name="agi_log_test",
            bag_path="/tmp/test_bags/agi_log_test",
            start_time=100.0,
            command=["ros2", "bag", "record"],
        )

        result = run_manual_recording(cfg_file)
        assert result == "menu"
        mock_mgr.stop_recording.assert_called()


def test_manual_recording_critical_resource_stop(tmp_path, capsys):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    with patch("agi_logger.cli.RecorderManager") as mock_cls, \
         patch("agi_logger.cli.sys.stdin.isatty", return_value=False), \
         patch("agi_logger.cli.time.time") as mock_time, \
         patch("agi_logger.cli.check_system_resources") as mock_check:
        
        mock_mgr = mock_cls.return_value
        mock_mgr.is_recording.side_effect = [False, True, True, False]
        mock_mgr.start_recording.return_value = RecordingState(
            pid=12345,
            bag_name="agi_log_test",
            bag_path="/tmp/test_bags/agi_log_test",
            start_time=100.0,
            command=["ros2", "bag", "record"],
        )
        mock_time.side_effect = [100.0, 115.0, 115.0, 115.0, 115.0]
        mock_check.return_value = ("critical", (0.1, 32.0, 0.3, 100.0, 500.0, 20.0))

        result = run_manual_recording(cfg_file)
        assert result == "exit"
        mock_mgr.stop_recording.assert_called()
        captured = capsys.readouterr().out
        assert "Emergency stop triggered" in captured


def test_record_start_background(tmp_path):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    args = argparse.Namespace(config=cfg_file, background=True)

    with patch("agi_logger.cli._get_manager") as mock_get_mgr:
        mock_mgr = mock_get_mgr.return_value
        mock_mgr.start_recording.return_value = RecordingState(
            pid=999,
            bag_name="agi_log_bg",
            bag_path="/tmp/test_bags/agi_log_bg",
            start_time=100.0,
            command=["ros2", "bag", "record"],
        )

        code = _record_start(args)
        assert code == 0
        mock_mgr.start_recording.assert_called_with(verbose=True, foreground=False)


def test_record_start_foreground_menu(tmp_path):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger:\n  logger:\n    bag_path: /tmp/test_bags\n")

    args = argparse.Namespace(config=cfg_file, background=False)

    with patch("agi_logger.cli.run_manual_recording", return_value="menu") as mock_run, \
         patch("agi_logger.cli._interactive_menu", return_value=0) as mock_menu:
        
        code = _record_start(args)
        assert code == 0
        mock_run.assert_called_with(cfg_file)
        mock_menu.assert_called()
