#!/bin/bash
###############################################################################
# ROS 2 timed bag recorder
#
# Features:
# - Optional MCAP storage (--mcap)
# - Optional compression (--compress, works only with MCAP)
# - Optional QoS overrides
# - Optional max bag size (--max_bag_size=<GB>)
# - Optional duration (--duration=<minutes>)
# - Optional save path (--path)
# - Optional extra name (--name)
# - Writes per-bag metadata after recording
# - Graceful stop on SIGINT / SIGTERM
###############################################################################

set -euo pipefail

############################
# Defaults
############################
DURATION_MIN=0         # 0 = unlimited until manual stop
MAX_BAG_SIZE_GB=0      # 0 = unlimited
MIN_FREE_GB=20
QOS_FILE="qos.yaml"

############################
# Argument handling
############################
if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <topic1> [topic2 ...] [--name=<name>] [--path=<bag_path>] [--compress] [--mcap] [--max_bag_size=<GB>] [--duration=<minutes>]"
  exit 1
fi

TOPICS=()
BAG_PATH="."
EXTRA_NAME=""
COMPRESS=false
USE_MCAP=false

# Parse arguments
for arg in "$@"; do
  case "$arg" in
    --name=*)
      EXTRA_NAME="${arg#--name=}"
      ;;
    --path=*)
      BAG_PATH="${arg#--path=}"
      ;;
    --compress)
      COMPRESS=true
      ;;
    --mcap)
      USE_MCAP=true
      ;;
    --max_bag_size=*)
      MAX_BAG_SIZE_GB="${arg#--max_bag_size=}"
      ;;
    --duration=*)
      DURATION_MIN="${arg#--duration=}"
      ;;
    *)
      TOPICS+=("$arg")
      ;;
  esac
done

# Convert GB to bytes and minutes to seconds
MAX_BAG_SIZE_BYTES=$((MAX_BAG_SIZE_GB * 1024 * 1024 * 1024))
DURATION_SEC=$((DURATION_MIN * 60))

############################
# Sanity checks
############################
command -v ros2 >/dev/null || { echo "ERROR: ros2 command not found in PATH."; exit 1; }

############################
# Cleanup handler
############################
cleanup() {
  echo
  echo "======================================="
  echo "Stop signal received."
  if [[ -n "${BAG_PID:-}" ]]; then
    echo "Sending SIGINT to ros2 bag process (PID=$BAG_PID)..."
    kill -INT "$BAG_PID" 2>/dev/null || true
    echo "Waiting for recorder to shut down cleanly..."
    wait "$BAG_PID" || true
    echo "Recorder stopped."
  else
    echo "No active recorder process."
  fi
  echo "Exiting."
  echo "======================================="
  exit 0
}
trap cleanup SIGINT SIGTERM

############################
# Infinite recording loop
############################
while true; do
  FREE_GB=$(df --output=avail -BG "$BAG_PATH" | tail -1 | tr -dc '0-9')
  if (( FREE_GB < MIN_FREE_GB )); then
    echo "======================================="
    echo "STOP CONDITION: Low disk space ($FREE_GB GB < $MIN_FREE_GB GB)"
    exit 1
  fi

  BAG_NAME="agi_log_$(date +%Y%m%d_%H%M%S)"
  [[ -n "$EXTRA_NAME" ]] && BAG_NAME="${BAG_NAME}_${EXTRA_NAME}"
  FULL_BAG_PATH="${BAG_PATH}/${BAG_NAME}"

  echo
  echo "======================================="
  echo "Starting new recording:"
  echo "  Bag path   : $FULL_BAG_PATH"
  echo "  Duration   : $([[ "$DURATION_SEC" -gt 0 ]] && echo "${DURATION_MIN} minutes" || echo "unlimited")"
  echo "  Topics     : ${TOPICS[*]}"
  echo "  MCAP       : $([[ "$USE_MCAP" = true ]] && echo "enabled" || echo "disabled")"
  echo "  Compression: $([[ "$COMPRESS" = true ]] && echo "enabled" || echo "disabled")"
  echo "  Max size   : $([[ "$MAX_BAG_SIZE_BYTES" -gt 0 ]] && echo "${MAX_BAG_SIZE_GB} GB" || echo "unlimited")"
  echo "======================================="

  ############################
  # Build ros2 bag command
  ############################
  CMD=(ros2 bag record -o "$FULL_BAG_PATH")

  # Storage selection
  if [[ "$USE_MCAP" = true ]]; then
    CMD+=(--storage mcap)
    [[ "$COMPRESS" = true ]] && CMD+=(--compression-mode file --compression-format zstd)
  fi

  # QoS overrides
  [[ -n "$QOS_FILE" && -f "$QOS_FILE" ]] && CMD+=(--qos-profile-overrides-path "$QOS_FILE")

  # Max bag size
  [[ "$MAX_BAG_SIZE_BYTES" -gt 0 ]] && CMD+=(--max-bag-size "$MAX_BAG_SIZE_BYTES")

  # Topics
  CMD+=("${TOPICS[@]}")

  ############################
  # Launch recorder
  ############################
  "${CMD[@]}" &
  BAG_PID=$!
  echo "Recorder running (PID=$BAG_PID)."

  ############################
  # Timed run (optional)
  ############################
  if [[ "$DURATION_SEC" -gt 0 ]]; then
    sleep "$DURATION_SEC" || true
    echo
    echo "======================================="
    echo "STOP CONDITION: Time limit reached"
    echo "Stopping recording: $BAG_NAME"
    echo "======================================="
    kill -INT "$BAG_PID" 2>/dev/null || true
    wait "$BAG_PID" || true
  else
    echo "Recording will continue until manual stop (CTRL+C)..."
    wait "$BAG_PID"
  fi

  ############################
  # Metadata creation
  ############################
  METADATA_FILE="${FULL_BAG_PATH}/metadata.txt"
  {
    echo "bag_name: $BAG_NAME"
    echo "bag_path: $BAG_PATH"
    echo "date: $(date --iso-8601=seconds)"
    echo "hostname: $(hostname)"
    echo "user: $(whoami)"
    echo "ros_distro: ${ROS_DISTRO:-unknown}"
    echo "kernel: $(uname -r)"
    echo "topics:"
    for t in "${TOPICS[@]}"; do
      echo "  - $t"
    done
    echo "storage: $([[ "$USE_MCAP" = true ]] && echo "MCAP" || echo "default")"
    echo "compression: $([[ "$COMPRESS" = true ]] && echo "enabled" || echo "disabled")"
    echo "max_bag_size: $([[ "$MAX_BAG_SIZE_BYTES" -gt 0 ]] && echo "${MAX_BAG_SIZE_GB} GB" || echo "unlimited")"
    echo "duration: $([[ "$DURATION_SEC" -gt 0 ]] && echo "${DURATION_MIN} minutes" || echo "unlimited")"
    echo "qos_override_file: ${QOS_FILE:-none}"
    echo "git_commit: $(git rev-parse HEAD 2>/dev/null || echo n/a)"
  } > "$METADATA_FILE"

  echo "Bag saved successfully: $FULL_BAG_PATH"

  # If unlimited duration, exit loop (recording handled by user)
  [[ "$DURATION_SEC" -eq 0 ]] && break
done
