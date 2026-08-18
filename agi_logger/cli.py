from __future__ import annotations

import argparse
import curses
import getpass
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    iter_nested_keys,
    load_raw_config,
    save_raw_config,
    update_nested_value,
)
from .logging_manager import RecorderManager
from .tcp_transfer import TcpClientConfig, TcpServerConfig, receive_file, send_file

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
ORANGE = "\033[38;2;255;165;0m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
RED = "\033[31m"
LIGHT_GRAY = "\033[90m"
CLEAR = "\033[2J\033[H"


def _clear_screen() -> None:
    print(CLEAR, end="")


def _print_title() -> None:
    title_lines = [
        "      █████╗  ██████╗ ██╗██████╗ ██╗██╗  ██╗ ",
        "     ██╔══██╗██╔════╝ ██║██╔══██╗██║╚██╗██╔╝ ",
        "     ███████║██║  ███╗██║██████╔╝██║ ╚███╔╝  ",
        "     ██╔══██║██║   ██║██║██╔═══╝ ██║ ██╔██╗  ",
        "     ██║  ██║╚██████╔╝██║██║     ██║██╔╝ ██╗ ",
        "     ╚═╝  ╚═╝ ╚═════╝ ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ",
        "",
        "██╗      ██████╗  ██████╗  ██████╗ ███████╗██████╗ ",
        "██║     ██╔═══██╗██╔════╝ ██╔════╝ ██╔════╝██╔══██╗",
        "██║     ██║   ██║██║  ███╗██║  ███╗█████╗  ██████╔╝",
        "██║     ██║   ██║██║   ██║██║   ██║██╔══╝  ██╔══██╗",
        "███████╗╚██████╔╝╚██████╔╝╚██████╔╝███████╗██║  ██║",
        "╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝",
    ]
    print()
    for line in title_lines:
        print(f"{ORANGE}{line}{RESET}")
    print(f"{CYAN}    Advanced ROS2 logging for Agipix Platform{RESET}\n")


def _load_config(config_path: Path) -> Dict[str, Any]:
    return load_raw_config(config_path)


def _get_manager(config_path: Path) -> RecorderManager:
    config = _load_config(config_path)
    return RecorderManager(config, config_path)


def _print_status(manager: RecorderManager) -> int:
    state = manager.status()
    if not state or not manager.is_recording():
        print("Recording inactive")
        return 0
    print("Recording active")
    print(f"Bag name: {state.bag_name}")
    print(f"Bag path: {state.bag_path}")
    print(f"PID: {state.pid}")
    return 0


def _tcp_allowed(manager: RecorderManager) -> None:
    if manager.is_recording():
        raise RuntimeError("TCP transfer disabled while logging is active")


def _parse_value(raw: str, existing_value: Any = None) -> Any:
    trimmed = raw.strip()
    lowered = trimmed.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None

    # If the setting is a list or formatted as a list
    if isinstance(existing_value, list) or (trimmed.startswith("[") and trimmed.endswith("]")):
        try:
            parsed = yaml.safe_load(trimmed)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
        return [item.strip() for item in trimmed.strip("[]").split(",") if item.strip()]

    try:
        if "." in trimmed:
            return float(trimmed)
        return int(trimmed)
    except ValueError:
        return trimmed


def _format_display_value(value: Any) -> str:
    if isinstance(value, list):
        if len(value) <= 3:
            return f"[{', '.join(str(v) for v in value)}]"
        return f"[{len(value)} items: {', '.join(str(v) for v in value[:2])}, ...]"
    return str(value)


def _settings_menu(config_path: Path, start_section: str | None = None) -> None:
    config = _load_config(config_path)
    dirty_keys: Set[str] = set()

    is_shortcut_flow = start_section is not None

    while True:
        _clear_screen()
        if start_section == "logger":
            choice = "1"
        elif start_section == "tcp_server":
            choice = "2_server"
        elif start_section == "tcp_client":
            choice = "2_client"
        else:
            print(f"\n{BOLD}{CYAN}Settings Menu{RESET}")
            print(f"{GREEN}1){RESET} Edit logger settings")
            print(f"{GREEN}2){RESET} Edit TCP transfer settings")
            print(f"{GREEN}3){RESET} Save configuration")
            print(f"{GREEN}4){RESET} Back")
            choice = input(f"{BOLD}Select option:{RESET} ").strip()

        if choice in {"1", "2", "2_server", "2_client"}:
            if choice == "1":
                section_key = "agi_logger.logger"
                current_section = "logger"
                section = config
                for part in section_key.split("."):
                    section = section.get(part, {}) if isinstance(section, dict) else {}
                entries = list(iter_nested_keys(section, section_key))
            else:
                if choice in {"2_server", "2_client"}:
                    mode_choice = "server" if choice == "2_server" else "client"
                else:
                    mode_choice = input(
                        f"{BOLD}Edit TCP settings for{RESET} [server/client]: "
                    ).strip().lower()
                    if mode_choice not in {"server", "client"}:
                        print(f"{RED}Invalid selection{RESET}")
                        time.sleep(1)
                        continue
                current_section = f"tcp_{mode_choice}"
                section_key = f"agi_logger.tcp_file_communication.{mode_choice}"
                section = config
                for part in section_key.split("."):
                    section = section.get(part, {}) if isinstance(section, dict) else {}
                entries = list(iter_nested_keys(section, section_key))

            if not entries:
                print("No editable keys found in section")
                time.sleep(1)
                if is_shortcut_flow:
                    return
                continue

            while True:
                _clear_screen()
                section_title = current_section.replace("_", " ").title()
                print(f"\n{BOLD}{CYAN}{section_title} Settings:{RESET}")
                display_entries = []
                for full_key, value in entries:
                    display_name = full_key.split(".")[-1]
                    display_entries.append((display_name, full_key, value))

                for idx, (display_name, full_key, value) in enumerate(display_entries, start=1):
                    val_str = _format_display_value(value)
                    color = YELLOW if full_key in dirty_keys else LIGHT_GRAY
                    print(f"{CYAN}{idx:2d}){RESET} {display_name:<20} = {color}{val_str}{RESET}")

                print(f"\n{LIGHT_GRAY}Press Enter to go back to the previous menu.{RESET}")
                raw_index = input(f"{BOLD}Select number to edit:{RESET} ").strip()

                if not raw_index:
                    if is_shortcut_flow:
                        if current_section == "logger":
                            _prompt_record_after_settings(config_path, config, dirty_keys)
                        elif current_section == "tcp_server":
                            _prompt_tcp_after_settings(config_path, config, "server", dirty_keys)
                        elif current_section == "tcp_client":
                            _prompt_tcp_after_settings(config_path, config, "client", dirty_keys)
                        return
                    break

                if not raw_index.isdigit():
                    print(f"{RED}Invalid selection{RESET}")
                    time.sleep(0.8)
                    continue

                index = int(raw_index)
                if index < 1 or index > len(display_entries):
                    print(f"{RED}Selection out of range{RESET}")
                    time.sleep(0.8)
                    continue

                display_name, full_key, current_value = display_entries[index - 1]
                print(f"\n{YELLOW}Editing{RESET} {BOLD}{display_name}{RESET}")
                if isinstance(current_value, list):
                    print(f"{LIGHT_GRAY}Current list ({len(current_value)} items):{RESET}")
                    for item in current_value:
                        print(f"  - {item}")
                    print(f"{LIGHT_GRAY}(Enter items separated by comma or YAML list format){RESET}")
                else:
                    print(f"Current value: {LIGHT_GRAY}{current_value}{RESET}")

                new_val_str = input("Enter new value (press Enter to keep current): ").strip()
                if new_val_str == "":
                    print(f"{YELLOW}No change made.{RESET}")
                    time.sleep(0.5)
                    continue

                parsed = _parse_value(new_val_str, existing_value=current_value)
                update_nested_value(config, full_key, parsed)
                dirty_keys.add(full_key)

                # Refresh entries with updated values
                section = config
                for part in section_key.split("."):
                    section = section.get(part, {}) if isinstance(section, dict) else {}
                entries = list(iter_nested_keys(section, section_key))
                print(f"{GREEN}Value updated successfully.{RESET}")
                time.sleep(0.5)

            if is_shortcut_flow:
                return

        elif choice == "3":
            save_raw_config(config, config_path)
            dirty_keys.clear()
            print(f"{GREEN}Saved changes to {config_path}{RESET}")
            time.sleep(1)
        elif choice == "4":
            if dirty_keys:
                save_prompt = input(f"{YELLOW}You have unsaved changes. Save before returning? [Y/n]:{RESET} ").strip().lower()
                if save_prompt in {"", "y", "yes"}:
                    save_raw_config(config, config_path)
                    print(f"{GREEN}Saved changes.{RESET}")
                    time.sleep(0.8)
            return
        else:
            print(f"{RED}Invalid selection{RESET}")
            time.sleep(0.8)

        start_section = None


def _record_start(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    state = manager.start_recording(verbose=True, foreground=not args.background)
    print(f"Started recording: {state.bag_name}")
    return 0


def _record_preview(args: argparse.Namespace) -> int:
    _clear_screen()
    config = _load_config(args.config)
    logger_cfg = config.get("agi_logger", {}).get("logger", {})
    print(f"\n{BOLD}{CYAN}Record Settings Preview{RESET}")
    for key, value in logger_cfg.items():
        val_str = _format_display_value(value)
        print(f"{CYAN}- {key:<20}{RESET}: {LIGHT_GRAY}{val_str}{RESET}")

    action = input(
        f"\n{BOLD}Action:{RESET} [Enter = Start / e = Edit / a = Autostart node / n = Cancel]: "
    ).strip().lower()
    if action == "e":
        _settings_menu(args.config, start_section="logger")
        return 0
    if action == "a":
        args = build_parser().parse_args(["--config", str(args.config), "ros2", "autostart"])
        return args.func(args)
    if action in {"", "y", "start"}:
        args = build_parser().parse_args(["--config", str(args.config), "record", "start"])
        return args.func(args)
    return 0


def _prompt_record_after_settings(
    config_path: Path, config: Dict[str, Any], highlight_keys: Set[str] | None = None
) -> bool:
    _clear_screen()
    logger_cfg = config.get("agi_logger", {}).get("logger", {})
    print(f"\n{BOLD}{CYAN}Record Settings Preview{RESET}")
    for key, value in logger_cfg.items():
        val_str = _format_display_value(value)
        full_key = f"agi_logger.logger.{key}"
        color = YELLOW if highlight_keys and full_key in highlight_keys else LIGHT_GRAY
        print(f"{CYAN}- {key:<20}{RESET}: {color}{val_str}{RESET}")

    action = input(
        f"\n{BOLD}Continue?{RESET} [Enter = Start Recording / e = Edit / a = Autostart node / s = Save & Return / n = Discard]: "
    ).strip().lower()
    if action == "e":
        _settings_menu(config_path, start_section="logger")
        return True
    if action == "s":
        save_raw_config(config, config_path)
        if highlight_keys is not None:
            highlight_keys.clear()
        print(f"{GREEN}Settings saved.{RESET}")
        time.sleep(1)
        return False
    if action == "a":
        save_raw_config(config, config_path)
        if highlight_keys is not None:
            highlight_keys.clear()
        args = build_parser().parse_args(["--config", str(config_path), "ros2", "autostart"])
        args.func(args)
        return False
    if action in {"", "y", "start"}:
        save_raw_config(config, config_path)
        if highlight_keys is not None:
            highlight_keys.clear()
        args = build_parser().parse_args(["--config", str(config_path), "record", "start"])
        args.func(args)
    return False


def _prompt_tcp_after_settings(
    config_path: Path, config: Dict[str, Any], mode: str, highlight_keys: Set[str] | None = None
) -> bool:
    _clear_screen()
    tcp_cfg = config.get("agi_logger", {}).get("tcp_file_communication", {})
    mode_cfg = tcp_cfg.get(mode, {})
    print(f"\n{BOLD}{CYAN}TCP {mode.title()} Settings Preview{RESET}")
    for key, value in mode_cfg.items():
        val_str = _format_display_value(value)
        full_key = f"agi_logger.tcp_file_communication.{mode}.{key}"
        color = YELLOW if highlight_keys and full_key in highlight_keys else LIGHT_GRAY
        print(f"{CYAN}- {key:<20}{RESET}: {color}{val_str}{RESET}")

    action = input(
        f"\n{BOLD}Continue?{RESET} [Enter = Start Transfer / e = Edit / s = Save & Return / n = Back]: "
    ).strip().lower()
    if action == "e":
        _settings_menu(config_path, start_section=f"tcp_{mode}")
        return True
    if action == "s":
        save_raw_config(config, config_path)
        if highlight_keys is not None:
            highlight_keys.clear()
        print(f"{GREEN}Settings saved.{RESET}")
        time.sleep(1)
        return False
    if action in {"", "y", "start"}:
        save_raw_config(config, config_path)
        cmd = "send" if mode == "server" else "receive"
        args = build_parser().parse_args(["--config", str(config_path), "tcp", cmd])
        args.func(args)
    return False


def _record_stop(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    manager.stop_recording()
    print("Recording stopped successfully.")
    return 0


def _record_status(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    return _print_status(manager)


def _bag_play(args: argparse.Namespace) -> int:
    cmd = ["ros2", "bag", "play", args.bag]
    if args.rate:
        cmd += ["--rate", str(args.rate)]
    if args.loop:
        cmd += ["--loop"]
    print("Running: " + " ".join(cmd))
    return _run_command(cmd)


def _tcp_send(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    _tcp_allowed(manager)

    config = _load_config(args.config)
    tcp_cfg = config["agi_logger"]["tcp_file_communication"]
    server_cfg = tcp_cfg.get("server", {})

    server = TcpServerConfig(
        host=args.host or server_cfg.get("host", "0.0.0.0"),
        port=args.port or int(server_cfg.get("port", 6000)),
        file_path=args.file or server_cfg.get("file_path", ""),
    )
    if not server.file_path:
        raise RuntimeError("File path is required for TCP send (configure in settings or pass --file)")
    send_file(server)
    return 0


def _tcp_receive(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    _tcp_allowed(manager)

    config = _load_config(args.config)
    tcp_cfg = config["agi_logger"]["tcp_file_communication"]
    client_cfg = tcp_cfg.get("client", {})

    client = TcpClientConfig(
        host=args.host or client_cfg.get("host", "localhost"),
        port=args.port or int(client_cfg.get("port", 6000)),
        destination_path=args.dest or client_cfg.get("destination_path", "."),
    )
    receive_file(client)
    return 0


def _tcp_run(args: argparse.Namespace) -> int:
    manager = _get_manager(args.config)
    _tcp_allowed(manager)

    config = _load_config(args.config)
    tcp_cfg = config["agi_logger"]["tcp_file_communication"]
    mode = str(tcp_cfg.get("mode", "ask")).lower()

    if mode == "ask":
        choice = input("Start as server or client? [server/client]: ").strip().lower()
        mode = choice or "ask"

    if mode == "server":
        args.file = args.file or tcp_cfg.get("server", {}).get("file_path")
        args.host = args.host or tcp_cfg.get("server", {}).get("host")
        args.port = args.port or tcp_cfg.get("server", {}).get("port")
        return _tcp_send(args)

    if mode == "client":
        args.host = args.host or tcp_cfg.get("client", {}).get("host")
        args.port = args.port or tcp_cfg.get("client", {}).get("port")
        args.dest = args.dest or tcp_cfg.get("client", {}).get("destination_path")
        return _tcp_receive(args)

    raise RuntimeError(f"Unsupported tcp mode: {mode}")


def _ros2_autostart(args: argparse.Namespace) -> int:
    from .ros2_node import run_autostart_node

    run_autostart_node(args.config)
    return 0


def _run_command(cmd: List[str]) -> int:
    try:
        process = subprocess.run(cmd, check=False)
        return process.returncode
    except FileNotFoundError:
        print("Command not found. Ensure ROS 2 is installed and available in PATH.")
        return 1


def _list_bag_dirs(path: str) -> List[str]:
    base = Path(path).expanduser()
    if not base.exists() or not base.is_dir():
        return []
    return sorted([p.name for p in base.iterdir() if p.is_dir()])


def _curses_select(
    options: List[str],
    title: str,
    hint: str,
    initial_index: int | None = None,
    last_played_index: int | None = None,
) -> Tuple[str, int | None]:
    def _inner(stdscr: "curses._CursesWindow") -> Tuple[str, int | None]:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.nodelay(False)
        stdscr.keypad(True)

        index = initial_index or 0
        offset = 0

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 4 or width < 10:
                time.sleep(0.1)
                continue

            visible = max(1, height - 5)

            try:
                stdscr.addstr(0, 0, title[: width - 1])
                stdscr.addstr(1, 0, hint[: width - 1])
            except curses.error:
                pass

            if not options:
                try:
                    stdscr.addstr(3, 0, "No bags found in directory."[: width - 1])
                    stdscr.addstr(4, 0, "Press 'c' to change directory or 'q' to go back."[: width - 1])
                except curses.error:
                    pass
            else:
                if index < offset:
                    offset = index
                elif index >= offset + visible:
                    offset = index - visible + 1

                for row in range(visible):
                    opt_index = offset + row
                    if opt_index >= len(options):
                        break
                    label = options[opt_index]
                    prefix = "* " if last_played_index == opt_index else "  "
                    line = f"{prefix}{label}"
                    y = row + 3
                    if y < height - 1:
                        try:
                            if opt_index == index:
                                stdscr.addstr(y, 0, line[: width - 1], curses.A_REVERSE)
                            else:
                                stdscr.addstr(y, 0, line[: width - 1])
                        except curses.error:
                            pass

            stdscr.refresh()
            key = stdscr.getch()

            if key in (curses.KEY_UP, ord("k")):
                if options:
                    index = max(0, index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                if options:
                    index = min(len(options) - 1, index + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                if options:
                    return "play", index
            elif key in (ord("c"), ord("C")):
                return "change", None
            elif key in (ord("q"), ord("Q"), 27):
                return "cancel", None

    try:
        return curses.wrapper(_inner)
    except Exception as exc:
        print(f"Interactive selector unavailable ({exc}).")
        return "cancel", None


def _run_rosbag_play_with_quit(cmd: List[str]) -> int:
    try:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(cmd, stdin=slave_fd)
    except FileNotFoundError:
        print("Command not found. Ensure ROS 2 is installed and available in PATH.")
        return 1

    os.close(slave_fd)

    try:
        tty_handle = open("/dev/tty", "rb")
    except OSError:
        os.close(master_fd)
        return process.wait()

    fd = tty_handle.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        os.close(master_fd)
        tty_handle.close()
        return process.wait()

    try:
        tty.setcbreak(fd)
        while process.poll() is None:
            readable, _, _ = select.select([tty_handle], [], [], 0.1)
            if readable:
                data = os.read(fd, 1024)
                if not data:
                    continue
                if b"q" in data or b"Q" in data:
                    process.send_signal(signal.SIGINT)
                    break
                os.write(master_fd, data)
        return process.wait()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        tty_handle.close()
        os.close(master_fd)


def _play_menu(config_path: Path, initial_path: str | None = None) -> int:
    config = _load_config(config_path)
    logger_cfg = config.get("agi_logger", {}).get("logger", {})
    bag_path = initial_path or str(logger_cfg.get("bag_path", "."))
    last_played: str | None = None

    while True:
        options = _list_bag_dirs(bag_path)
        title = f"Select a bag to play (path: {bag_path})"
        hint = "UP/DOWN to select | Enter: play | c: change dir | q: back (press 'q' during play to stop)"
        last_index = options.index(last_played) if last_played in options else None
        action, index = _curses_select(options, title, hint, last_index, last_index)

        if action == "cancel":
            return 0
        if action == "change":
            new_path = input("\nEnter absolute bag directory path: ").strip()
            if new_path:
                expanded = str(Path(new_path).expanduser().resolve())
                if Path(expanded).exists():
                    bag_path = expanded
                else:
                    print(f"{RED}Directory does not exist: {expanded}{RESET}")
                    time.sleep(1)
            continue
        if action == "play" and index is not None:
            selected = options[index]
            full_path = str(Path(bag_path).expanduser() / selected)
            cmd = ["ros2", "bag", "play", full_path]
            _run_rosbag_play_with_quit(cmd)
            last_played = selected
            continue


def _play_command(args: argparse.Namespace) -> int:
    return _play_menu(args.config, args.path)


def _interactive_menu(parser: argparse.ArgumentParser, config_path: Path) -> int:
    while True:
        _clear_screen()
        _print_title()
        print(f"{GREEN}1){RESET} Record")
        print(f"{GREEN}2){RESET} Transfer")
        print(f"{GREEN}3){RESET} Play")
        print(f"{GREEN}4){RESET} Settings")
        print(f"{GREEN}5){RESET} Exit")
        choice = input(f"\n{BOLD}Select option:{RESET} ").strip()

        if choice == "1":
            _record_preview(parser.parse_args(["--config", str(config_path), "record"]))
            continue

        if choice == "2":
            while True:
                _clear_screen()
                print(f"\n{BOLD}{CYAN}TCP Transfer Menu{RESET}")
                print(f"{GREEN}1){RESET} Server (Send)")
                print(f"{GREEN}2){RESET} Client (Receive)")
                print(f"{GREEN}3){RESET} Back")
                sub = input(f"\n{BOLD}Select option:{RESET} ").strip()
                if sub == "3" or sub == "":
                    break
                if sub not in {"1", "2"}:
                    print(f"{RED}Invalid selection{RESET}")
                    time.sleep(0.8)
                    continue

                config = _load_config(config_path)
                tcp_cfg = config.get("agi_logger", {}).get("tcp_file_communication", {})
                mode = "server" if sub == "1" else "client"
                mode_cfg = tcp_cfg.get(mode, {})

                _clear_screen()
                print(f"\n{BOLD}{CYAN}TCP {mode.title()} Settings Preview{RESET}")
                for key, value in mode_cfg.items():
                    val_str = _format_display_value(value)
                    print(f"{CYAN}- {key:<20}{RESET}: {LIGHT_GRAY}{val_str}{RESET}")

                action = input(
                    f"\n{BOLD}Continue transfer?{RESET} [Enter = Start / e = Edit / n = Back]: "
                ).strip().lower()
                if action == "e":
                    _settings_menu(
                        config_path,
                        start_section="tcp_server" if mode == "server" else "tcp_client",
                    )
                    continue
                if action in {"", "y", "start"}:
                    cmd = "send" if mode == "server" else "receive"
                    args = parser.parse_args(["--config", str(config_path), "tcp", cmd])
                    try:
                        args.func(args)
                    except Exception as exc:
                        print(f"\n{RED}Transfer error: {exc}{RESET}")
                    input(f"\n{LIGHT_GRAY}Press Enter to continue...{RESET}")
                continue

        if choice == "3":
            args = parser.parse_args(["--config", str(config_path), "play"])
            args.func(args)
            continue

        if choice == "4":
            args = parser.parse_args(["--config", str(config_path), "settings"])
            args.func(args)
            continue

        if choice == "5" or choice.lower() in {"q", "exit", "quit"}:
            return 0

        continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agi-logger", description="AGI logger CLI")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command")

    record_parser = subparsers.add_parser("record", help="Manage bag recording")
    record_sub = record_parser.add_subparsers(dest="record_cmd")

    record_start = record_sub.add_parser("start", help="Start recording")
    record_start.add_argument(
        "--background",
        action="store_true",
        help="Run in background (use 'agi-logger record stop' to terminate)",
    )
    record_start.set_defaults(func=_record_start)

    record_stop = record_sub.add_parser("stop", help="Stop recording")
    record_stop.set_defaults(func=_record_stop)

    record_status = record_sub.add_parser("status", help="Show recording status")
    record_status.set_defaults(func=_record_status)

    record_parser.set_defaults(func=_record_preview)

    bag_parser = subparsers.add_parser("bag", help="Bag utilities")
    bag_sub = bag_parser.add_subparsers(dest="bag_cmd", required=True)
    bag_play = bag_sub.add_parser("play", help="Play a bag")
    bag_play.add_argument("bag", help="Bag path")
    bag_play.add_argument("--rate", type=float, default=1.0, help="Playback rate")
    bag_play.add_argument("--loop", action="store_true", help="Loop playback")
    bag_play.set_defaults(func=_bag_play)

    play_parser = subparsers.add_parser("play", help="Select and play a bag")
    play_parser.add_argument("--path", help="Override bag directory path")
    play_parser.set_defaults(func=_play_command)

    tcp_parser = subparsers.add_parser("tcp", help="TCP file transfer")
    tcp_sub = tcp_parser.add_subparsers(dest="tcp_cmd", required=True)

    tcp_send = tcp_sub.add_parser("send", help="Send a file or bag directory over TCP")
    tcp_send.add_argument("--file", help="Path to file or bag directory to send")
    tcp_send.add_argument("--host", help="Server bind host")
    tcp_send.add_argument("--port", type=int, help="Server port")
    tcp_send.set_defaults(func=_tcp_send)

    tcp_receive = tcp_sub.add_parser("receive", help="Receive a file or bag directory over TCP")
    tcp_receive.add_argument("--host", help="Server host")
    tcp_receive.add_argument("--port", type=int, help="Server port")
    tcp_receive.add_argument("--dest", help="Destination directory")
    tcp_receive.set_defaults(func=_tcp_receive)

    tcp_run = tcp_sub.add_parser("run", help="Use configured TCP mode")
    tcp_run.add_argument("--file", help="File or bag directory path (server mode)")
    tcp_run.add_argument("--host", help="Host override")
    tcp_run.add_argument("--port", type=int, help="Port override")
    tcp_run.add_argument("--dest", help="Destination directory (client mode)")
    tcp_run.set_defaults(func=_tcp_run)

    settings_parser = subparsers.add_parser("settings", help="Open settings menu")
    settings_parser.set_defaults(func=lambda args: _settings_menu(args.config) or 0)

    ros2_parser = subparsers.add_parser("ros2", help="ROS 2 utilities")
    ros2_sub = ros2_parser.add_subparsers(dest="ros2_cmd", required=True)
    ros2_autostart = ros2_sub.add_parser("autostart", help="Run autostart node")
    ros2_autostart.set_defaults(func=_ros2_autostart)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _print_title()
    if not args.command:
        exit_code = _interactive_menu(parser, args.config)
        raise SystemExit(exit_code)

    try:
        exit_code = args.func(args)
    except (ConfigError, RuntimeError) as exc:
        print(f"{RED}Error: {exc}{RESET}")
        exit_code = 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        exit_code = 130

    raise SystemExit(exit_code)
