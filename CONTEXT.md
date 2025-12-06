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
| `odom_scale` | 0.902521 | Odometry distance calibration |
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
| `move_skew` | 0.015 | MOVE lateral drift bias |
| `move_base_pwm` | 0.58 | MOVE base PWM |
| `move_timeout` | 20000 | MOVE timeout (ms) |
| `rotate_tol_dist` | 0.02 | ROTATE position tolerance (m) |
| `rotate_tol_angle` | 0.02 | ROTATE angle tolerance (rad) |
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
- **Dependencies**: rplidar-roboticia, confluent-kafka, pyserial
- **Kafka Topics**: robot.cmd, robot.telemetry, robot.map, robot.events

# Preferences & Conventions
- Code comments marked with `//THEANH` and `//THEANH END`
- Uses HAL library for STM32 peripheral access
- Odometry uses empirical scale factor (ODOM_DISTANCE_SCALE = 0.902521)
- MCU expects clockwise-positive rotation (command_delta = -world_delta)

# Open Questions / TODOs
- (none yet)

# Historical Notes / Archived
- [2025-12-06] Added SET_PARAM command for runtime STM32 configuration without reflashing
