"""Flight sessions: one folder per arm → disarm, holding everything needed to debug that flight.

<session_path>/<YYYYmmdd_HHMMSS>[_<name>]/
  manifest.json              who/what/when: drone, host, software versions (git), mission (process command
                             lines, config paths, AGIPIX_* environment), recorder stats, collected files,
                             sizes, how the session ended
  config/                    copies of the config files referenced by the stack's command lines
  flight_log/                curated, rate-limited MCAP (always; profile "flight")
  diag_log/                  full diagnostic MCAP (optional; profile "diagnostic")
  files/                     component files written during the flight (LIOM scan logs, traces, mission result)
  recorder_<profile>_stats.json

The session is announced to the stack in ~/.agi_logger/current_session.json (and on the
`/<ns>/logger/session` topic by the ROS node), so components can write into it. Starting and
stopping never block the caller for long: the recorders are separate processes, stopped with SIGINT
and a bounded wait. A session left open by a crash or power loss is closed on the next start
(`end_reason: recovered_after_restart`).

No ROS dependency here; the ROS node (ros2_node.py) only calls start() / stop().
"""
from __future__ import annotations

import fnmatch
import getpass
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__ as AGI_LOGGER_VERSION

STATE_DIR = Path.home() / ".agi_logger"
SESSION_STATE = STATE_DIR / "flight_session.json"
CURRENT_SESSION = STATE_DIR / "current_session.json"
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".xml", ".usd", ".usda")
PARAM_FILE_FLAGS = ("--params-file", "--config_file", "--config-file", "--config", "-c", "config_file", "params_file")


def _atomic_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def git_info(path: str) -> Dict[str, Any]:
    """HEAD, branch and dirty flag of a repository (None fields if it is not one)."""
    def run(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return dict(path=path, commit=run("rev-parse", "HEAD"), branch=run("rev-parse", "--abbrev-ref", "HEAD"),
                dirty=bool(run("status", "--porcelain", "--untracked-files=no")))


def process_snapshot(patterns: List[str], env_prefixes: List[str]) -> List[Dict[str, Any]]:
    """Processes whose command line matches any glob pattern: pid, command line, cwd, selected
    environment variables and the config files their arguments point to."""
    procs = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            argv = [a.decode(errors="replace") for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        line = " ".join(argv)
        if not any(fnmatch.fnmatch(line, p) for p in patterns):
            continue
        entry: Dict[str, Any] = dict(pid=int(proc.name), cmdline=argv)
        try:
            entry["cwd"] = os.readlink(proc / "cwd")
        except OSError:
            entry["cwd"] = None
        try:
            env = dict(kv.split(b"=", 1) for kv in (proc / "environ").read_bytes().split(b"\0") if b"=" in kv)
            entry["env"] = {k.decode(): v.decode(errors="replace") for k, v in env.items()
                            if any(k.decode().startswith(p) for p in env_prefixes)}
        except OSError:
            entry["env"] = {}
        files = []
        for i, arg in enumerate(argv[1:], start=1):
            token = arg.split("=", 1)[-1]
            p = Path(token) if os.path.isabs(token) else Path(entry["cwd"] or "/") / token
            # config-like suffixes, or anything passed as a parameter / config file (ROS launch writes the
            # effective node parameters to /tmp/launch_params_* without a suffix)
            follows_flag = argv[i - 1] in PARAM_FILE_FLAGS or arg.split("=", 1)[0] in PARAM_FILE_FLAGS
            if (p.suffix in CONFIG_SUFFIXES or follows_flag) and p.is_file():
                files.append(str(p))
        entry["config_files"] = sorted(set(files))
        procs.append(entry)
    return procs


def _mcap_ok(path: Path) -> bool:
    try:
        return subprocess.run(["mcap", "info", str(path)], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True  # no mcap CLI: assume fine, reindex still runs if metadata is missing


def repair_logs(sdir: Path, log: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    """Make every log in the session readable after a crash: salvage the finished chunks of a segment
    without a footer (`mcap recover`), drop empty segments, regenerate a missing rosbag2
    metadata.yaml (`ros2 bag reindex`). Clean sessions are only checked (fast)."""
    repairs = []
    have_mcap_cli = shutil.which("mcap") is not None
    for bag in sorted(d for d in sdir.iterdir() if d.is_dir() and any(d.glob("*.mcap"))):
        changed = False
        for seg in sorted(bag.glob("*.mcap")):
            if seg.stat().st_size == 0:
                seg.unlink()
                repairs.append(dict(file=str(seg.relative_to(sdir)), action="removed_empty"))
                changed = True
            elif have_mcap_cli and not _mcap_ok(seg):
                fixed = seg.with_suffix(".recovered.mcap")
                subprocess.run(["mcap", "recover", str(seg), "-o", str(fixed)], capture_output=True, timeout=300)
                if fixed.exists() and fixed.stat().st_size > 0 and _mcap_ok(fixed):
                    os.replace(fixed, seg)
                    repairs.append(dict(file=str(seg.relative_to(sdir)), action="recovered"))
                else:
                    fixed.unlink(missing_ok=True)
                    seg.rename(seg.with_suffix(".mcap.corrupt"))
                    repairs.append(dict(file=str(seg.relative_to(sdir)), action="unrecoverable"))
                changed = True
        if (changed or not (bag / "metadata.yaml").exists()) and any(bag.glob("*.mcap")):
            (bag / "metadata.yaml").unlink(missing_ok=True)
            try:
                r = subprocess.run(["ros2", "bag", "reindex", str(bag), "-s", "mcap"], capture_output=True,
                                   text=True, timeout=600)
                repairs.append(dict(file=str(bag.relative_to(sdir)), action="reindexed" if r.returncode == 0
                                    else f"reindex_failed: {r.stderr.strip()[-200:]}"))
            except (OSError, subprocess.TimeoutExpired) as exc:
                repairs.append(dict(file=str(bag.relative_to(sdir)), action=f"reindex_failed: {exc}"))
    for r in repairs:
        log(f"[flight_session] repair {r['file']}: {r['action']}")
    return repairs


class SessionManager:
    def __init__(self, cfg: Dict[str, Any], config_path: Path, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.log = log
        self.session_dir: Optional[Path] = None
        self.start_time: Optional[float] = None
        self.recorders: Dict[str, int] = {}
        self.namespace = str(cfg.get("namespace", "drone0"))

    # ------------------------------------------------------------------ state
    @property
    def active(self) -> bool:
        return self.session_dir is not None

    def _write_state(self) -> None:
        _atomic_json(SESSION_STATE, dict(session_dir=str(self.session_dir), start_time=self.start_time,
                                         recorders=self.recorders, pid=os.getpid()))

    def announce(self) -> Dict[str, Any]:
        info = dict(active=self.active, path=str(self.session_dir) if self.session_dir else None,
                    id=self.session_dir.name if self.session_dir else None, namespace=self.namespace,
                    start_time=self.start_time, updated=time.time())
        _atomic_json(CURRENT_SESSION, info)
        return info

    # ------------------------------------------------------------------ lifecycle
    def recover_stale(self) -> Optional[Path]:
        """Close a session left open by a crash / power loss of the logger."""
        if not SESSION_STATE.exists():
            return None
        try:
            st = json.loads(SESSION_STATE.read_text())
        except (OSError, ValueError):
            SESSION_STATE.unlink(missing_ok=True)
            return None
        sdir = Path(st.get("session_dir", ""))
        for pid in (st.get("recorders") or {}).values():
            self._stop_pid(int(pid), wait_s=5.0)
        if sdir.is_dir():
            self.session_dir, self.start_time, self.recorders = sdir, st.get("start_time"), {}
            self.log(f"[flight_session] closing session left open by a restart: {sdir}")
            self.stop("recovered_after_restart")
        SESSION_STATE.unlink(missing_ok=True)
        return sdir if sdir.is_dir() else None

    def start(self, trigger: Dict[str, Any]) -> Path:
        if self.active:
            return self.session_dir
        root = Path(os.path.expanduser(str(self.cfg.get("session_path", "~/agi_flights"))))
        name = str(self.cfg.get("name", "")).strip()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sdir = root / (f"{stamp}_{name}" if name else stamp)
        sdir.mkdir(parents=True, exist_ok=False)
        self.session_dir, self.start_time, self.recorders = sdir, time.time(), {}

        manifest = self._initial_manifest(trigger)
        _atomic_json(sdir / "manifest.json", manifest)
        self._write_state()
        for profile in self._profiles():
            pid = self._spawn_recorder(profile)
            if pid:
                self.recorders[profile] = pid
        self._write_state()
        self.announce()
        self.log(f"[flight_session] started {sdir} (recorders: {', '.join(self.recorders) or 'none'})")
        return sdir

    def stop(self, end_reason: str = "disarm") -> Dict[str, Any]:
        if not self.active:
            return {}
        sdir = self.session_dir
        for profile, pid in list(self.recorders.items()):
            self._stop_pid(pid, wait_s=float(self.cfg.get("recorder_stop_timeout_s", 15.0)))
        manifest = self._read_manifest(sdir)
        manifest["repairs"] = repair_logs(sdir, self.log)
        manifest["files_collected"] = self._collect(sdir)
        end = time.time()
        manifest.update(
            status="closed", end_reason=end_reason, end_time=end,
            end_time_iso=datetime.fromtimestamp(end).isoformat(timespec="seconds"),
            duration_s=round(end - (self.start_time or end), 1),
            recorders={p: self._read_json(sdir / f"recorder_{p}_stats.json") for p in self._profiles()},
            sizes_bytes={d.name: _dir_size(d) for d in sdir.iterdir() if d.is_dir()},
            total_bytes=_dir_size(sdir))
        _atomic_json(sdir / "manifest.json", manifest)
        self.session_dir, self.start_time, self.recorders = None, None, {}
        SESSION_STATE.unlink(missing_ok=True)
        self.announce()
        self.log(f"[flight_session] closed {sdir} ({end_reason}, {manifest['duration_s']} s, "
                 f"{manifest['total_bytes'] / 1e6:.1f} MB)")
        return manifest

    # ------------------------------------------------------------------ helpers
    def _profiles(self) -> List[str]:
        profiles = ["flight"] if self.cfg.get("flight_log", True) else []
        if self.cfg.get("diagnostic", False):
            profiles.append("diagnostic")
        return [p for p in profiles if p in (self.cfg.get("profiles") or {})]

    def _initial_manifest(self, trigger: Dict[str, Any]) -> Dict[str, Any]:
        mcfg = self.cfg.get("manifest", {}) or {}
        procs = process_snapshot(list(mcfg.get("processes", [])), list(mcfg.get("env_prefixes", ["AGIPIX_"])))
        copied = self._copy_configs(sorted({f for p in procs for f in p["config_files"]} |
                                           {os.path.expanduser(f) for f in mcfg.get("extra_files", [])}))
        return dict(
            schema="agi_logger.flight_session/1", status="recording", session_id=self.session_dir.name,
            namespace=self.namespace, start_time=self.start_time,
            start_time_iso=datetime.fromtimestamp(self.start_time).isoformat(timespec="seconds"),
            trigger=trigger, host=os.uname().nodename, user=getpass.getuser(), kernel=os.uname().release,
            ros_distro=os.environ.get("ROS_DISTRO", "unknown"), agi_logger_version=AGI_LOGGER_VERSION,
            logger_config=str(self.config_path), profiles=self._profiles(),
            software=[git_info(os.path.expanduser(r)) for r in mcfg.get("repos", [])],
            mission=dict(processes=procs, config_files=copied))

    def _copy_configs(self, files: List[str]) -> List[Dict[str, str]]:
        out = []
        cdir = self.session_dir / "config"
        for f in files:
            src = Path(f)
            if not src.is_file() or src.stat().st_size > 20 * 1024 * 1024:
                continue
            dst = cdir / src.as_posix().lstrip("/").replace("/", "__")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            out.append(dict(original=str(src), copy=str(dst.relative_to(self.session_dir))))
        if self.config_path.is_file():
            cdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.config_path, cdir / "agi_logger_configs.yaml")
        return out

    def _collect(self, sdir: Path) -> List[Dict[str, Any]]:
        """Copy component files modified during the session (globs from config) into files/."""
        out = []
        since = (self.start_time or 0) - 1.0
        for entry in (self.cfg.get("collect") or []):
            pattern = os.path.expandvars(os.path.expanduser(entry if isinstance(entry, str) else entry["glob"]))
            for f in sorted(glob.glob(pattern, recursive=True)):
                p = Path(f)
                try:
                    if not p.is_file() or p.stat().st_mtime < since:
                        continue
                    dst = sdir / "files" / p.as_posix().lstrip("/").replace("/", "__")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    out.append(dict(original=str(p), copy=str(dst.relative_to(sdir)), bytes=p.stat().st_size))
                except OSError as exc:
                    self.log(f"[flight_session] could not collect {p}: {exc}")
        return out

    def _spawn_recorder(self, profile: str) -> Optional[int]:
        out_dir = self.session_dir / {"flight": "flight_log", "diagnostic": "diag_log"}.get(profile, f"{profile}_log")
        cmd = [sys.executable, "-m", "agi_logger.flight_recorder", "--out", str(out_dir),
               "--profile", profile, "--config", str(self.config_path), "--namespace", self.namespace]
        if shutil.which("ionice"):
            cmd = ["ionice", "-c", "2", "-n", "7"] + cmd
        log_path = self.session_dir / f"recorder_{profile}.log"
        try:
            with open(log_path, "ab") as logf:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                        start_new_session=True, cwd=str(Path(__file__).resolve().parents[1]))
        except OSError as exc:
            self.log(f"[flight_session] could not start the '{profile}' recorder: {exc}")
            return None
        return proc.pid

    @staticmethod
    def _stop_pid(pid: int, wait_s: float) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except ChildProcessError:  # not our child (recovered session): poll its existence
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _read_manifest(self, sdir: Path) -> Dict[str, Any]:
        return self._read_json(sdir / "manifest.json") or dict(schema="agi_logger.flight_session/1",
                                                               session_id=sdir.name)
