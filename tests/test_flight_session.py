import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agi_logger import flight_session as fs


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(fs, "STATE_DIR", d)
    monkeypatch.setattr(fs, "SESSION_STATE", d / "flight_session.json")
    monkeypatch.setattr(fs, "CURRENT_SESSION", d / "current_session.json")
    return d


def make_manager(tmp_path, **overrides):
    cfg_file = tmp_path / "configs.yaml"
    cfg_file.write_text("agi_logger: {}\n")
    cfg = dict(enabled=True, namespace="drone7", session_path=str(tmp_path / "flights"), name="test",
               profiles={}, manifest=dict(repos=[], processes=[], env_prefixes=["AGIPIX_"]), collect=[])
    cfg.update(overrides)
    return fs.SessionManager(cfg, cfg_file, log=lambda m: None)


def test_start_stop_manifest_and_announce(tmp_path, state_dir):
    mgr = make_manager(tmp_path)
    sdir = mgr.start(dict(source="/fmu/out/vehicle_status_v1", state="armed"))
    assert sdir.is_dir() and sdir.name.endswith("_test")
    current = json.loads((state_dir / "current_session.json").read_text())
    assert current["active"] and current["path"] == str(sdir) and current["namespace"] == "drone7"
    m = json.loads((sdir / "manifest.json").read_text())
    assert m["status"] == "recording" and m["trigger"]["state"] == "armed"
    assert (sdir / "config" / "agi_logger_configs.yaml").is_file()

    m = mgr.stop("disarm")
    assert m["status"] == "closed" and m["end_reason"] == "disarm" and m["duration_s"] >= 0
    assert not mgr.active
    assert json.loads((state_dir / "current_session.json").read_text())["active"] is False
    assert not (state_dir / "flight_session.json").exists()


def test_mission_process_and_config_capture(tmp_path, state_dir):
    mission = tmp_path / "mission.yaml"
    mission.write_text("auto: {profile: test}\n")
    env = dict(os.environ, AGIPIX_LIOM_CONFIG="mapping_m_livox_imu.yaml")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "agipix_auto_dummy",
                             f"--params-file={mission}"], env=env)
    try:
        time.sleep(0.3)
        mgr = make_manager(tmp_path, manifest=dict(repos=[], processes=["*agipix_auto_dummy*"],
                                                   env_prefixes=["AGIPIX_"]))
        sdir = mgr.start({})
        m = json.loads((sdir / "manifest.json").read_text())
        procs = m["mission"]["processes"]
        assert len(procs) == 1 and procs[0]["pid"] == proc.pid
        assert procs[0]["env"].get("AGIPIX_LIOM_CONFIG") == "mapping_m_livox_imu.yaml"
        assert str(mission) in procs[0]["config_files"]
        copies = {c["original"]: c["copy"] for c in m["mission"]["config_files"]}
        assert (sdir / copies[str(mission)]).read_text() == mission.read_text()
        mgr.stop()
    finally:
        proc.kill()


def test_git_info(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    info = fs.git_info(str(repo))
    assert info["commit"] and len(info["commit"]) == 40 and info["dirty"] is False
    (repo / "a.txt").write_text("y")
    assert fs.git_info(str(repo))["dirty"] is True
    assert fs.git_info(str(tmp_path / "nope"))["commit"] is None


def test_collects_only_files_written_during_the_session(tmp_path, state_dir):
    comp = tmp_path / "liom"
    comp.mkdir()
    old = comp / "old.csv"
    old.write_text("old")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    mgr = make_manager(tmp_path, collect=[str(comp / "*.csv")])
    sdir = mgr.start({})
    (comp / "scan_log.csv").write_text("frame,t\n1,2\n")
    m = mgr.stop()
    names = [Path(c["original"]).name for c in m["files_collected"]]
    assert names == ["scan_log.csv"]
    assert (sdir / m["files_collected"][0]["copy"]).read_text().startswith("frame")


def test_recover_stale_session(tmp_path, state_dir):
    mgr = make_manager(tmp_path)
    sdir = mgr.start({})
    # simulate a logger crash: a new manager instance finds the open session
    mgr2 = make_manager(tmp_path)
    assert mgr2.recover_stale() == sdir
    m = json.loads((sdir / "manifest.json").read_text())
    assert m["status"] == "closed" and m["end_reason"] == "recovered_after_restart"
    assert mgr2.recover_stale() is None


def test_profiles_selection(tmp_path, state_dir):
    profiles = dict(flight=dict(topics=["/a"]), diagnostic=dict(topics=["/b"]))
    assert make_manager(tmp_path, profiles=profiles)._profiles() == ["flight"]
    assert make_manager(tmp_path, profiles=profiles, diagnostic=True)._profiles() == ["flight", "diagnostic"]
    assert make_manager(tmp_path, profiles=profiles, flight_log=False)._profiles() == []
