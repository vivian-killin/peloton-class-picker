#!/usr/bin/env bash
# Double-click this file to start the class picker.
# It opens the interface in your browser. Close this window when you're done.

cd "$(dirname "$0")" || exit 1

echo "Starting your Peloton class picker..."
echo "The interface will open in your browser in a moment."
echo
echo "Leave this window open while you use it."
echo "To stop: close this window, or press Control-C."
echo

python3 peloton_picker.py serve

echo
echo "Stopped. You can close this window."
