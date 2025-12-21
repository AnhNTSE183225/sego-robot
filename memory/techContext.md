---
trigger: always_on
---

# Tech Context

## Runtime
- **Python**: `odom_only_navigator.py` (Navigator + Kafka + LIDAR)
- **Config**: `robot_config.json` (required; centralizes serial, lidar, kafka, motion, planner, STM32 params)
- **Management Scripts**: `start.sh`, `stop.sh`, `start_dev.sh` (handles detached execution and env overrides)
- **Logs**: `robot.log` (can be cleaned via `start.sh`)

## Dependencies (not exhaustive)
- `pyserial` (STM32 UART)
- `rplidar-roboticia` (RPLidar scans)
- `confluent-kafka` (Kafka producer/consumer)
- Supports environment variables for overrides (e.g. `KAFKA_BOOTSTRAP_SERVERS`)

## Hardware/Protocols (high level)
- STM32 receives commands like `MOVE <m>` and `ROTATE_DEG <deg>` and emits status lines including `OK ...`, `TARGET_REACHED`, `TIMEOUT`.