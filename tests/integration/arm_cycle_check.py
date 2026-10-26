#!/usr/bin/env python3
"""End-to-end check of flight sessions with a fake PX4 (no simulator).

Starts the agi_logger autostart node on an isolated ROS domain. A fake vehicle publishes
/fmu/out/vehicle_status_v1 (disarmed → armed for --armed-s → disarmed), odometry at 100 Hz,
/drone0/auto/status at 10 Hz, JPEG camera frames at 15 Hz and some /rosout warnings. A dummy
"agipix_auto" process carries a mission file on its command line.

Then it checks the session folder: manifest closed and complete, mission config copied, flight and
diagnostic MCAPs readable, per-topic rate limits, thumbnails, health and rosout filter.

  python3 tests/integration/arm_cycle_check.py [--armed-s 12] [--keep]
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--armed-s", type=float, default=12.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--crash", action="store_true",
                    help="SIGKILL the logger and its recorders 5 s into the flight (power loss), restart it while armed")
    a = ap.parse_args()
    os.environ.update(ROS_DOMAIN_ID="88", ROS_LOCALHOST_ONLY="1")
    work = Path(tempfile.mkdtemp(prefix="agi_logger_it_"))
    cfg = yaml.safe_load((REPO / "cfg/configs.yaml").read_text())
    lg = cfg["agi_logger"]["logger"]
    lg.update(auto_start=False, bag_path=str(work / "bags"))  # bags are not under test here
    fsc = cfg["agi_logger"]["flight_session"]
    fsc.update(session_path=str(work / "flights"), diagnostic=True, disarm_grace_s=2.0, name="it",
               collect=[str(work / "liom" / "*.csv")])
    fsc["manifest"]["processes"] = ["*agipix_auto_itdummy*"]
    cfg_path = work / "configs.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    mission = work / "mission_it.yaml"
    mission.write_text("auto: {profile: it}\n")
    (work / "liom").mkdir()
    os.environ["HOME"] = str(work)  # isolate ~/.agi_logger state

    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "agipix_auto_itdummy",
                              "--params-file", str(mission)])
    def start_logger(tag):
        return subprocess.Popen([sys.executable, "-c",
                                 "import sys; from pathlib import Path; from agi_logger.ros2_node import run_autostart_node;"
                                 f"run_autostart_node(Path('{cfg_path}'))"],
                                cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=open(work / f"logger_{tag}.log", "w"),
                                stderr=subprocess.STDOUT, start_new_session=True)
    logger = start_logger("a")
    crash = None
    if a.crash:
        def crash():
            nonlocal logger
            st = json.loads((work / ".agi_logger" / "flight_session.json").read_text())
            for pid in [logger.pid] + list(st["recorders"].values()):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            logger.wait()
            print("  crashed logger + recorders (SIGKILL)")
            time.sleep(1.0)
            logger = start_logger("b")
    try:
        fake_vehicle(a.armed_s, work, crash)
        time.sleep(4.0)
    finally:
        os.killpg(os.getpgid(logger.pid), signal.SIGINT)
        logger.wait(timeout=30)
        dummy.kill()
    ok = verify_crash(work) if a.crash else verify(work, a.armed_s)
    print(("PASS" if ok else "FAIL") + f"  (work dir {work})")
    if ok and not a.keep:
        shutil.rmtree(work)
    return 0 if ok else 1


def fake_vehicle(armed_s: float, work: Path, crash=None) -> None:
    import cv2
    import numpy as np
    import rclpy
    from nav_msgs.msg import Odometry
    from px4_msgs.msg import VehicleStatus
    from rcl_interfaces.msg import Log
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String

    rclpy.init()
    n = rclpy.create_node("fake_vehicle")
    be = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)
    p_status = n.create_publisher(VehicleStatus, "/fmu/out/vehicle_status_v1", be)
    p_odom = n.create_publisher(Odometry, "/drone0/agiptive_liom/state/odom", be)
    p_auto = n.create_publisher(String, "/drone0/auto/status", 10)
    p_cam = n.create_publisher(CompressedImage, "/drone0/camera/image/compressed", be)
    p_log = n.create_publisher(Log, "/rosout", 10)
    img = (np.random.default_rng(0).random((480, 640, 3)) * 255).astype(np.uint8)
    jpg = cv2.imencode(".jpg", img)[1].tobytes()
    t0 = time.time()
    k = 0
    phases = [(3.0, False), (armed_s, True), (6.0, False)]
    for dur, armed in phases:
        end = time.time() + dur
        while time.time() < end:
            if k % 20 == 0:  # 5 Hz status
                st = VehicleStatus()
                st.arming_state = VehicleStatus.ARMING_STATE_ARMED if armed else VehicleStatus.ARMING_STATE_DISARMED
                p_status.publish(st)
            p_odom.publish(Odometry())
            if k % 10 == 0:
                p_auto.publish(String(data=f"phase={'flying' if armed else 'idle'} t={time.time() - t0:.1f}"))
            if k % 7 == 0:
                p_cam.publish(CompressedImage(format="jpeg", data=jpg))
            if k % 50 == 0:
                p_log.publish(Log(level=30 if k % 100 == 0 else 20, name="it", msg=f"k={k}"))
            if armed and crash is not None and k == 800:  # 5 s into the armed phase
                crash()
                crash = None
            if armed and k % 100 == 0:
                (work / "liom" / "scan_log.csv").write_text(f"frame\n{k}\n")
            rclpy.spin_once(n, timeout_sec=0.0)
            time.sleep(0.01)
            k += 1
    n.destroy_node()
    rclpy.shutdown()


def read_mcap(bag_dir: Path):
    import rosbag2_py
    r = rosbag2_py.SequentialReader()
    r.open(rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id="mcap"),
           rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr"))
    counts, first, last = {}, {}, {}
    while r.has_next():
        topic, _, t = r.read_next()
        counts[topic] = counts.get(topic, 0) + 1
        first.setdefault(topic, t)
        last[topic] = t
    return counts, {k: (last[k] - first[k]) / 1e9 for k in counts}


def verify(work: Path, armed_s: float) -> bool:
    sessions = sorted((work / "flights").glob("*_it"))
    checks = []
    def check(name, cond, detail=""):
        checks.append(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {name} {detail}")
    check("exactly one session", len(sessions) == 1, str([s.name for s in sessions]))
    if not sessions:
        return False
    s = sessions[0]
    m = json.loads((s / "manifest.json").read_text())
    check("manifest closed on disarm", m.get("status") == "closed" and m.get("end_reason") == "disarm", m.get("end_reason"))
    check("duration ~ armed + grace", armed_s <= m.get("duration_s", 0) <= armed_s + 6, str(m.get("duration_s")))
    procs = m["mission"]["processes"]
    check("mission process captured", any("agipix_auto_itdummy" in " ".join(p["cmdline"]) for p in procs))
    check("mission config copied", any(c["original"].endswith("mission_it.yaml") for c in m["mission"]["config_files"]))
    check("component file collected", any(c["original"].endswith("scan_log.csv") for c in m["files_collected"]))
    for prof, sub in (("flight", "flight_log"), ("diagnostic", "diag_log")):
        counts, spans = read_mcap(s / sub)
        print(f"  {prof}: " + ", ".join(f"{t}={c}" for t, c in sorted(counts.items())))
        odom = "/drone0/agiptive_liom/state/odom"
        rate = counts.get(odom, 0) / max(spans.get(odom, 1e-9), 1e-9)
        if prof == "flight":
            check("flight: odom limited to ~20 Hz", 15 <= rate <= 21, f"{rate:.1f} Hz")
            check("flight: thumbnails ~1 Hz", "/drone0/camera/image/compressed/thumbnail" in counts and
                  counts["/drone0/camera/image/compressed/thumbnail"] <= armed_s + 4)
            check("flight: rosout only WARN+", 0 < counts.get("/rosout", 0) <= armed_s + 6)
        else:
            check("diagnostic: odom at full rate", rate > 60, f"{rate:.1f} Hz")
        check(f"{prof}: health channel", counts.get("/agi_logger/health", 0) >= armed_s / 2 - 2)
        st = m["recorders"].get(prof, {})
        check(f"{prof}: recorder stats final, no drops", st.get("final") and
              all(v.get("dropped", 0) == 0 for v in st.get("topics", {}).values()))
    return all(checks)


def verify_crash(work: Path) -> bool:
    """After a power-loss style crash while armed: the interrupted session is closed as
    recovered_after_restart with readable logs, and the restarted logger opened a new session
    for the rest of the flight that closed normally on disarm."""
    sessions = sorted((work / "flights").glob("*_it"))
    ok = len(sessions) == 2
    print(f"  [{'ok' if ok else 'FAIL'}] two sessions {[s.name for s in sessions]}")
    for s, reason in zip(sessions, ("recovered_after_restart", "disarm")):
        m = json.loads((s / "manifest.json").read_text())
        good = m.get("status") == "closed" and m.get("end_reason") == reason
        print(f"  [{'ok' if good else 'FAIL'}] {s.name}: {m.get('end_reason')} {m.get('duration_s')} s")
        ok &= good
        for sub in ("flight_log", "diag_log"):
            try:
                counts, _ = read_mcap(s / sub)
                n = sum(counts.values())
                print(f"  [{'ok' if n > 0 else 'FAIL'}] {s.name}/{sub} readable: {n} messages")
                ok &= n > 0
            except Exception as exc:
                print(f"  [FAIL] {s.name}/{sub} unreadable: {exc}")
                ok = False
    return ok


if __name__ == "__main__":
    raise SystemExit(main())
