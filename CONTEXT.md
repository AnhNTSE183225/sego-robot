# User Profile
- Name: THEANH (appears in code comments)
- Working on robotics project with STM32 + Raspberry Pi

# Current Projects
- [Project] SeGo Robot – Omni-wheel robot with STM32 firmware + Python navigator
  - Repository: `c:\GIT\sego-robot`
  - Components: STM32F1 MCU firmware, Raspberry Pi navigator, RPLidar obstacle avoidance, Kafka telemetry

# Technical Environment

## Hardware Architecture
- **MCU**: STM32F1xx (Blue Pill or similar)
- **SBC**: Raspberry Pi (runs Python navigator)
- **LIDAR**: RPLidar (connected via USB to Pi at `/dev/ttyUSB0`)
- **Motors**: 3 omni-wheels with DC motors + encoders
- **Communication**: UART @ 115200 baud (`/dev/ttyAMA0` on Pi ↔ USART3 on STM32)

## Robot Parameters
- Wheel diameter: 58mm (0.058m)
- Robot radius (center to wheel): 105mm (0.105m)
- Encoder CPR: 52 counts/revolution
- Gear ratio: 34:1
- Odometry update rate: 50 Hz
- Wheel position angles: 60°, 180°, 300°
- Wheel drive angles: 150°, 270°, 30°

## STM32 Pin Mapping
- **PWM (TIM1)**: CH1→Motor1, CH2→Motor2, CH3→Motor3
- **Encoders**: TIM2→Motor1, TIM3→Motor2, TIM4→Motor3
- **Motor Direction GPIO (GPIOB)**:
  - Motor1: W1_A=PB0, W1_B=PB1
  - Motor2: W2_A=PB13, W2_B=PB12
  - Motor3: W3_A=PB8, W3_B=PB9

## Communication Protocol

### STM32 → Pi (Odometry @ 50Hz)
```
x,y,theta,vx,vy,omega,m1,m2,m3\r\n
```
- x,y in meters; theta in radians; vx,vy in m/s; omega in rad/s; m1,m2,m3 encoder deltas

### Pi → STM32 (Commands)
| Command | Format | Description |
|---------|--------|-------------|
| STOP | `STOP` | Emergency stop |
| RESET_ODOM | `RESET_ODOM` | Reset odometry to (0,0,0) |
| MOVE | `MOVE distance` | PID forward movement (meters) |
| ROTATE_DEG | `ROTATE_DEG angle` | Rotate by angle (degrees) |
| SET | `SET dir1,dir2,dir3` | Set motor directions (F/R/S) |
| RUN | `RUN dir duration_ms` | Timed motor run |
| RUNP | `RUNP dir1,dir2,dir3 pwm duration_ms` | PWM-controlled run |
| SET_PARAM | `SET_PARAM name value` | Set runtime config parameter |

### SET_PARAM Parameters
Configurable at runtime from Python without reflashing STM32:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `odom_scale` | 0.902521 | Odometry distance calibration (linear) |
| `odom_angular_scale` | 0.95 | Odometry angular calibration (rotation only) |
| `linear_k` | 1.0 | Position P-gain |
| `angular_k` | 0.55 | Rotation P-gain |
| `max_speed` | 0.8 | Max wheel speed |
| `min_duty_linear` | 0.45 | Deadband for linear motion |
| `min_duty_rotate` | 0.48 | Deadband for rotation |
| `min_duty_brake` | 0.18 | Minimum during braking |
| `move_kp` | -0.0040 | MOVE PID P-gain |
| `move_ki` | -0.00015 | MOVE PID I-gain |
| `move_kd` | -0.0008 | MOVE PID D-gain |
| `move_min_pwm` | 0.46 | MOVE min correction PWM |
| `move_max_pwm` | 0.70 | MOVE max correction PWM |
| `move_smoothing` | 0.25 | MOVE output low-pass filter |
| `move_skew` | 0.017 | MOVE lateral drift bias (calibrated) |
| `move_base_pwm` | 0.58 | MOVE base PWM |
| `move_timeout` | 20000 | MOVE timeout (ms) |
| `rotate_tol_dist` | 0.02 | ROTATE position tolerance (m) |
| `rotate_tol_angle` | 0.087 | ROTATE angle tolerance (rad, ~5°) |
| `rotate_timeout` | 10000 | ROTATE timeout (ms) |
| `move_dist_tol_dist` | 0.01 | MOVE_DIST position tolerance (m) |
| `move_dist_tol_angle` | 0.05 | MOVE_DIST angle tolerance (rad) |
| `move_dist_timeout` | 10000 | MOVE_DIST timeout (ms) |
| `angular_rate_tol` | 0.05 | Angular rate for "stopped" (rad/s) |

Python sends all params via `STM32_PARAMS` dict in `odom_only_navigator.py` at startup.

### STM32 Responses
- `OK <CMD>` – Command acknowledged
- `ERR <reason>` – Error
- `TARGET_REACHED` / `MOVE TARGET_REACHED` – Motion complete
- `TIMEOUT` / `MOVE TIMEOUT` – Motion timed out

## Software Stack
- **STM32 Firmware**: C (HAL library), STM32CubeIDE project at `robot/`
- **Navigator**: Python 3 (`odom_only_navigator.py`)
- **Test Scripts**: `test_run.py`, `test_rotate.py` - standalone testers for debugging
- **Config**: `robot_config.json` - all settings in one file (replaces env vars)
- **Dependencies**: rplidar-roboticia, confluent-kafka, pyserial
- **Kafka Topics**: robot.cmd, robot.telemetry, robot.map, robot.events

## Configuration File (`robot_config.json`)
All settings are centralized in `robot_config.json`:
- `robot.id` - Robot UUID for Kafka
- `serial.*` - Serial port settings
- `lidar.*` - LIDAR port and offset
- `kafka.*` - Kafka bootstrap and topic names
- `logging.*` - Log file and level
- `motion.*` - Tolerances, timeouts, axis-aligned mode
- `obstacle_avoidance.*` - LIDAR-based avoidance parameters
- `path_planning.*` - A* planner settings
- `stm32_params.*` - Parameters sent to STM32 via SET_PARAM

Config file is **required** - scripts throw error if missing.

# Preferences & Conventions
- **No fallbacks** - always one solution, fail fast if config/dependencies missing
- Code comments marked with `//THEANH` and `//THEANH END`
- Uses HAL library for STM32 peripheral access
- Odometry uses empirical scale factor (ODOM_DISTANCE_SCALE = 0.902521)
- MCU expects clockwise-positive rotation (command_delta = -world_delta)
- Testing A* path planning (LIDAR-based) for obstacle avoidance instead of axis-aligned detours; needs min/max MOVE and ROTATE_DEG command thresholds in `robot_config.json` to prevent timeouts on too-small motions

# Open Questions / TODOs
- [TODO] Pose tracking / odometry sync with STM32 deferred (issue #2 to be fixed later)

# Historical Notes / Archived
- [2025-12-09] Navigator switched to A* path planning with live LIDAR-obstacle blocking (drops axis-aligned detour state machine); MOVE/ROTATE commands now clamped by min/max values in `robot_config.json` (motion.min_move_command_m, motion.max_move_command_m, motion.min_rotate_deg, motion.max_rotate_deg; planner max replans configurable)
- [2025-12-09] Rotation min command raised to 20° and timeout tolerance widened to 20° in `robot_config.json` to accept small-angle TIMEOUTs as success
- [2025-12-10] Increased rotate drive authority for small turns (`stm32_params.min_duty_rotate` 0.52, `stm32_params.move_base_pwm` 0.62) to help complete 20° rotations without hitting timeout
- [2025-12-10] Further raised rotate drive floor (`stm32_params.min_duty_rotate` 0.60) to push through stiction on small-angle rotations
- [2025-12-10] Raised rotate drive floor (`stm32_params.min_duty_rotate` 0.63) to balance 20° completion with reduced 90° overshoot; `motion.max_rotate_deg` capped at 150° to avoid single large spins; later user set `motion.min_rotate_deg` to 26 and `stm32_params.min_duty_rotate` to 0.55 for further tuning
- [2025-12-10] A* planner now treats boundary/edge points as inside with a looser epsilon (1e-3) to keep boundary goals in free space
- [2025-12-10] A* grid indexing switched to floor-based to avoid skipped cells from rounding (improves pathfinding on tight maps)
- [2025-12-10] Planner snaps start/goal to nearest free grid cell if sampling misses them (prevents “no path” on tiny maps)
- [2025-12-08] Kafka status loop tightened to ~1s and publishes in-flight pose by fusing live STM32 odom; lateral odom ignored on axis-aligned moves (X move ignores Y drift and vice versa)
- [2025-12-08] Navigator now consumes STM32 odometry deltas directly (no RESET_ODOM between commands) for MOVE/ROTATE pose updates
- [2025-12-08] Rotation handling now applies STM32 odom delta (and retries residual error once) to keep heading in sync; detects sign mismatch and stops instead of drifting
- [2025-12-08] Added static boundary enforcement to forward-clear checks to prevent driving outside map; added axis correction during L-shape navigation to pull robot back to target axis after detours
- [2025-12-07] **CRITICAL BUG FIX**: Robot couldn't move perpendicular during obstacle avoidance - `cos(90°)=0` was causing ±90° offsets to be skipped. Now allows perpendicular movement in axis-aligned mode.
- [2025-12-07] Increased `max_side_switches` from 5 → 10 for more persistent obstacle avoidance
- [2025-12-07] Fixed obstacle avoidance to use only ±90° offsets when axis-aligned mode enabled (was trying 15°, 30°, 45°... causing cumulative drift)
- [2025-12-07] Enabled `axis_aligned_moves` mode to reduce cumulative heading errors from arbitrary rotations (A* pathfinding was causing 18-30° drift over multiple rotations)
- [2025-12-07] Calibrated `move_skew` from 0.015 → 0.017 to eliminate ~4.5cm/90cm lateral drift
- [2025-12-07] Increased `rotate_tol_angle` to 0.087 rad (~5°) to match odom under-reporting
- [2025-12-07] Added `odom_angular_scale` parameter for independent rotation calibration (calibrated to 0.95 for accurate 90° rotation)
- [2025-12-06] Added SET_PARAM command for runtime STM32 configuration without reflashing
- [2025-12-06] Added `robot_config.json` to centralize all settings (replaces env vars)
- [2025-12-06] Added `test_run.py` and `test_rotate.py` for standalone command testing
- [2025-12-06] Removed fallback configs - config file now required, fail fast on missing
