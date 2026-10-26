"""Flight-log recorder: one curated, rate-limited MCAP per flight-session profile.

Runs as its own process (started and stopped by the flight session), so nothing in it can block
the flight stack or the session logic:
- ROS callbacks only take the serialized message, apply the per-topic rate limit and push it to a
  bounded queue. When the queue is full the message is dropped and counted; the callback never
  waits.
- A writer thread drains the queue into a rosbag2 MCAP writer with small zstd chunks, split into
  complete segments every `segment_s` (10 s). A crash or power loss costs at most the open segment;
  its finished chunks are salvaged when the session is closed (`mcap recover`, `ros2 bag reindex`).
- Topics are subscribed as they appear in the graph, so the recorder can start before the stack.
- Camera topics configured with `thumbnail` are stored only as small JPEG thumbnails
  (`<topic>/thumbnail`) for the log browser.
- `/rosout` can be filtered by level (`min_level`, e.g. 30 = WARN).
- `/agi_logger/health` (std_msgs/String JSON): CPU load, RAM, disk, temperature every
  `health_period_s`.
- Per-topic counts (written, throttled, dropped) go to `<out>/../recorder_<profile>_stats.json`
  every few seconds and at exit.

  python3 -m agi_logger.flight_recorder --out <session>/flight_log --profile flight --config cfg/configs.yaml
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message, serialize_message
from rosidl_runtime_py.utilities import get_message
import rosbag2_py

from .config import load_raw_config
from .system_monitor import get_system_resources

HEALTH_TOPIC = "/agi_logger/health"
QUEUE_MAX = 20000


def topic_specs(profile_cfg: Any, namespace: str) -> List[Dict[str, Any]]:
    """Normalise a profile's topic list: entries are a topic string or a dict with `topic` and options.
    `{ns}` in a topic is replaced by the drone namespace."""
    specs = []
    for entry in (profile_cfg or {}).get("topics", []):
        spec = {"topic": entry} if isinstance(entry, str) else dict(entry)
        spec["topic"] = str(spec["topic"]).replace("{ns}", namespace)
        spec.setdefault("max_hz", 0.0)
        specs.append(spec)
    return specs


def _atomic_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _cpu_temp_c() -> Optional[float]:
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            temps.append(int(zone.read_text().strip()) / 1000.0)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


class FlightRecorder(Node):
    def __init__(self, out_dir: Path, profile: str, specs: List[Dict[str, Any]], health_period_s: float,
                 stats_path: Path, segment_s: float = 10.0, chunk_bytes: int = 32768) -> None:
        super().__init__(f"agi_logger_{profile}_recorder")
        self._specs = {s["topic"]: s for s in specs}
        self._subs: Dict[str, Any] = {}
        self._types: Dict[str, str] = {}
        self._last_rx: Dict[str, float] = {}
        self._stats = collections.defaultdict(lambda: {"written": 0, "throttled": 0, "dropped": 0, "filtered": 0})
        self._queue: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stats_path = stats_path
        self._out_dir = out_dir
        self._profile = profile
        self._health_period = health_period_s
        self._created: set = set()

        # Small zstd chunks and 10 s segments: the MCAP writer keeps a whole chunk in memory, so with the
        # default multi-MB chunks a low-rate flight log would reach the disk only at close. Every finished
        # segment is a complete file; a crash or power loss costs at most the open segment, and its finished
        # chunks are recovered on the next session close (flight_session.repair_logs).
        storage_cfg = out_dir.parent / f".mcap_writer_{profile}.yaml"
        storage_cfg.write_text(f"chunkSize: {chunk_bytes}\ncompression: Zstd\ncompressionLevel: Fast\n")
        self._writer = rosbag2_py.SequentialWriter()
        self._writer.open(
            rosbag2_py.StorageOptions(uri=str(out_dir), storage_id="mcap", storage_config_uri=str(storage_cfg),
                                      max_bagfile_duration=int(segment_s)),
            rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr"),
        )
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
        self._discover()
        self.create_timer(1.0, self._discover)
        self.get_logger().info(f"flight recorder '{profile}' -> {out_dir} ({len(specs)} topics configured)")

    # ------------------------------------------------------------------ subscriptions (ROS thread)
    def _discover(self) -> None:
        graph = dict(self.get_topic_names_and_types())
        for topic, spec in self._specs.items():
            if topic in self._subs or topic not in graph or not graph[topic]:
                continue
            type_name = graph[topic][0]
            try:
                msg_cls = get_message(type_name)
            except (AttributeError, ModuleNotFoundError, ValueError) as exc:
                self.get_logger().warn(f"cannot record {topic} ({type_name}): {exc}")
                self._subs[topic] = None
                continue
            if spec.get("latched"):
                qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                                 history=HistoryPolicy.KEEP_LAST, depth=int(spec.get("depth", 10)))
            else:
                qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE,
                                 history=HistoryPolicy.KEEP_LAST, depth=int(spec.get("depth", 50)))
            self._types[topic] = type_name
            self._subs[topic] = self.create_subscription(
                msg_cls, topic, lambda data, t=topic: self._on_msg(t, data), qos, raw=True)
            self.get_logger().info(f"recording {topic} [{type_name}]")

    def _on_msg(self, topic: str, data: bytes) -> None:
        now = time.time()
        spec = self._specs[topic]
        period = 1.0 / spec["max_hz"] if spec.get("max_hz") else 0.0
        if spec.get("thumbnail"):
            period = max(period, 1.0 / float(spec["thumbnail"].get("max_hz", 1.0)))
        if period and now - self._last_rx.get(topic, 0.0) < period:
            self._stats[topic]["throttled"] += 1
            return
        self._last_rx[topic] = now
        with self._lock:
            if len(self._queue) >= QUEUE_MAX:
                self._stats[topic]["dropped"] += 1
                return
            self._queue.append((topic, bytes(data), int(now * 1e9)))

    # ------------------------------------------------------------------ writer thread
    def _ensure_topic(self, name: str, type_name: str) -> None:
        if name not in self._created:
            self._writer.create_topic(rosbag2_py.TopicMetadata(name=name, type=type_name, serialization_format="cdr"))
            self._created.add(name)

    def _write(self, topic: str, data: bytes, stamp_ns: int) -> None:
        spec = self._specs[topic]
        type_name = self._types[topic]
        if spec.get("thumbnail"):
            thumb = self._thumbnail(data, spec["thumbnail"])
            if thumb is None:
                self._stats[topic]["filtered"] += 1
                return
            topic, data = topic.rstrip("/") + "/thumbnail", thumb
            type_name = "sensor_msgs/msg/CompressedImage"
        elif "min_level" in spec and type_name == "rcl_interfaces/msg/Log":
            msg = deserialize_message(data, get_message(type_name))
            if msg.level < int(spec["min_level"]):
                self._stats[spec["topic"]]["filtered"] += 1
                return
        self._ensure_topic(topic, type_name)
        self._writer.write(topic, data, stamp_ns)
        self._stats[spec["topic"]]["written"] += 1

    @staticmethod
    def _thumbnail(data: bytes, cfg: Dict[str, Any]) -> Optional[bytes]:
        import cv2
        import numpy as np
        from sensor_msgs.msg import CompressedImage
        msg = deserialize_message(data, CompressedImage)
        img = cv2.imdecode(np.frombuffer(bytes(msg.data), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        width = int(cfg.get("width", 320))
        h, w = img.shape[:2]
        if w > width:
            img = cv2.resize(img, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(cfg.get("quality", 70))])
        if not ok:
            return None
        out = CompressedImage()
        out.header = msg.header
        out.format = "jpeg"
        out.data = buf.tobytes()
        return serialize_message(out)

    def _health(self) -> bytes:
        from std_msgs.msg import String
        free_ram, total_ram, ram_pct, free_disk, total_disk, disk_pct = get_system_resources(str(self._out_dir))
        load1, load5, _ = os.getloadavg()
        with self._lock:
            qlen = len(self._queue)
        payload = dict(t=time.time(), load1=load1, load5=load5, ncpu=os.cpu_count(), ram_free_gb=free_ram,
                       ram_pct=ram_pct, disk_free_gb=free_disk, disk_pct=disk_pct, temp_c=_cpu_temp_c(), queue=qlen)
        return serialize_message(String(data=json.dumps(payload)))

    def _writer_loop(self) -> None:
        next_health = 0.0
        next_stats = time.time() + 5.0
        while True:
            item = None
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
            now = time.time()
            if item is not None:
                try:
                    self._write(*item)
                except Exception as exc:  # a bad message must not stop the log
                    self._stats[item[0]]["dropped"] += 1
                    self.get_logger().warn(f"write failed on {item[0]}: {exc}")
            if self._health_period > 0 and now >= next_health:
                try:
                    self._ensure_topic(HEALTH_TOPIC, "std_msgs/msg/String")
                    self._writer.write(HEALTH_TOPIC, self._health(), int(now * 1e9))
                except Exception as exc:
                    self.get_logger().warn(f"health write failed: {exc}")
                next_health = now + self._health_period
            if now >= next_stats:
                self.dump_stats()
                next_stats = now + 5.0
            if item is None:
                if self._stop.is_set():
                    break
                time.sleep(0.01)

    # ------------------------------------------------------------------ shutdown
    def dump_stats(self, final: bool = False) -> None:
        stats = {t: dict(v) for t, v in self._stats.items()}
        _atomic_json(self._stats_path, dict(profile=self._profile, out=str(self._out_dir), final=final,
                                            updated=time.time(), subscribed=sorted(t for t, s in self._subs.items() if s),
                                            missing=sorted(t for t in self._specs if t not in self._subs),
                                            topics=stats))

    def close(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        self._writer_thread.join(timeout=timeout_s)
        del self._writer  # closes the MCAP (footer, index)
        self.dump_stats(final=True)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="output bag directory (created by the writer)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--namespace", default="drone0")
    a = ap.parse_args(argv)
    try:
        os.nice(10)
    except OSError:
        pass
    cfg = load_raw_config(Path(a.config))["agi_logger"].get("flight_session", {})
    profile_cfg = cfg.get("profiles", {}).get(a.profile)
    if profile_cfg is None:
        raise SystemExit(f"unknown flight-session profile '{a.profile}'")
    out = Path(a.out)
    stats = out.parent / f"recorder_{a.profile}_stats.json"
    rclpy.init()
    node = FlightRecorder(out, a.profile, topic_specs(profile_cfg, a.namespace),
                          float(cfg.get("health_period_s", 2.0)), stats,
                          segment_s=float(cfg.get("segment_s", 10.0)), chunk_bytes=int(cfg.get("chunk_bytes", 32768)))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
