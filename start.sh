#!/bin/bash

# Remove old log file for a clean start
echo "Cleaning up old logs..."
sudo rm -f robot.log

# Run the navigator in detached mode
echo "Starting odom_only_navigator.py in the background..."
nohup python3 odom_only_navigator.py > /dev/null 2>&1 &

echo "Robot navigator started detached."
echo "You can monitor the logs with: tail -f robot.log"
