import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from agi_logger.reader import summary
from agi_logger.reader.server import make_handler


def make_session(root: Path, name: str, **manifest) -> Path:
    d = root / name
    d.mkdir(parents=True)
    m = dict(schema="agi_logger.flight_session/1", status="closed", end_reason="disarm", start_time=1000.0,
             start_time_iso="2026-10-26T10:00:00", duration_s=12.0, total_bytes=1234, namespace="drone0",
             software=[dict(path="/w/agi_logger", commit="abc", dirty=True)],
             mission=dict(config_files=[dict(original="/x/mission.yaml", copy="config/mission.yaml"),
                                        dict(original="/tmp/launch_params_ab", copy="config/lp")]))
    m.update(manifest)
    (d / "manifest.json").write_text(json.dumps(m))
    (d / "config").mkdir()
    (d / "config" / "mission.yaml").write_text("auto: {}\n")
    return d


def test_list_sessions(tmp_path):
    make_session(tmp_path, "20261026_100000_a")
    make_session(tmp_path, "20261026_110000_b", end_reason="recovered_after_restart")
    (tmp_path / "not_a_session").mkdir()
    got = summary.list_sessions(tmp_path)
    assert [s["id"] for s in got] == ["20261026_110000_b", "20261026_100000_a"]
    assert got[1]["mission_files"] == ["mission.yaml"] and got[1]["dirty"] == ["agi_logger"]
    assert summary.list_sessions(tmp_path / "missing") == []


def test_align_xy_recovers_yaw_and_offset():
    import math
    t = [i * 0.1 for i in range(200)]
    px4 = dict(t=t, x=[math.cos(a / 20) * 5 for a in range(200)], y=[math.sin(a / 20) * 5 + 0.01 * a for a in range(200)], z=[1.0] * 200)
    yaw, tx, ty = math.radians(30), 2.0, -1.0
    co, si = math.cos(-yaw), math.sin(-yaw)  # LIOM = inverse transform of PX4
    lx = [co * (x - tx) - si * (y - ty) for x, y in zip(px4["x"], px4["y"])]
    ly = [si * (x - tx) + co * (y - ty) for x, y in zip(px4["x"], px4["y"])]
    out, al = summary._align_xy(dict(t=t, x=lx, y=ly, z=[1.0] * 200), px4)
    assert abs(al["yaw_deg"] - 30) < 0.1 and al["rms_xy_m"] < 1e-3
    assert abs(out["x"][50] - px4["x"][50]) < 1e-2


def test_server_routes_and_traversal(tmp_path):
    sdir = make_session(tmp_path, "20261026_100000_a")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert b"agi-logger" in urllib.request.urlopen(base + "/").read()
        assert json.loads(urllib.request.urlopen(base + "/api/sessions").read())[0]["id"] == sdir.name
        assert json.loads(urllib.request.urlopen(f"{base}/api/sessions/{sdir.name}/manifest").read())["duration_s"] == 12.0
        assert urllib.request.urlopen(f"{base}/api/sessions/{sdir.name}/files/config/mission.yaml").read() == b"auto: {}\n"
        for bad in (f"/api/sessions/{sdir.name}/files/..%2F..%2Fetc%2Fpasswd", "/api/sessions/..%2F..%2Fetc/manifest",
                    "/api/sessions/nope/summary"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 404
    finally:
        httpd.shutdown()
