      █████╗  ██████╗ ██╗██████╗ ██╗██╗  ██╗ 
     ██╔══██╗██╔════╝ ██║██╔══██╗██║╚██╗██╔╝ 
     ███████║██║  ███╗██║██████╔╝██║ ╚███╔╝  
     ██╔══██║██║   ██║██║██╔═══╝ ██║ ██╔██╗  
     ██║  ██║╚██████╔╝██║██║     ██║██╔╝ ██╗ 
     ╚═╝  ╚═╝ ╚═════╝ ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ 
██╗      ██████╗  ██████╗  ██████╗ ███████╗██████╗ 
██║     ██╔═══██╗██╔════╝ ██╔════╝ ██╔════╝██╔══██╗
██║     ██║   ██║██║  ███╗██║  ███╗█████╗  ██████╔╝
██║     ██║   ██║██║   ██║██║   ██║██╔══╝  ██╔══██╗
███████╗╚██████╔╝╚██████╔╝╚██████╔╝███████╗██║  ██║
╚══════╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝
    Advanced ROS2 logging for Agipix Platform

Interactive CLI and ROS 2 autostart logger for recording ROS 2 bags and transferring files over TCP.

## Highlights
- Interactive menu when running `agi-logger` without arguments.
- Foreground recording with full rosbag output (Ctrl+C stops recording cleanly).
- Config-driven defaults from `cfg/configs.yaml` with an interactive editor.
- Settings editor split into Logger and TCP (Server/Client) sections with colorized output.
- TCP transfer automatically blocked while logging is active.
- ROS 2 autostart node that listens to `/fmu/out/vehicle_status` and starts recording on arming.
- Optional background recording via `--background`.

## Quick start
- Install (editable):
    ```bash
    cd /workspaces/logging/src/agi_logger
    pip install -e .
    ```
- Ensure ROS 2 and px4_msgs are installed and your ROS environment is sourced.
- Open interactive menu: `agi-logger`
- Show CLI help: `agi-logger --help`
- Start recording (foreground): `agi-logger record start`
- Start recording in background: `agi-logger record start --background`
- Stop background recording: `agi-logger record stop`
- Show status (background only): `agi-logger record status`
- Start ROS 2 autostart node: `agi-logger ros2 autostart`
- Open settings menu: `agi-logger settings`

Configuration defaults are loaded from `cfg/configs.yaml`.

## Interactive menu
Running `agi-logger` without arguments shows:
- Record: previews logger settings, then Enter = start / e = edit logger settings.
- Transfer: choose server/client, preview settings, then Enter = start / e = edit that section.
- Settings: opens the editor directly.

## Settings editor
- Logger settings: edit only the logger section values.
- TCP settings: choose Server or Client sections.
- Enter at value prompt keeps current value; Enter at selection list goes back.

## ROS 2 autostart node
Starts recording when `auto_start` is true and the vehicle is armed (PX4 `VehicleStatus`).
- Run: `agi-logger ros2 autostart`

## TCP transfer
Uses the config values in `cfg/configs.yaml`.
- Server: `agi-logger tcp send`
- Client: `agi-logger tcp receive`
- Auto mode: `agi-logger tcp run` (respects `tcp_file_communication.mode`)

TCP transfer is disabled while recording is active.

## Bash scripts
The repository includes legacy scripts for reference:

- [bags/bag_record.sh](bags/bag_record.sh): timed rosbag recorder with MCAP, compression, QoS overrides, max size, and per-bag metadata.
- [bags/play_bags.sh](bags/play_bags.sh): loops through `timed_bag_*` directories and plays them in order.

Example usage:
- `bash bags/bag_record.sh /topic1 /topic2 --name=run1 --path=~/bags --mcap --compress`
- `bash bags/play_bags.sh`
