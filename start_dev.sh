#!/bin/bash

# Remove old log file for a clean start
echo "Cleaning up old logs..."
sudo rm -f robot.log

# Set environment variable to override Kafka bootstrap server address
export KAFKA_BOOTSTRAP_SERVERS="dev-api.sego.vn:9092"

# Run the navigator in detached mode
echo "Starting odom_only_navigator.py (DEV MODE) in the background..."
echo "Overriding bootstrap_servers to: $KAFKA_BOOTSTRAP_SERVERS"
nohup python3 odom_only_navigator.py > /dev/null 2>&1 &

echo "Robot navigator started detached in DEV MODE."
echo "You can monitor the logs with: tail -f robot.log"
