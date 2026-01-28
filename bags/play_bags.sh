#!/usr/bin/env bash
set -euo pipefail

# Clean exit on Ctrl+C or SIGTERM
cleanup() {
  echo
  echo "Interrupted—exiting."
  exit 0
}
trap cleanup SIGINT SIGTERM

while true; do
  # enable nullglob so that the pattern expands to empty if no matches
  shopt -s nullglob
  bags=(timed_bag_*/)
  shopt -u nullglob

  if [ ${#bags[@]} -eq 0 ]; then
    echo "No timed_bag_* directories found. Retrying in 5s..."
    sleep 5
    continue
  fi

  # sort them lexicographically (which, given your timestamp naming,
  # is the same as chronological order)
  IFS=$'\n' sorted=($(sort <<<"${bags[*]}"))
  unset IFS

  for bag_dir in "${sorted[@]}"; do
    # strip trailing slash for prettier output
    bag_name="${bag_dir%/}"
    echo "▶️  Playing $bag_name …"
    ros2 bag play "$bag_name" --rate 1.0
    echo "✅  Finished $bag_name."
  done

  echo "🔁  All bags played. Restarting sequence…"
done

