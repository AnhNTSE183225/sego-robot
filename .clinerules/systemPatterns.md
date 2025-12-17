# System Patterns

## Architecture
- **STM32 firmware**: Executes closed-loop motion to reach targets; streams odometry; emits `TARGET_REACHED`/`TIMEOUT`.
- **Python navigator** (`odom_only_navigator.py`): High-level command execution, map anchoring, obstacle checks (static + LIDAR), and Kafka bridge.
- **Kafka bridge**: Receives commands and publishes telemetry/status and map verdict events.
- **RPLidar**: Provides scans used for forward clearance checks and during-motion monitoring.

## Key Patterns / Conventions
- **Map anchoring**: Rotate/translate boundary/obstacles/POIs so the first boundary edge aligns to current pose heading at load time.
- **Command clamping**: Motion commands are clamped by config (`motion.min_*`, `motion.max_*`) to keep STM32 behavior stable.
- **Discrete rotation primitives**: Rotations are snapped to allowed angles (e.g., 30/60/90/120/180) with per-angle calibration/params.
- **Perimeter verification**: Implemented as a sequence of “rotate + move” segments to boundary vertices (no detours).

