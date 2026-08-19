from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
except ImportError as exc:  # pragma: no cover - optional dependency
    raise RuntimeError(
        "ROS 2 dependencies not available. Ensure rclpy is installed."
    ) from exc

try:
    from px4_msgs.msg import VehicleStatus
except ImportError:
    VehicleStatus = None

try:
    from as2_msgs.msg import PlatformInfo
except ImportError:
    PlatformInfo = None


from .config import load_raw_config, resolve_logger_paths
from .logging_manager import RecorderManager
from .system_monitor import (
    BOLD,
    CRITICAL_RAM_GB,
    CRITICAL_STORAGE_GB,
    CYAN,
    GREEN,
    LIGHT_GRAY,
    RED,
    RESET,
    WARN_RAM_GB,
    WARN_STORAGE_GB,
    YELLOW,
    check_system_resources,
    get_system_resources,
)


class AutoStartLoggerNode(Node):
    def __init__(self, config_path: Path) -> None:
        super().__init__("agi_logger_autostart")
        self._config_path = config_path
        self._config = load_raw_config(config_path)
        self._logger_cfg = resolve_logger_paths(self._config, config_path)
        self._auto_start = bool(self._logger_cfg.get("auto_start", False))
        self._behavior = str(self._logger_cfg.get("auto_start_behavior", "toggle_arm"))
        self._manager = RecorderManager(self._config, config_path)
        self._last_armed_state: Optional[bool] = None
        self._received_first_msg = False
        self._bag_path = str(self._logger_cfg.get("bag_path", "/workspaces/logging/test_bags"))

        if VehicleStatus is None and PlatformInfo is None:
            raise RuntimeError(
                "Neither px4_msgs nor as2_msgs are available. Please source your ROS 2 overlay workspace."
            )

        topic = str(self._logger_cfg.get("auto_start_topic", "/drone0/platform/info"))

        # Best effort QoS profile for high-rate sensor/telemetry compatibility
        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )

        # Determine message type (PlatformInfo or VehicleStatus)
        msg_type = None
        topic_types_dict = dict(self.get_topic_names_and_types())

        if topic in topic_types_dict:
            types = topic_types_dict[topic]
            if any("PlatformInfo" in t for t in types) and PlatformInfo is not None:
                msg_type = PlatformInfo
            elif any("VehicleStatus" in t for t in types) and VehicleStatus is not None:
                msg_type = VehicleStatus

        if msg_type is None:
            if ("platform" in topic or "info" in topic) and PlatformInfo is not None:
                msg_type = PlatformInfo
            elif VehicleStatus is not None:
                msg_type = VehicleStatus
            elif PlatformInfo is not None:
                msg_type = PlatformInfo

        self._sub = self.create_subscription(
            msg_type,
            topic,
            self._on_status_msg,
            qos_profile,
        )

        # Create periodic 0.1 Hz (every 10.0 seconds) resource monitor timer
        self._monitor_timer = self.create_timer(10.0, self._check_system_resources)

        self.get_logger().info(
            f"AGI logger autostart node initialized on topic '{topic}' [{msg_type.__name__}] "
            f"(auto_start={self._auto_start}, behavior={self._behavior})"
        )
        if not self._auto_start:
            self.get_logger().warn(
                "auto_start is set to false in configs.yaml. Recording will NOT trigger on arming."
            )

    def is_recording_active(self) -> bool:
        return self._manager.is_recording()

    def _check_system_resources(self) -> None:
        if not self._manager.is_recording():
            return

        status, _ = check_system_resources(self._bag_path, print_output=True)
        if status == "critical":
            self.get_logger().error("Critical storage/memory threshold reached. Performing emergency safe stop.")
            try:
                self._manager.stop_recording()
                self._print_disarmed_instructions()
            except Exception as exc:
                self.get_logger().error(f"Error during emergency stop: {exc}")

    def _print_disarmed_instructions(self) -> None:
        print(
            f"\n{BOLD}{YELLOW}[Awaiting Arm]{RESET} "
            f"Press {BOLD}{GREEN}'m'{RESET} for main menu | {BOLD}{RED}'q'{RESET} to quit | or wait for next arm..."
        )

    def _on_status_msg(self, msg: Any) -> None:
        if hasattr(msg, "armed"):
            # as2_msgs/msg/PlatformInfo
            armed = bool(msg.armed)
            state_info = f"armed={armed}, connected={getattr(msg, 'connected', True)}"
        elif hasattr(msg, "arming_state"):
            # px4_msgs/msg/VehicleStatus
            armed_constant = getattr(VehicleStatus, "ARMING_STATE_ARMED", 2)
            armed = (msg.arming_state == armed_constant)
            state_info = f"arming_state={msg.arming_state} (armed={armed})"
        else:
            armed = False
            state_info = str(msg)

        if not self._received_first_msg:
            self._received_first_msg = True
            self.get_logger().info(f"Connected to telemetry stream. Initial state: {state_info}")
            if not armed:
                self._print_disarmed_instructions()

        if not self._auto_start:
            return

        if self._last_armed_state is not None and self._last_armed_state != armed:
            self.get_logger().info(f"Arming transition: {self._last_armed_state} -> {armed} ({state_info})")

        if self._behavior == "toggle_arm":
            if self._last_armed_state is None:
                self._last_armed_state = armed
                if armed and not self._manager.is_recording():
                    self.get_logger().info("Vehicle is currently armed: starting bag recording (background)")
                    try:
                        state = self._manager.start_recording(foreground=False, verbose=True)
                        self.get_logger().info(f"Recording active: {state.bag_name} (PID {state.pid})")
                    except Exception as exc:
                        self.get_logger().error(f"Failed to start recording: {exc}")
                return

            if armed and not self._last_armed_state:
                if not self._manager.is_recording():
                    self.get_logger().info("Vehicle armed: starting bag recording (background)")
                    try:
                        state = self._manager.start_recording(foreground=False, verbose=True)
                        self.get_logger().info(f"Recording active: {state.bag_name} (PID {state.pid})")
                    except Exception as exc:
                        self.get_logger().error(f"Failed to start recording: {exc}")
            elif not armed and self._last_armed_state:
                if self._manager.is_recording():
                    self.get_logger().info("Vehicle disarmed: stopping bag recording")
                    try:
                        self._manager.stop_recording()
                        self.get_logger().info("Recording stopped successfully.")
                        self._print_disarmed_instructions()
                    except Exception as exc:
                        self.get_logger().error(f"Failed to stop recording: {exc}")
        else:
            if armed:
                if not self._manager.is_recording():
                    self.get_logger().info("Vehicle armed: starting bag recording (background)")
                    try:
                        state = self._manager.start_recording(foreground=False, verbose=True)
                        self.get_logger().info(f"Recording active: {state.bag_name} (PID {state.pid})")
                    except Exception as exc:
                        self.get_logger().error(f"Failed to start recording: {exc}")
            else:
                if self._manager.is_recording():
                    self.get_logger().info("Vehicle not armed: stopping bag recording")
                    try:
                        self._manager.stop_recording()
                        self.get_logger().info("Recording stopped successfully.")
                        self._print_disarmed_instructions()
                    except Exception as exc:
                        self.get_logger().error(f"Failed to stop recording: {exc}")

        self._last_armed_state = armed

    def shutdown(self) -> None:
        if self._manager.is_recording():
            self.get_logger().info("Shutting down autostart node: stopping active recording")
            try:
                self._manager.stop_recording()
            except Exception:
                pass


def run_autostart_node(config_path: Path) -> str:
    rclpy.init()
    node = AutoStartLoggerNode(config_path)
    action = "exit"

    is_tty = sys.stdin.isatty()
    old_settings = None
    if is_tty:
        try:
            old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            old_settings = None

    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)

            # Check keyboard input during disarmed state
            if is_tty and not node.is_recording_active():
                readable, _, _ = select.select([sys.stdin], [], [], 0.0)
                if readable:
                    char = sys.stdin.read(1)
                    if char in ("m", "M"):
                        node.get_logger().info("Option 'm' pressed: returning to main menu...")
                        action = "menu"
                        break
                    elif char in ("q", "Q"):
                        node.get_logger().info("Option 'q' pressed: exiting autostart node...")
                        action = "exit"
                        break
    except KeyboardInterrupt:
        action = "exit"
    finally:
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        node.shutdown()
        node.destroy_node()
        rclpy.shutdown()

    return action
