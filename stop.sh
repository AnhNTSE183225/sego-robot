#!/bin/bash

# Terminate any running instances of the navigator
echo "Stopping odom_only_navigator.py..."
pkill -f odom_only_navigator.py

echo "Stop command sent. Robot navigator should be stopped."
