---
trigger: always_on
---

# System Patterns

## Architecture
- **STM32 firmware**: Executes closed-loop motion to reach targets; streams odometry; emits `TARGET_REACHED`/`TIMEOUT`.
- **Python navigator** (`odom_only_navigator.py`): High-level command execution, map anchoring, obstacle checks (static + LIDAR), and Kafka bridge.
- **Kafka bridge**: Receives commands and publishes telemetry/status and map verdict events.
- **RPLidar**: Provides scans used for forward clearance checks and during-motion monitoring.

## Key Patterns / Conventions
- **Map anchoring**: Rotate/translate boundary/obstacles/POIs so the first boundary edge aligns to current pose heading at load time.
- **Command clamping**: Motion commands are clamped by config (`motion.min_*`, `motion.max_*`) to keep STM32 behavior stable.
- **Discrete rotation primitives**: Rotations are snapped to allowed angles (e.g., 30/60/90/120/180) with per-angle calibration/params. **Crucially, the software records the intended (logical) angle in the pose, regardless of the calibrated command sent to the motors**, to ensure world-frame coordinate integrity.
- **Dead-reckoning position tracking**: Position updates use accumulated X-axis magnitude from STM32 odometry, ignoring Y-axis noise during forward movement. Both normal completion and emergency stops use this consistent approach.
- **Perimeter verification**: Implemented as a sequence of "rotate + move" segments to boundary vertices (no detours).
- **Navigation point management (POI/Counter)**: Uses update-or-create pattern to preserve IDs and prevent duplicates. Frontend sends entity IDs when updating, backend checks existence and updates in-place or creates new. Prevents delete+recreate cycles that generate new UUIDs.
