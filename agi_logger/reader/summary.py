"""Flight-session summary for the log reader: one compact JSON per session, built from its flight log.

Everything the browser shows comes from here:
- downsampled time series: PX4 and LIOM position, speed, battery, logger health, LIOM solve health, planner timing;
- timelines: armed intervals, PX4 navigation mode, mission phase;
- an event list: arm and disarm, mode and phase changes, vehicle commands and acks, WARN+ rosout, session end;
- camera thumbnails, extracted to JPEG files.

The result is cached in `<session>/.reader/summary.json` (with `thumbs/`) and rebuilt when the flight log is
newer than the cache. Times are seconds since the session start.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SUMMARY_VERSION = 2
MAX_POINTS = 2500
NAV_STATES = {0: "MANUAL", 1: "ALTCTL", 2: "POSCTL", 3: "AUTO_MISSION", 4: "AUTO_LOITER", 5: "AUTO_RTL",
              10: "ACRO", 12: "DESCEND", 13: "TERMINATION", 14: "OFFBOARD", 15: "STAB", 17: "AUTO_TAKEOFF",
              18: "AUTO_LAND", 19: "AUTO_FOLLOW", 20: "AUTO_PRECLAND", 21: "ORBIT", 22: "AUTO_VTOL_TAKEOFF",
              **{23 + i: f"EXTERNAL{1 + i}" for i in range(8)}}  # external (px4-ros2) modes, e.g. the agipix mode
LOG_LEVELS = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
_KV = re.compile(r"(\w+)=(\S*)")


def _downsample(series: Dict[str, List[float]], n: int = MAX_POINTS) -> Dict[str, List[float]]:
    length = len(series.get("t", []))
    if length <= n:
        return series
    step = length / n
    idx = [int(i * step) for i in range(n)]
    return {k: [v[i] for i in idx] for k, v in series.items()}


def _r(x: float, nd: int = 3) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


class _Builder:
    def __init__(self, t0: float) -> None:
        self.t0 = t0
        self.series: Dict[str, Dict[str, List[Any]]] = {}
        self.events: List[Dict[str, Any]] = []
        self.arm: List[List[float]] = []
        self.nav: List[List[Any]] = []
        self.phase: List[List[Any]] = []
        self.thumbs: List[Dict[str, Any]] = []
        self._armed: Optional[bool] = None
        self._nav: Optional[int] = None
        self._phase: Optional[str] = None
        self._last_t = 0.0

    def add(self, name: str, t: float, **vals: Any) -> None:
        s = self.series.setdefault(name, {"t": []})
        s["t"].append(round(t, 3))
        for k, v in vals.items():
            s.setdefault(k, []).append(_r(v))

    def event(self, t: float, level: str, source: str, text: str) -> None:
        self.events.append(dict(t=round(t, 3), level=level, source=source, text=text[:400]))

    # ------------------------------------------------------------------ per topic
    def vehicle_status(self, t: float, m: Any) -> None:
        armed = m.arming_state == 2
        if armed != self._armed:
            if armed:
                self.arm.append([t, None])
            elif self.arm and self.arm[-1][1] is None:
                self.arm[-1][1] = t
            if self._armed is not None or armed:
                self.event(t, "INFO", "px4", "ARMED" if armed else "DISARMED")
            self._armed = armed
        if m.nav_state != self._nav:
            name = NAV_STATES.get(int(m.nav_state), str(m.nav_state))
            self.nav.append([t, name])
            if self._nav is not None:
                self.event(t, "INFO", "px4", f"mode {name}")
            self._nav = m.nav_state
        if getattr(m, "failsafe", False):
            self.event(t, "WARN", "px4", "failsafe active")

    def local_position(self, t: float, m: Any) -> None:
        # PX4 NED -> ENU (x east, y north, z up), right-handed like LIOM's map frame
        self.add("px4_pos", t, x=m.y, y=m.x, z=-m.z)
        self.add("speed", t, v=math.sqrt(m.vx * m.vx + m.vy * m.vy + m.vz * m.vz), vz=-m.vz)

    def liom_odom(self, t: float, m: Any) -> None:
        p = m.pose.pose.position
        self.add("liom_pos", t, x=p.x, y=p.y, z=p.z)

    def battery(self, t: float, m: Any) -> None:
        self.add("battery", t, pct=100.0 * m.remaining if m.remaining >= 0 else None, volt=m.voltage_v)

    def health(self, t: float, m: Any) -> None:
        try:
            h = json.loads(m.data)
        except ValueError:
            return
        self.add("health", t, load1=h.get("load1"), temp=h.get("temp_c"), ram_pct=h.get("ram_pct"),
                 disk_free=h.get("disk_free_gb"), queue=h.get("queue"))

    def solve_diag(self, t: float, m: Any) -> None:
        d = list(m.data)
        if len(d) >= 12:
            # layout of lidarodom_m::SolveDiagnostics::ToVector: score_t, score_r, eig_t[3], eig_r[3], n_deg_t, n_deg_r, ...
            self.add("liom", t, score_t=d[0], score_r=d[1], n_deg=d[8] + d[9])

    def auto_status(self, t: float, m: Any) -> None:
        kv = dict(_KV.findall(m.data))
        phase = kv.get("phase")
        if phase and phase != self._phase:
            self.phase.append([t, phase])
            self.event(t, "INFO", "auto", f"phase {phase}" + (f" ({kv['detail']})" if kv.get("detail") else ""))
            self._phase = phase
        nums = {}
        for k in ("plan_ms", "map_ms", "tick_ms"):
            try:
                nums[k] = float(kv[k])
            except (KeyError, ValueError):
                pass
        if nums:
            self.add("planner", t, **nums)

    def vehicle_command(self, t: float, m: Any) -> None:
        self.event(t, "INFO", "cmd", f"vehicle_command {m.command} p1={m.param1:g} p2={m.param2:g}")

    def command_ack(self, t: float, m: Any) -> None:
        ok = m.result == 0
        self.event(t, "INFO" if ok else "WARN", "cmd", f"ack command {m.command} result {m.result}")

    def rosout(self, t: float, m: Any) -> None:
        level = LOG_LEVELS.get(int(m.level), str(m.level))
        self.event(t, level, m.name, m.msg)

    def land_detected(self, t: float, m: Any) -> None:
        self.add("landed", t, landed=1.0 if m.landed else 0.0)


HANDLERS = {
    "/fmu/out/vehicle_status_v1": "vehicle_status", "/fmu/out/vehicle_status": "vehicle_status",
    "/fmu/out/vehicle_local_position_v1": "local_position", "/fmu/out/vehicle_local_position": "local_position",
    "/fmu/out/battery_status_v1": "battery", "/fmu/out/battery_status": "battery",
    "/agi_logger/health": "health", "/fmu/in/vehicle_command": "vehicle_command",
    "/fmu/out/vehicle_command_ack": "command_ack", "/rosout": "rosout",
    "/fmu/out/vehicle_land_detected": "land_detected",
}
SUFFIX_HANDLERS = {"/agiptive_liom/state/odom": "liom_odom", "/agiptive_liom/solve/diag": "solve_diag",
                   "/auto/status": "auto_status"}


def _handler_for(topic: str) -> Optional[str]:
    if topic in HANDLERS:
        return HANDLERS[topic]
    for suffix, h in SUFFIX_HANDLERS.items():
        if topic.endswith(suffix):
            return h
    return "thumb" if topic.endswith("/thumbnail") else None


def build_summary(session_dir: Path) -> Dict[str, Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    manifest = json.loads((session_dir / "manifest.json").read_text())
    t0 = float(manifest.get("start_time") or 0.0)
    cache = session_dir / ".reader"
    thumbs_dir = cache / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    b = _Builder(t0)
    log_dir = session_dir / "flight_log"
    counts: Dict[str, int] = {}
    if (log_dir / "metadata.yaml").exists():
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(log_dir), storage_id="mcap"),
                    rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr"))
        types = {tm.name: tm.type for tm in reader.get_all_topics_and_types()}
        classes: Dict[str, Any] = {}
        while reader.has_next():
            topic, data, stamp = reader.read_next()
            counts[topic] = counts.get(topic, 0) + 1
            handler = _handler_for(topic)
            if handler is None:
                continue
            if not t0:
                t0 = b.t0 = stamp / 1e9
            t = stamp / 1e9 - t0
            if handler == "thumb":
                name = f"{len(b.thumbs):05d}.jpg"
                msg = deserialize_message(data, get_message(types[topic]))
                (thumbs_dir / name).write_bytes(bytes(msg.data))
                b.thumbs.append(dict(t=round(t, 3), file=name, topic=topic.rsplit("/thumbnail", 1)[0]))
                continue
            cls = classes.get(topic) or classes.setdefault(topic, get_message(types[topic]))
            try:
                getattr(b, handler)(t, deserialize_message(data, cls))
            except Exception:  # one malformed message must not break the summary
                continue
    end_t = float(manifest.get("duration_s") or 0.0)
    if manifest.get("end_reason"):
        b.event(end_t, "INFO" if manifest["end_reason"] == "disarm" else "WARN", "logger",
                f"session ended: {manifest['end_reason']}")
    for iv in b.arm:
        if iv[1] is None:
            iv[1] = end_t
    b.events.sort(key=lambda e: e["t"])
    aligned = _align_xy(b.series.get("liom_pos"), b.series.get("px4_pos"))
    if aligned is not None:
        b.series["liom_aligned"], alignment = aligned
    else:
        alignment = None
    return dict(
        version=SUMMARY_VERSION, built=time.time(), session_id=session_dir.name, t0=t0, duration=end_t,
        series={k: _downsample(v) for k, v in b.series.items()},
        timeline=dict(arm=b.arm, nav=b.nav, phase=b.phase), events=b.events, thumbs=b.thumbs, liom_alignment=alignment,
        topic_counts=counts)


def _align_xy(liom: Optional[Dict[str, List[Any]]], px4: Optional[Dict[str, List[Any]]]):
    """LIOM's map frame starts wherever LIOM initialised; for display, fit the 2D rotation + offset that best maps
    LIOM x/y onto PX4 east/north over the time both exist (Kabsch). What still differs after the fit is drift."""
    if not liom or not px4 or len(liom["t"]) < 10 or len(px4["t"]) < 10:
        return None
    import bisect
    pairs = []
    for i in range(0, len(liom["t"]), max(1, len(liom["t"]) // 2000)):
        t = liom["t"][i]
        j = bisect.bisect_left(px4["t"], t)
        if j < len(px4["t"]) and abs(px4["t"][j] - t) < 0.2 and None not in (liom["x"][i], liom["y"][i], px4["x"][j], px4["y"][j]):
            pairs.append((liom["x"][i], liom["y"][i], px4["x"][j], px4["y"][j]))
    if len(pairs) < 10:
        return None
    n = len(pairs)
    mx, my = sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n
    px, py = sum(p[2] for p in pairs) / n, sum(p[3] for p in pairs) / n
    sxx = sum((a - mx) * (c - px) + (b - my) * (d - py) for a, b, c, d in pairs)
    sxy = sum((a - mx) * (d - py) - (b - my) * (c - px) for a, b, c, d in pairs)
    yaw = math.atan2(sxy, sxx)
    co, si = math.cos(yaw), math.sin(yaw)
    tx, ty = px - (co * mx - si * my), py - (si * mx + co * my)
    out = {"t": liom["t"], "x": [], "y": [], "z": liom["z"]}
    for x, y in zip(liom["x"], liom["y"]):
        out["x"].append(None if x is None else round(co * x - si * y + tx, 3))
        out["y"].append(None if y is None else round(si * x + co * y + ty, 3))
    rms = math.sqrt(sum((co * a - si * b + tx - c) ** 2 + (si * a + co * b + ty - d) ** 2 for a, b, c, d in pairs) / n)
    return out, dict(yaw_deg=round(math.degrees(yaw), 2), tx=round(tx, 3), ty=round(ty, 3), rms_xy_m=round(rms, 3), n=n)


def get_summary(session_dir: Path, rebuild: bool = False) -> Dict[str, Any]:
    cache = session_dir / ".reader" / "summary.json"
    log_dir = session_dir / "flight_log"
    newest = max((p.stat().st_mtime for p in [session_dir / "manifest.json", *log_dir.glob("*")] if p.exists()),
                 default=0.0)
    if not rebuild and cache.exists() and cache.stat().st_mtime >= newest:
        try:
            data = json.loads(cache.read_text())
            if data.get("version") == SUMMARY_VERSION:
                return data
        except ValueError:
            pass
    data = build_summary(session_dir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, separators=(",", ":")))
    return data


def list_sessions(root: Path) -> List[Dict[str, Any]]:
    out = []
    if not root.is_dir():
        return out
    for d in sorted((p for p in root.iterdir() if (p / "manifest.json").is_file()), reverse=True):
        try:
            m = json.loads((d / "manifest.json").read_text())
        except ValueError:
            continue
        mission_files = [Path(c["original"]).name for c in (m.get("mission") or {}).get("config_files", [])
                         if not Path(c["original"]).name.startswith("launch_params_")]
        out.append(dict(id=d.name, start=m.get("start_time_iso"), duration_s=m.get("duration_s"),
                        status=m.get("status"), end_reason=m.get("end_reason"), total_bytes=m.get("total_bytes"),
                        namespace=m.get("namespace"), host=m.get("host"), profiles=m.get("profiles"),
                        mission_files=mission_files[:6],
                        dirty=[Path(s["path"]).name for s in m.get("software", []) if s.get("dirty")]))
    return out
