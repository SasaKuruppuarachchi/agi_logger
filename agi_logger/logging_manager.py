from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import getpass
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import resolve_logger_paths


STATE_DIR = Path.home() / ".agi_logger"
STATE_FILE = STATE_DIR / "recording_state.json"


@dataclass
class RecordingState:
    pid: int
    bag_name: str
    bag_path: str
    start_time: float
    command: List[str]


class RecorderManager:
    def __init__(self, config: Dict[str, Any], config_path: Path) -> None:
        self._config = config
        self._config_path = config_path
        self._logger_cfg = resolve_logger_paths(config, config_path)

    def is_recording(self) -> bool:
        state = self._read_state()
        if not state or state.pid <= 0:
            return False
        try:
            os.kill(state.pid, 0)
        except OSError:
            self._clear_state()
            return False
        return True

    def start_recording(self, verbose: bool = False, foreground: bool = True) -> RecordingState:
        if self.is_recording():
            raise RuntimeError("Recording already active")

        bag_path = Path(self._logger_cfg["bag_path"]).expanduser()
        bag_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"agi_log_{timestamp}"
        extra_name = str(self._logger_cfg.get("name", "")).strip()
        bag_name = f"{base_name}_{extra_name}" if extra_name else base_name
        full_bag_path = bag_path / bag_name

        cmd = self._build_command(str(full_bag_path))

        duration_min = float(self._logger_cfg.get("duration", 0) or 0)
        max_bag_size = float(self._logger_cfg.get("max_bag_size", 0) or 0)

        if verbose:
            print("=======================================")
            print("Starting new recording:")
            print(f"  Bag path   : {full_bag_path}")
            print(
                "  Duration   : "
                + (f"{duration_min} minutes" if duration_min > 0 else "unlimited")
            )
            print(f"  Topics     : {', '.join(self._logger_cfg.get('topics', []))}")
            print(f"  MCAP       : {'enabled' if self._logger_cfg.get('mcap') else 'disabled'}")
            print(
                "  Compression: "
                + ("enabled" if self._logger_cfg.get("compress") else "disabled")
            )
            print(
                "  Max size   : "
                + (f"{max_bag_size} GB" if max_bag_size > 0 else "unlimited")
            )
            print("  Command    : " + " ".join(cmd))
            print("=======================================")

        if foreground:
            state = RecordingState(
                pid=0,
                bag_name=bag_name,
                bag_path=str(full_bag_path),
                start_time=time.time(),
                command=cmd,
            )
            try:
                subprocess.run(cmd, check=False)
            except KeyboardInterrupt:
                pass
            self._write_metadata(state)
            return state

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        state = RecordingState(
            pid=process.pid,
            bag_name=bag_name,
            bag_path=str(full_bag_path),
            start_time=time.time(),
            command=cmd,
        )
        self._write_state(state)

        time.sleep(0.5)
        if process.poll() is not None:
            self._clear_state()
            raise RuntimeError(
                "Recorder exited early. Ensure ROS 2 is sourced and topics exist."
            )

        if duration_min > 0:
            timer = threading.Timer(duration_min * 60, self.stop_recording)
            timer.daemon = True
            timer.start()

        return state

    def stop_recording(self) -> None:
        state = self._read_state()
        if not state:
            raise RuntimeError("No active recording found")

        try:
            try:
                pgid = os.getpgid(state.pid)
                os.killpg(pgid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                os.kill(state.pid, signal.SIGINT)
        except OSError:
            self._clear_state()
            return

        for _ in range(60):
            if not self.is_recording():
                break
            time.sleep(0.5)
        else:
            try:
                pgid = os.getpgid(state.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

        self._write_metadata(state)
        self._clear_state()

    def status(self) -> Optional[RecordingState]:
        return self._read_state()

    def _build_command(self, full_bag_path: str) -> List[str]:
        raw_topics = self._logger_cfg.get("topics", [])
        if isinstance(raw_topics, str):
            topics = [t.strip() for t in raw_topics.split(",") if t.strip()]
        elif isinstance(raw_topics, (list, tuple)):
            topics = [str(t).strip() for t in raw_topics if str(t).strip()]
        else:
            topics = []

        if not topics:
            raise RuntimeError("No topics configured for recording")

        cmd = ["ros2", "bag", "record", "-o", full_bag_path]

        duration_min = float(self._logger_cfg.get("duration", 0) or 0)
        if duration_min > 0:
            duration_sec = int(duration_min * 60)
            cmd += ["-d", str(duration_sec)]

        if bool(self._logger_cfg.get("mcap", False)):
            cmd += ["--storage", "mcap"]
            if bool(self._logger_cfg.get("compress", False)):
                cmd += ["--compression-mode", "file", "--compression-format", "zstd"]

        if bool(self._logger_cfg.get("override_qos", False)):
            qos_file = self._logger_cfg.get("qos_settings")
            if qos_file:
                qos_path = Path(qos_file)
                if qos_path.exists() and qos_path.stat().st_size > 0:
                    cmd += ["--qos-profile-overrides-path", str(qos_path)]

        max_bag_size = float(self._logger_cfg.get("max_bag_size", 0) or 0)
        if max_bag_size > 0:
            max_bytes = int(max_bag_size * 1024 * 1024 * 1024)
            cmd += ["--max-bag-size", str(max_bytes)]

        cmd += list(topics)
        return cmd

    def _write_metadata(self, state: RecordingState) -> None:
        metadata_path = Path(state.bag_path) / "metadata.txt"
        cfg = self._logger_cfg
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        content = [
            f"bag_name: {state.bag_name}",
            f"bag_path: {cfg.get('bag_path')}",
            f"date: {datetime.now().isoformat()}",
            f"hostname: {os.uname().nodename}",
            f"user: {getpass.getuser()}",
            f"ros_distro: {os.environ.get('ROS_DISTRO', 'unknown')}",
            f"kernel: {os.uname().release}",
            "topics:",
        ]
        for topic in cfg.get("topics", []):
            content.append(f"  - {topic}")
        content.extend(
            [
                f"storage: {'MCAP' if cfg.get('mcap') else 'default'}",
                f"compression: {'enabled' if cfg.get('compress') else 'disabled'}",
                f"max_bag_size: {cfg.get('max_bag_size', 'unlimited')} GB",
                f"duration: {cfg.get('duration', 'unlimited')} minutes",
                f"qos_override_file: {cfg.get('qos_settings', 'none')}",
            ]
        )
        metadata_path.write_text("\n".join(content) + "\n", encoding="utf-8")

    def _read_state(self) -> Optional[RecordingState]:
        if not STATE_FILE.exists():
            return None
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return RecordingState(
                pid=int(data["pid"]),
                bag_name=data["bag_name"],
                bag_path=data["bag_path"],
                start_time=float(data["start_time"]),
                command=list(data["command"]),
            )
        except Exception:
            return None

    def _write_state(self, state: RecordingState) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": state.pid,
            "bag_name": state.bag_name,
            "bag_path": state.bag_path,
            "start_time": state.start_time,
            "command": state.command,
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _clear_state(self) -> None:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
