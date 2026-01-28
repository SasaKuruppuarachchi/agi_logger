#!/bin/bash

# Duration of each recording in seconds (10 minutes)
DURATION=600

# Clean up on SIGINT or SIGTERM
cleanup() {
  echo
  echo "Received stop signal, shutting down..."
  if [[ -n "$BAG_PID" ]]; then
    kill -2 "$BAG_PID"  # graceful shutdown of ros2 bag
    wait "$BAG_PID"
  fi
  exit 0
}
trap cleanup SIGINT SIGTERM

# Infinite loop
while true; do
  # Generate timestamped bag name
  BAG_NAME="timed_bag_$(date +%Y%m%d_%H%M%S)"
  echo "Starting new recording: $BAG_NAME (PID will follow)..."

  # Launch recorder in background
  ros2 bag record --storage mcap \
    --compression-mode file \
    --compression-format zstd -o "$BAG_NAME" \
    /ground0/livox/imu \
    /ground0/livox/lidar \
    /image_raw \
    /tf \
    /tf_static \
    /ground0/map \
    /ground0/dlio/odom_node/pose \
    /ground0/dlio/odom_node/path \
    /ground0/dlio/odom_node/pointcloud/keyframe \
    /ground0/dlio/odom_node/pointcloud/deskewed \
    /ground0/dlio/odom_node/odom \
    /ground0/imu/data &
  BAG_PID=$!
  echo "  Recording PID: $BAG_PID"

  # Let it run for the specified duration
  sleep "$DURATION"

  # Stop recording gracefully
  echo "Stopping recording $BAG_NAME..."
  kill -2 "$BAG_PID"
  wait "$BAG_PID"

  echo "Saved bag: $BAG_NAME"
  echo "---------------------------------------"
done

