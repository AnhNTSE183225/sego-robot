---
trigger: always_on
---

# Tech Context

## Runtime
- **Python**: `odom_only_navigator.py` (Navigator + Kafka + LIDAR)
- **Config**: `robot_config.json` (required; centralizes serial, lidar, kafka, motion, planner, STM32 params)
- **Logs**: `robot.log`

## Dependencies (not exhaustive)
- `pyserial` (STM32 UART)
- `rplidar-roboticia` (RPLidar scans)
- `confluent-kafka` (Kafka producer/consumer)

## Hardware/Protocols (high level)
- STM32 receives commands like `MOVE <m>` and `ROTATE_DEG <deg>` and emits status lines including `OK ...`, `TARGET_REACHED`, `TIMEOUT`.