import json
import logging
import logging.handlers
import math
import queue
import serial
import sys
import threading
import time
from pathlib import Path
from typing import Optional


def load_robot_config():
    """Load configuration from robot_config.json. Raises error if file missing."""
    config_path = Path(__file__).parent / "robot_config.json"
    
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Please create robot_config.json with required settings."
        )
    
    with open(config_path, 'r') as f:
        return json.load(f)


# Load config once at module level
ROBOT_CONFIG = load_robot_config()

LOG_FILE = ROBOT_CONFIG.get('logging', {}).get('file', 'robot.log')
LOG_LEVEL = ROBOT_CONFIG.get('logging', {}).get('level', 'INFO').upper()


def configure_logging():
    """Configure console + rotating file logging.
    
    Console: INFO level (or ROBOT_LOG_LEVEL env var)
    File: INFO level to keep logs from being too noisy
    """
    console_level = getattr(logging, LOG_LEVEL, logging.INFO)
    file_level = logging.INFO  # File logs at INFO to reduce noise
    
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    root.setLevel(logging.DEBUG)  # Root accepts all, handlers filter

    # Console: INFO (hoặc theo env var)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(formatter)

    # File: INFO (luôn luôn để debug chi tiết)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)
    
    # Log startup info (shows in both console and file)
    logger = logging.getLogger("odom.navigator")
    logger.info(f"Logging configured: Console={logging.getLevelName(console_level)}, File={logging.getLevelName(file_level)} -> {LOG_FILE}")


try:
    from rplidar import RPLidar, RPLidarException
except ImportError:
    logging.getLogger("odom.navigator").error(
        "RPLidar library not found. Install with 'pip install rplidar-roboticia'."
    )
    RPLidar = None

    class RPLidarException(Exception):
        pass
else:
    # Some rplidar builds return 3 values from get_health(); force a 2-tuple for compatibility.
    _orig_get_health = RPLidar.get_health

    def _get_health_two_fields(self):
        result = _orig_get_health(self)
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            return result[0], result[1]
        return result, 0

    RPLidar.get_health = _get_health_two_fields

try:
    from confluent_kafka import Consumer, Producer, KafkaException
except ImportError:
    Consumer = None
    Producer = None
    KafkaException = Exception

from odom_kafka_bridge import KafkaBridge


# Serial configuration (from config file)
SERIAL_PORT = ROBOT_CONFIG.get('serial', {}).get('port', '/dev/ttyAMA0')
BAUD_RATE = ROBOT_CONFIG.get('serial', {}).get('baud_rate', 115200)

# STM32 configurable parameters (sent on startup via SET_PARAM)
# These can be tuned without reflashing the STM32
STM32_PARAMS = ROBOT_CONFIG.get('stm32_params', {
    'odom_scale': 0.902521,
    'linear_k': 1.0,
    'angular_k': 0.55,
    'max_speed': 0.8,
    'min_duty_linear': 0.45,
    'min_duty_rotate': 0.48,
    'min_duty_brake': 0.18,
    'move_kp': -0.0040,
    'move_ki': -0.00015,
    'move_kd': -0.0008,
    'move_min_pwm': 0.46,
    'move_max_pwm': 0.70,
    'move_smoothing': 0.25,
    'move_skew': 0.015,
    'move_base_pwm': 0.58,
    'move_timeout': 20000,
    'rotate_tol_dist': 0.02,
    'rotate_tol_angle': 0.02,
    'rotate_timeout': 10000,
    'move_dist_tol_dist': 0.01,
    'move_dist_tol_angle': 0.05,
    'move_dist_timeout': 10000,
    'angular_rate_tol': 0.05,
})

# LIDAR configuration (from config file)
LIDAR_PORT = ROBOT_CONFIG.get('lidar', {}).get('port', '/dev/ttyUSB0')
LIDAR_FRONT_OFFSET_DEG = ROBOT_CONFIG.get('lidar', {}).get('front_offset_deg', 119.0)

# Kafka configuration (from config file)
_kafka_cfg = ROBOT_CONFIG.get('kafka', {})
KAFKA_BOOTSTRAP_SERVERS = _kafka_cfg.get('bootstrap_servers', 'localhost:9092')
KAFKA_TOPIC_COMMAND = _kafka_cfg.get('topic_command', 'robot.cmd')
KAFKA_TOPIC_TELEMETRY = _kafka_cfg.get('topic_telemetry', 'robot.telemetry')
KAFKA_TOPIC_MAP = _kafka_cfg.get('topic_map', 'robot.map')
KAFKA_TOPIC_EVENTS = _kafka_cfg.get('topic_events', 'robot.events')
ROBOT_ID = ROBOT_CONFIG.get('robot', {}).get('id')

# Motion tolerances (from config file)
_motion_cfg = ROBOT_CONFIG.get('motion', {})
DISTANCE_TOLERANCE_M = _motion_cfg.get('distance_tolerance_m', 0.02)
ANGLE_TOLERANCE_DEG = _motion_cfg.get('angle_tolerance_deg', 2.0)
MOVE_TIMEOUT_SEC = _motion_cfg.get('move_timeout_sec', 25.0)
ROTATE_TIMEOUT_SEC = _motion_cfg.get('rotate_timeout_sec', 15.0)
AXIS_ALIGNED_MOVES = _motion_cfg.get('axis_aligned_moves', False)
HEADING_OFFSET_DEG = _motion_cfg.get('heading_offset_deg', 0.0)
AXIS_DRIFT_TOLERANCE_M = max(DISTANCE_TOLERANCE_M * 1.5, 0.03)  # How far we allow drift off-axis before correcting
DETOUR_AXIS_ALIGNED = _motion_cfg.get('detour_axis_aligned', True)  # Detours snap to axis
PREFER_CW_FOR_180 = _motion_cfg.get('prefer_cw_for_180', True)  # Prefer clockwise when delta ~180°
MIN_MOVE_COMMAND_M = _motion_cfg.get('min_move_command_m', 0.05)
MAX_MOVE_COMMAND_M = _motion_cfg.get('max_move_command_m', 1.0)
MIN_ROTATE_COMMAND_DEG = _motion_cfg.get('min_rotate_deg', 5.0)
MAX_ROTATE_COMMAND_DEG = _motion_cfg.get('max_rotate_deg', 180.0)

# Obstacle avoidance parameters (from config file)
_obstacle_cfg = ROBOT_CONFIG.get('obstacle_avoidance', {})
MOVE_STEP_M = _obstacle_cfg.get('move_step_m', 0.25)
CLEARANCE_MARGIN_M = _obstacle_cfg.get('clearance_margin_m', 0.05)
OBSTACLE_STOP_DISTANCE_M = _obstacle_cfg.get('obstacle_stop_distance_m', 0.25)
OBSTACLE_LOOKAHEAD_M = _obstacle_cfg.get('obstacle_lookahead_m', 1.2)
ROBOT_RADIUS_M = _obstacle_cfg.get('robot_radius_m', 0.15)
CORRIDOR_HALF_WIDTH_M = _obstacle_cfg.get('corridor_half_width_m', 0.20)
SIDE_WALL_MIN_M = _obstacle_cfg.get('side_wall_min_m', 0.05)
SIDE_WALL_MAX_M = _obstacle_cfg.get('side_wall_max_m', 0.80)
SIDE_CONE_DEG = _obstacle_cfg.get('side_cone_deg', 60.0)
FORWARD_SCAN_ANGLE_DEG = _obstacle_cfg.get('forward_scan_angle_deg', 140.0)
DETOUR_SCAN_ANGLE_DEG = _obstacle_cfg.get('detour_scan_angle_deg', 120.0)
DETOUR_ANGLE_STEP_DEG = _obstacle_cfg.get('detour_angle_step_deg', 15.0)
DETOUR_MAX_ANGLE_DEG = _obstacle_cfg.get('detour_max_angle_deg', 90.0)
BLOCKED_RETRY_WAIT_SEC = _obstacle_cfg.get('blocked_retry_wait_sec', 0.4)
MAX_BLOCKED_RETRIES = _obstacle_cfg.get('max_blocked_retries', 25)
MAX_SIDE_SWITCHES = _obstacle_cfg.get('max_side_switches', 5)
START_MIN_CLEARANCE_M = _obstacle_cfg.get('start_min_clearance_m', 0.25)
ROTATE_TIMEOUT_TOLERANCE_DEG = _obstacle_cfg.get('rotate_timeout_tolerance_deg', 15.0)
MIN_VALID_LIDAR_DIST_M = _obstacle_cfg.get('min_valid_lidar_dist_m', 0.20)

# Path planning (from config file)
_path_cfg = ROBOT_CONFIG.get('path_planning', {})
ASTAR_GRID_STEP_M = _path_cfg.get('astar_grid_step_m', 0.05)
ASTAR_MAX_NODES = _path_cfg.get('astar_max_nodes', 20000)
PLANNER_MAX_REPLANS = _path_cfg.get('max_replans', 10)


def normalize_angle_deg(angle):
    """Wrap an angle in degrees to [-180, 180]."""
    while angle > 180.0:
        angle -= 360.0
    while angle < -180.0:
        angle += 360.0
    return angle


def lidar_angle_to_robot(angle_deg: float) -> float:
    """
    Convert raw LIDAR angle (per RPLidar frame) into robot-forward frame.
    After conversion: 0deg = robot forward, +90deg = robot left, -90deg = robot right.
    """
    return normalize_angle_deg(-angle_deg + LIDAR_FRONT_OFFSET_DEG)


class OdomOnlyNavigator:
    """
    Navigator that relies on STM32 odometry but uses an RPLidar for real-time
    obstacle detection and on-the-fly detours.
    """

    def __init__(self):
        self.logger = logging.getLogger("odom.navigator")
        self.serial_conn = None
        self.response_queue = queue.Queue()
        
        # Command-based pose tracking (dead reckoning)
        # Dead reckoning làm chính, odom từ STM32 dùng khi interrupted
        self.pose_lock = threading.Lock()
        self.cmd_pose = {
            'x': 0.0,
            'y': 0.0,
            'heading_deg': 0.0,  # degrees, 0 = +X, 90 = +Y
        }
        
        # Latest STM32 odometry (relative to last RESET_ODOM)
        # Dùng khi bị interrupt để biết thực tế đã đi bao xa
        self.stm32_odom_lock = threading.Lock()
        self.stm32_odom = {
            'x': 0.0,
            'y': 0.0,
            'heading_deg': 0.0,
        }
        # Track active motion for live status fusion
        self.active_motion_lock = threading.Lock()
        self.active_motion = None  # {'start_pose':..., 'odom_start':...}

        # LIDAR state
        self.lidar = None
        self.lidar_thread = None
        self.lidar_running = False
        self.scan_lock = threading.Lock()
        self.latest_scan = []
        self.first_scan_event = threading.Event()

        # Kafka
        self.kafka_bridge = None
        self.command_queue = queue.Queue()
        self.map_definition = None
        self.map_definition_correlation = None
        self.current_map_id = None
        self.obstacles = []
        self.boundary_polygon = None
        self.points_of_interest = []
        self.map_anchor_applied = False
        # Anchor metadata (raw -> anchor). Used to convert pose back to raw frame when publishing status.
        self.anchor_rotation_deg = None
        self.anchor_p0_raw = None
        self.anchor_pose_at_load = None
        self.map_loaded_event = threading.Event()
        self.status_thread = None
        self.status_running = False

    # --- Connection management ---
    def _connect(self):
        if RPLidar is None:
            self.logger.error("RPLidar dependency missing; cannot perform obstacle avoidance.")
            return False

        self.logger.info("Connecting to STM32 controller and RPLidar...")
        try:
            self.serial_conn = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            time.sleep(1.0)
        except serial.SerialException as exc:
            self.logger.error(f"Error opening {SERIAL_PORT}: {exc}")
            return False

        try:
            self.lidar = RPLidar(LIDAR_PORT)
            self.lidar.start_motor()
            self._start_lidar_listener()
        except RPLidarException as exc:
            self.logger.error(f"Error connecting to LIDAR on {LIDAR_PORT}: {exc}")
            self._safe_close_serial()
            return False

        self._start_serial_listener()
        time.sleep(0.2)  # Give serial listener time to start
        
        # Configure STM32 parameters before any motion commands
        self._configure_stm32_params()
        
        self._send_raw_command("RESET_ODOM")
        self._reset_pose()  # Reset internal pose tracking
        self._reset_stm32_odom()  # Reset STM32 odom cache

        # Give the scanner a moment to deliver its first frames
        if not self.first_scan_event.wait(timeout=3.0):
            self.logger.warning("No LIDAR data received yet; motion will pause until scans arrive.")

        self.logger.info("Connected. Pose reset to (0,0,0).")
        return True

    def _safe_close_serial(self):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass

    def _disconnect(self):
        self.logger.info("Disconnecting...")
        self._stop_serial_listener()
        self._stop_lidar_listener()
        if self.lidar:
            try:
                self.lidar.stop()
                self.lidar.stop_motor()
                self.lidar.disconnect()
            except Exception:
                pass
            self.lidar = None
        self._safe_close_serial()
        self.logger.info("Disconnected.")

    # --- Serial response reading (không parse odometry, chỉ đọc response) ---
    def _start_serial_listener(self):
        if hasattr(self, '_serial_running') and self._serial_running:
            return
        self.logger.debug("Starting serial listener thread.")
        self._serial_running = True
        self._serial_thread = threading.Thread(target=self._serial_reader_loop, daemon=True)
        self._serial_thread.start()

    def _stop_serial_listener(self):
        self._serial_running = False
        if hasattr(self, '_serial_thread') and self._serial_thread:
            self.logger.debug("Stopping serial listener thread.")
            self._serial_thread.join(timeout=2.0)
            self._serial_thread = None

    def _serial_reader_loop(self):
        """Đọc response từ STM32, parse odometry data để lưu lại."""
        while self._serial_running and self.serial_conn and self.serial_conn.is_open:
            try:
                raw = self.serial_conn.readline()
            except serial.SerialException as exc:
                self.logger.error(f"Serial read error: {exc}")
                break
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            # Parse odometry lines (bắt đầu bằng số hoặc dấu trừ, có dấu phẩy)
            # Format từ STM32: x,y,theta,vx,vy,omega,m1,m2,m3
            # Lưu ý: theta từ STM32 là RADIANS, cần convert sang degrees
            if line[0].isdigit() or line[0] == '-':
                parts = line.split(',')
                if len(parts) >= 3:
                    try:
                        x = float(parts[0])
                        y = float(parts[1])
                        theta_rad = float(parts[2])
                        # Convert radians to degrees
                        heading_deg = math.degrees(theta_rad)
                        with self.stm32_odom_lock:
                            self.stm32_odom['x'] = x
                            self.stm32_odom['y'] = y
                            self.stm32_odom['heading_deg'] = heading_deg
                            odom_snapshot = dict(self.stm32_odom)
                    except ValueError:
                        pass
                    else:
                        # Accumulate live progress for the active MOVE/ROTATE using odom deltas.
                        self._accumulate_active_motion_progress(odom_snapshot)
                    continue  # Don't put odom lines in response queue
            self.logger.info(f"[STM32] {line}")
            self.response_queue.put(line.strip())
        self._serial_running = False
    
    def _get_stm32_odom(self):
        """Lấy odometry mới nhất từ STM32 (relative to last RESET_ODOM)."""
        with self.stm32_odom_lock:
            return dict(self.stm32_odom)
    
    def _reset_stm32_odom(self):
        """Reset cached STM32 odometry về 0."""
        with self.stm32_odom_lock:
            self.stm32_odom['x'] = 0.0
            self.stm32_odom['y'] = 0.0
            self.stm32_odom['heading_deg'] = 0.0

    def _odom_delta(self, odom_start, odom_end):
        """Compute STM32 odom delta (MCU frame, clockwise-positive heading) between two snapshots."""
        return {
            'x': odom_end.get('x', 0.0) - odom_start.get('x', 0.0),
            'y': odom_end.get('y', 0.0) - odom_start.get('y', 0.0),
            'heading_deg': normalize_angle_deg(
                odom_end.get('heading_deg', 0.0) - odom_start.get('heading_deg', 0.0)
            ),
        }

    def _compose_pose_with_delta(self, start_pose, odom_delta):
        """
        Compose STM32 odom delta (MCU frame: x fwd, y left, heading clockwise-positive)
        onto an absolute start_pose without mutating internal pose.
        """
        dx_r = odom_delta.get('x', 0.0)
        dy_r = odom_delta.get('y', 0.0)
        dtheta_mcu = odom_delta.get('heading_deg', 0.0)

        heading_start = start_pose['heading_deg']
        heading_start_rad = math.radians(heading_start)

        dx_w = dx_r * math.cos(heading_start_rad) - dy_r * math.sin(heading_start_rad)
        dy_w = dx_r * math.sin(heading_start_rad) + dy_r * math.cos(heading_start_rad)

        dtheta_world = -dtheta_mcu  # MCU clockwise-positive -> world CCW-positive
        new_heading = normalize_angle_deg(heading_start + dtheta_world)

        fused = {
            'x': start_pose['x'] + dx_w,
            'y': start_pose['y'] + dy_w,
            'heading_deg': new_heading,
        }
        return fused

    def _compose_progress_pose(self, start_pose, progress_m, heading_deg):
        """
        Compose forward progress along a fixed heading (ignore lateral odom/yaw drift).
        Use when we assume perfect path but want live progress between start->goal.
        """
        dx = max(0.0, progress_m)  # only forward progress
        rad = math.radians(heading_deg)
        return {
            'x': start_pose['x'] + dx * math.cos(rad),
            'y': start_pose['y'] + dx * math.sin(rad),
            'heading_deg': heading_deg,
        }

    def _compose_rotation_pose(self, start_pose, progress_deg, sign):
        """Compose heading progress (degrees) without translating position."""
        signed = max(0.0, progress_deg) * (1 if sign >= 0 else -1)
        return {
            'x': start_pose['x'],
            'y': start_pose['y'],
            'heading_deg': normalize_angle_deg(start_pose['heading_deg'] + signed),
        }

    def _accumulate_active_motion_progress(self, current_odom):
        """Update active motion progress using odom delta magnitude; ignore sign/axis drift."""
        with self.active_motion_lock:
            if not self.active_motion:
                return 0.0
            delta = self._odom_delta(self.active_motion['odom_start'], current_odom)
            if self.active_motion.get('mode') == 'rotate':
                incr = abs(delta.get('heading_deg', 0.0))
            else:
                incr = abs(delta.get('x', 0.0)) + abs(delta.get('y', 0.0))
            if incr > 0:
                target_cap = self.active_motion.get('target', float('inf'))
                self.active_motion['progress'] = min(
                    target_cap,
                    self.active_motion.get('progress', 0.0) + incr
                )
                # Reset baseline so we only accumulate fresh delta next time
                self.active_motion['odom_start'] = dict(current_odom)
            return self.active_motion.get('progress', 0.0)

    def _get_pose(self):
        """Trả về pose tính từ lệnh đã gửi."""
        with self.pose_lock:
            return {
                'x': self.cmd_pose['x'],
                'y': self.cmd_pose['y'],
                'heading_deg': self.cmd_pose['heading_deg'],
            }

    def _reset_pose(self):
        """Reset pose về (0, 0, 0)."""
        with self.pose_lock:
            self.cmd_pose = {'x': 0.0, 'y': 0.0, 'heading_deg': 0.0}

    def _update_pose_after_move(self, distance):
        """Cập nhật pose sau khi MOVE thành công."""
        with self.pose_lock:
            heading_rad = math.radians(self.cmd_pose['heading_deg'])
            self.cmd_pose['x'] += distance * math.cos(heading_rad)
            self.cmd_pose['y'] += distance * math.sin(heading_rad)
            self.logger.info(
                "Pose updated after MOVE: (%.2f, %.2f, %.1f°)",
                self.cmd_pose['x'], self.cmd_pose['y'], self.cmd_pose['heading_deg']
            )

    def _update_pose_after_rotate(self, angle_deg):
        """Cập nhật pose sau khi ROTATE thành công."""
        with self.pose_lock:
            self.cmd_pose['heading_deg'] = normalize_angle_deg(
                self.cmd_pose['heading_deg'] + angle_deg
            )
            self.logger.info(
                "Pose updated after ROTATE: (%.2f, %.2f, %.1f°)",
                self.cmd_pose['x'], self.cmd_pose['y'], self.cmd_pose['heading_deg']
            )

    # --- LIDAR reading ---
    def _start_lidar_listener(self):
        if self.lidar_running or not self.lidar:
            return
        self.logger.debug("Starting LIDAR listener thread.")
        self.lidar_running = True
        self.lidar_thread = threading.Thread(target=self._lidar_reader_loop, daemon=True)
        self.lidar_thread.start()

    def _stop_lidar_listener(self):
        self.lidar_running = False
        if self.lidar_thread:
            self.logger.debug("Stopping LIDAR listener thread.")
            self.lidar_thread.join(timeout=2.0)
            self.lidar_thread = None

    def _lidar_reader_loop(self):
        while self.lidar_running and self.lidar:
            try:
                # Standard scan mode is more widely compatible across firmware variants.
                for scan in self.lidar.iter_scans():
                    if not self.lidar_running:
                        break
                    with self.scan_lock:
                        self.latest_scan = scan
                    if not self.first_scan_event.is_set():
                        self.first_scan_event.set()
            except RPLidarException as exc:
                self.logger.warning(f"LIDAR read error: {exc}")
                try:
                    # Buffer desync errors (incorrect descriptor / data in buffer) need a flush.
                    self.lidar.clean_input()
                    self.logger.debug("LIDAR input buffer cleaned after error.")
                except Exception as clean_exc:
                    self.logger.debug(f"Failed to clean LIDAR buffer: {clean_exc}")
                time.sleep(0.5)
            except Exception as exc:
                # Log full traceback so we can pinpoint parser/driver issues.
                self.logger.exception("Unexpected LIDAR error")
                time.sleep(0.5)
        self.lidar_running = False

    def _get_scan_snapshot(self):
        with self.scan_lock:
            return list(self.latest_scan)

    # --- Command clamps ---
    def _clamp_move_command(self, distance):
        """Clamp MOVE distance to configured min/max to avoid too-small or oversized commands."""
        sign = 1.0 if distance >= 0 else -1.0
        abs_val = abs(distance)
        clamped = min(max(abs_val, MIN_MOVE_COMMAND_M), MAX_MOVE_COMMAND_M)
        if clamped != abs_val:
            self.logger.info(
                "MOVE command clamped from %.3fm to %.3fm (min=%.3fm, max=%.3fm)",
                abs_val * sign,
                clamped * sign,
                MIN_MOVE_COMMAND_M,
                MAX_MOVE_COMMAND_M,
            )
        return sign * clamped

    def _clamp_rotate_command(self, angle_deg):
        """Clamp ROTATE_DEG to configured min/max magnitude to avoid timeouts on tiny turns."""
        sign = 1.0 if angle_deg >= 0 else -1.0
        abs_val = abs(angle_deg)
        # For small corrections, let the MCU try the exact angle to avoid oscillation.
        if abs_val < MIN_ROTATE_COMMAND_DEG:
            return angle_deg
        clamped = min(max(abs_val, MIN_ROTATE_COMMAND_DEG), MAX_ROTATE_COMMAND_DEG)
        if clamped != abs_val:
            self.logger.info(
                "ROTATE_DEG command clamped from %.1f° to %.1f° (min=%.1f°, max=%.1f°)",
                abs_val * sign,
                clamped * sign,
                MIN_ROTATE_COMMAND_DEG,
                MAX_ROTATE_COMMAND_DEG,
            )
        return sign * clamped

    # --- Command helpers ---
    def _send_raw_command(self, text):
        if not self.serial_conn:
            return
        cmd = text.strip() + "\r\n"
        self.serial_conn.write(cmd.encode('utf-8'))

    def _configure_stm32_params(self):
        """Send all configurable parameters to STM32 on startup."""
        self.logger.info("Configuring STM32 parameters...")
        for param_name, param_value in STM32_PARAMS.items():
            # Skip comment keys (start with '_')
            if param_name.startswith('_'):
                continue
            
            # Format value appropriately (int for timeout values, float for others)
            if 'timeout' in param_name:
                value_str = str(int(param_value))
            else:
                value_str = f"{param_value:.6f}"
            
            cmd = f"SET_PARAM {param_name} {value_str}"
            self._send_raw_command(cmd)
            
            # Wait briefly for ACK
            result, line = self._wait_for_response(
                success_tokens=["OK SET_PARAM"],
                failure_tokens=["ERR"],
                timeout=0.5
            )
            if result:
                self.logger.debug(f"  {param_name} = {param_value}")
            else:
                self.logger.warning(f"  Failed to set {param_name}: {line}")
        
        self.logger.info("STM32 parameters configured.")

    def _clear_response_queue(self):
        while not self.response_queue.empty():
            try:
                self.response_queue.get_nowait()
            except queue.Empty:
                break

    def _wait_for_response(self, success_tokens, failure_tokens, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                line = self.response_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            text = line.strip()
            if any(text.startswith(tok) for tok in success_tokens):
                return True, text
            if any(text.startswith(tok) for tok in failure_tokens):
                return False, text
        return None, None

    def _close_enough_heading(self, desired_heading_world):
        pose = self._get_pose()
        current_heading = pose['heading_deg']
        err = abs(normalize_angle_deg(desired_heading_world - current_heading))
        return err <= ANGLE_TOLERANCE_DEG

    def _send_rotate(self, desired_angle_deg, target_heading_world=None, world_delta=None):
        """Rotate by desired_angle_deg relative to current heading.
        
        STM32 ROTATE_DEG behavior:
        - "OK ROTATE_DEG" = command received, rotation started
        - "TARGET_REACHED" = rotation completed successfully
        - "TIMEOUT" = rotation timed out
        
        Returns:
            True if rotation completed successfully
            False if rotation failed/interrupted (pose updated with actual rotation)
        """
        if abs(desired_angle_deg) < 0.1:
            return True
        
        # Reset STM32 odom cache before ROTATE
        self._reset_stm32_odom()
        
        cmd = f"ROTATE_DEG {desired_angle_deg:.2f}"
        self.logger.info(f"ROTATE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        
        # Step 1: wait for OK ROTATE_DEG (command acknowledged)
        result, line = self._wait_for_response(
            success_tokens=["OK ROTATE_DEG"],
            failure_tokens=["ERR"],
            timeout=2.0  # Short timeout for ACK
        )
        if not result:
            self.logger.warning(f"ROTATE_DEG not acknowledged: {line}")
            return False

        # Track active rotation for live status fusion
        world_delta = world_delta if world_delta is not None else -desired_angle_deg
        with self.active_motion_lock:
            self.active_motion = {
                'mode': 'rotate',
                'start_pose': self._get_pose(),
                'odom_start': self._get_stm32_odom(),
                'target': abs(world_delta),
                'sign': 1.0 if world_delta >= 0 else -1.0,
                'progress': 0.0,
            }
        
        # Step 2: wait for TARGET_REACHED or TIMEOUT
        result, line = self._wait_for_response(
            success_tokens=["TARGET_REACHED"],
            failure_tokens=["TIMEOUT"],
            timeout=ROTATE_TIMEOUT_SEC
        )
        
        if result:
            self.logger.info(f"Rotation completed: {line}")
            # Use actual odom rotation; if not available immediately, wait briefly.
            applied_world = None
            for _ in range(3):
                stm32_odom = self._get_stm32_odom()
                actual_rotation = stm32_odom.get('heading_deg', 0.0)
                if abs(actual_rotation) > 0.1:
                    applied_world = -actual_rotation  # MCU cw+ -> world ccw+
                    break
                time.sleep(0.05)
            if applied_world is not None:
                self._update_pose_after_rotate(applied_world)
            else:
                self.logger.warning("Rotation completed but odom heading ~0; pose not updated.")
            # Reset STM32 odometry so the next MOVE starts from (0,0,0)
            self._send_raw_command("RESET_ODOM")
            time.sleep(0.1)
            with self.active_motion_lock:
                self.active_motion = None
            return True
        
        # Rotation was interrupted/timed out: use STM32 odom to see how far it got
        stm32_odom = self._get_stm32_odom()
        actual_rotation = stm32_odom['heading_deg']
        target_delta_mcu = desired_angle_deg  # MCU frame (same sign as command)
        rotation_err_mcu = abs(actual_rotation - target_delta_mcu)
        if rotation_err_mcu <= ROTATE_TIMEOUT_TOLERANCE_DEG:
            self.logger.warning(
                "Rotation TIMEOUT but odom within tolerance in MCU frame (actual=%.1f°, target=%.1f°, err=%.1f° <= %.1f°); accepting.",
                actual_rotation, target_delta_mcu, rotation_err_mcu, ROTATE_TIMEOUT_TOLERANCE_DEG
            )
            applied_world = -actual_rotation
            self._update_pose_after_rotate(applied_world)
            self._send_raw_command("RESET_ODOM")
            time.sleep(0.1)
            with self.active_motion_lock:
                self.active_motion = None
            return True
        
        if abs(actual_rotation) > 0.5:  # At least some rotation happened
            self.logger.info(f"Rotation interrupted at {actual_rotation:.1f}deg (commanded: {desired_angle_deg:.1f}deg)")
        
        # Reset STM32 odometry for the next command
        self._send_raw_command("RESET_ODOM")
        time.sleep(0.1)
        with self.active_motion_lock:
            self.active_motion = None
        
        if result is False:
            self.logger.warning(f"Rotation failed ({line}).")
        else:
            self.logger.warning("Rotation timeout with no MCU response.")
        return False

    def _send_stop(self):
        """Emergency stop - dừng robot ngay lập tức."""
        self.logger.warning("EMERGENCY STOP!")
        self._send_raw_command("STOP")
        
        # Chờ OK STOP response
        result, line = self._wait_for_response(
            success_tokens=["OK STOP"],
            failure_tokens=[],
            timeout=1.0
        )
        
        # Đợi một chút rồi đọc STM32 odom để biết vị trí thực tế
        time.sleep(0.1)
        stm32_odom = self._get_stm32_odom()
        self.logger.info(f"Stopped at STM32 odom: x={stm32_odom['x']:.3f}, y={stm32_odom['y']:.3f}, heading={stm32_odom['heading_deg']:.1f}°")
        return stm32_odom

    def _send_move(self, target_distance, monitor_lidar=True):
        """Move forward by target_distance meters.
        
        Args:
            target_distance: Distance to move in meters
            monitor_lidar: If True, monitor LIDAR during movement and STOP if obstacle detected
        
        Returns:
            True if movement completed successfully
            False if movement failed/interrupted (pose updated with actual distance traveled)
        """
        if target_distance < DISTANCE_TOLERANCE_M:
            return True

        commanded_distance = self._clamp_move_command(target_distance)
        if commanded_distance != target_distance:
            target_distance = commanded_distance
        
        # Reset STM32 odom cache trước khi MOVE để đo được khoảng cách thực tế
        self._reset_stm32_odom()
        
        # Flag để signal stop từ LIDAR monitor thread
        self._move_stop_flag = False
        self._move_stop_reason = None
        
        cmd = f"MOVE {target_distance:.3f}"
        self.logger.info(f"MOVE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        
        # Track active motion for live status fusion
        with self.active_motion_lock:
            self.active_motion = {
                'mode': 'move',
                'start_pose': self._get_pose(),
                'odom_start': self._get_stm32_odom(),
                'target': target_distance,
                'progress': 0.0,
                'heading': self._get_pose()['heading_deg'],
            }

        # Bước 1: Chờ OK MOVE (command acknowledged)
        result, line = self._wait_for_response(
            success_tokens=["OK MOVE"],
            failure_tokens=["ERR"],
            timeout=2.0
        )
        if not result:
            self.logger.warning(f"MOVE not acknowledged: {line}")
            with self.active_motion_lock:
                self.active_motion = None
            return False
        
        # Bước 2: Monitor LIDAR trong khi chờ MOVE TARGET_REACHED
        # Nếu phát hiện obstacle, gửi STOP ngay lập tức
        lidar_monitor_thread = None
        if monitor_lidar:
            if self.first_scan_event.is_set():
                self.logger.info(f"Starting LIDAR monitor thread for MOVE {target_distance:.3f}m")
                lidar_monitor_thread = threading.Thread(
                    target=self._lidar_move_monitor,
                    args=(target_distance,),
                    daemon=True
                )
                lidar_monitor_thread.start()
            else:
                self.logger.warning("LIDAR monitor requested but no scan data available yet!")
        
        # Bước 3: Chờ TARGET_REACHED hoặc TIMEOUT
        result, line = self._wait_for_response(
            success_tokens=["MOVE TARGET_REACHED"],
            failure_tokens=["MOVE TIMEOUT", "OK STOP"],  # OK STOP = bị dừng bởi LIDAR
            timeout=MOVE_TIMEOUT_SEC
        )
        
        # Stop LIDAR monitor nếu còn chạy
        self._move_stop_flag = True
        
        if result and line and "TARGET_REACHED" in line:
            # MOVE hoàn thành thành công -> trust commanded distance
            self._update_pose_after_move(target_distance)
            # Reset STM32 odometry cho lệnh tiếp theo
            self._send_raw_command("RESET_ODOM")
            time.sleep(0.1)
            with self.active_motion_lock:
                self.active_motion = None
            # Publish latest pose so UI sees translation promptly
            self._send_status()
            return True
        
        # Movement bị gián đoạn -> dùng STM32 odom.x để biết thực tế đã đi bao xa
        # Chỉ đọc odom.x vì MOVE = forward motion = robot's local X axis
        # odom.y ≈ 0 (lateral drift), không cần đọc
        time.sleep(0.1)  # Cho STM32 cập nhật odom
        stm32_odom = self._get_stm32_odom()
        actual_distance = stm32_odom['x']  # Forward distance
        # Prefer accumulated progress magnitude (capped) when available, so STOP retains mid-position
        with self.active_motion_lock:
            if self.active_motion:
                progress_capped = min(
                    self.active_motion.get('progress', 0.0),
                    self.active_motion.get('target', float('inf'))
                )
                actual_distance = max(actual_distance, progress_capped)
        if actual_distance < 0:
            self.logger.warning(f"Negative odom.x ({actual_distance:.3f}m) - robot drifted backward?")
            actual_distance = 0.0  # Don't update pose if robot went backward
        
        if actual_distance > DISTANCE_TOLERANCE_M:
            self.logger.info(f"Movement interrupted at {actual_distance:.3f}m (commanded: {target_distance:.3f}m)")
            self._update_pose_after_move(actual_distance)
        else:
            self.logger.info(f"Movement barely started (odom x={stm32_odom['x']:.3f})")

        # Reset STM32 odometry cho lệnh tiếp theo
        self._send_raw_command("RESET_ODOM")
        time.sleep(0.1)

        with self.active_motion_lock:
            self.active_motion = None

        if self._move_stop_reason:
            self.logger.warning(f"Move stopped by LIDAR: {self._move_stop_reason}")
        elif result is False:
            self.logger.warning(f"Move failed ({line}).")
        else:
            self.logger.warning("Move timeout with no MCU response.")
        # Publish pose even on interruption so UI reflects actual stop point
        self._send_status()
        return False
    
    def _log_lidar_scan_debug(self, robot_heading, clearance_found, distance_needed):
        """Log full LIDAR scan data for debugging when obstacle detected."""
        scan = self._get_scan_snapshot()
        if not scan:
            self.logger.warning("LIDAR DEBUG: No scan data available!")
            return
        
        self.logger.info("=" * 60)
        self.logger.info("LIDAR DEBUG - OBSTACLE DETECTED")
        self.logger.info(f"  Robot heading: {robot_heading:.1f}°")
        self.logger.info(f"  Clearance found: {clearance_found:.2f}m")
        self.logger.info(f"  Distance needed: {distance_needed:.2f}m")
        self.logger.info(f"  Forward scan angle: ±{FORWARD_SCAN_ANGLE_DEG/2:.1f}°")
        self.logger.info(f"  Obstacle stop distance: {OBSTACLE_STOP_DISTANCE_M:.2f}m")
        
        # Log points in forward cone (robot frame)
        forward_points = []
        half_fov = FORWARD_SCAN_ANGLE_DEG / 2.0
        for quality, raw_angle_deg, dist_mm in scan:
            if dist_mm <= 0:
                continue
            dist_m = dist_mm / 1000.0

            # Ignore very close hits so debug focuses on external obstacles
            if dist_m < MIN_VALID_LIDAR_DIST_M:
                continue

            robot_angle = lidar_angle_to_robot(raw_angle_deg)
            angle_diff = robot_angle
            
            if abs(angle_diff) <= half_fov and dist_m < distance_needed + CLEARANCE_MARGIN_M:
                forward_points.append((robot_angle, raw_angle_deg, dist_m, quality))
        
        self.logger.info(f"  Points in forward cone ({len(forward_points)} total):")
        for robot_a, raw_a, dist, quality in sorted(forward_points, key=lambda x: x[0]):
            marker = " <-- BLOCKING" if dist < OBSTACLE_STOP_DISTANCE_M else ""
            self.logger.info(f"    robot={robot_a:6.1f}° raw={raw_a:6.1f}° : {dist:.2f}m (q={quality}){marker}")
        
        if not forward_points:
            self.logger.warning("  NO points in forward cone - check LIDAR alignment!")
        self.logger.info("=" * 60)
    
    def _lidar_move_monitor(self, target_distance):
        """Background thread to monitor LIDAR during MOVE and send STOP if obstacle detected."""
        check_interval = 0.05  # 50ms check interval for faster reaction
        stop_distance = OBSTACLE_STOP_DISTANCE_M
        check_count = 0
        min_clearance_seen = math.inf
        
        self.logger.info(f"[LIDAR Monitor] Started - target_distance={target_distance:.2f}m, stop_distance={stop_distance:.2f}m")
        
        while not self._move_stop_flag:
            if self.first_scan_event.is_set():
                # Check forward direction for obstacles
                current_heading = self._get_pose()['heading_deg']
                clearance = self._heading_clearance(
                    current_heading,
                    current_heading,
                    FORWARD_SCAN_ANGLE_DEG,
                    OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M
                )
                check_count += 1
                if clearance < min_clearance_seen:
                    min_clearance_seen = clearance
                
                # Log every 5th check (every 0.5s) at INFO for visibility
                if check_count % 5 == 0:
                    self.logger.info(
                        "[LIDAR Monitor] Check #%d: clearance=%.2fm, min_seen=%.2fm, heading=%.1f°",
                        check_count, clearance, min_clearance_seen, current_heading
                    )
                
                if clearance < stop_distance:
                    self.logger.warning(f"[LIDAR Monitor] OBSTACLE DETECTED at {clearance:.2f}m! (stop_distance={stop_distance:.2f}m)")
                    self._log_lidar_scan_debug(current_heading, clearance, target_distance)
                    self._move_stop_reason = f"Obstacle at {clearance:.2f}m"
                    self._send_stop()
                    break
            else:
                self.logger.warning("[LIDAR Monitor] No scan data available!")
            
            time.sleep(check_interval)
        
        self.logger.info(
            "[LIDAR Monitor] Stopped after %d checks (min_clearance_seen=%.2fm)",
            check_count, min_clearance_seen
        )

    # --- Obstacle awareness helpers ---
    def _heading_clearance(self, heading_world_deg, pose_heading_deg, fov_deg, max_range_m=None):
        """
        Returns the closest forward distance (meters) from the robot hull to an obstacle
        inside a corridor centered on heading_world_deg.
        
        Corridor definition:
        - Angular FOV: ±fov_deg/2 around heading_world_deg
        - Lateral half-width: CORRIDOR_HALF_WIDTH_M
        - Only points in front of the robot (forward > 0)
        - If max_range_m is provided, points beyond it are ignored (after hull offset)
        """
        scan = self._get_scan_snapshot()
        if not scan:
            self.logger.warning("_heading_clearance: No scan data!")
            return math.inf  # No data = assume clear (better than blocking blindly)

        relative_heading = normalize_angle_deg(heading_world_deg - pose_heading_deg)
        half_fov = fov_deg / 2.0
        min_forward = math.inf

        for _, raw_angle_deg, distance_mm in scan:
            if distance_mm <= 0:
                continue

            distance_m = distance_mm / 1000.0

            # Ignore hits that are extremely close to the robot (self/mount/edge of platform)
            if distance_m < MIN_VALID_LIDAR_DIST_M:
                continue

            # Skip very far points early if max_range_m provided (allowing for robot radius)
            if max_range_m is not None and distance_m > max_range_m + ROBOT_RADIUS_M:
                continue

            robot_angle = lidar_angle_to_robot(raw_angle_deg)
            angle_diff = normalize_angle_deg(robot_angle - relative_heading)
            if abs(angle_diff) > half_fov:
                continue
            ang_rad = math.radians(angle_diff)
            forward = distance_m * math.cos(ang_rad)
            lateral = distance_m * math.sin(ang_rad)

            if forward <= 0:
                continue
            if max_range_m is not None and forward > max_range_m + ROBOT_RADIUS_M:
                continue
            if abs(lateral) > CORRIDOR_HALF_WIDTH_M:
                continue

            # Account for robot hull: subtract projected radius in the travel direction
            if abs(lateral) < ROBOT_RADIUS_M:
                hull_offset = math.sqrt(max(ROBOT_RADIUS_M ** 2 - lateral ** 2, 0.0))
            else:
                hull_offset = 0.0

            clearance_along_path = forward - hull_offset
            if clearance_along_path < min_forward:
                min_forward = clearance_along_path

        if min_forward == math.inf:
            self.logger.debug(
                "_heading_clearance: No obstacles in corridor (FOV=±%.0f°, width=%.2fm)",
                half_fov, CORRIDOR_HALF_WIDTH_M * 2,
            )

        return min_forward

    def _step_static_clear(self, pose, heading_world_deg, step_distance):
        """
        Check whether a forward step of step_distance along heading_world_deg stays within boundary
        and does not cut through any static obstacle.
        """
        sx, sy = pose['x'], pose['y']
        rad = math.radians(heading_world_deg)
        ex = sx + step_distance * math.cos(rad)
        ey = sy + step_distance * math.sin(rad)
        start = (sx, sy)
        end = (ex, ey)

        if self._segment_leaves_boundary(start, end):
            return False
        if self._segment_crosses_obstacles(start, end):
            return False
        return True

    def _choose_heading_with_avoidance(
        self,
        desired_heading_world,
        step_distance,
        allow_detour=True,
        pose=None,
    ):
        """Pick a heading that is safe against both static map and LIDAR while progressing toward the goal."""
        if pose is None:
            pose = self._get_pose()

        pose_heading_deg = pose['heading_deg']
        required_clearance = max(step_distance + CLEARANCE_MARGIN_M, OBSTACLE_STOP_DISTANCE_M)

        if self._step_static_clear(pose, desired_heading_world, step_distance):
            forward_clear = self._heading_clearance(
                desired_heading_world,
                pose_heading_deg,
                FORWARD_SCAN_ANGLE_DEG,
                OBSTACLE_LOOKAHEAD_M,
            )
        else:
            forward_clear = 0.0

        if forward_clear >= required_clearance:
            return desired_heading_world

        if not allow_detour:
            return None

        candidates = []
        if AXIS_ALIGNED_MOVES or DETOUR_AXIS_ALIGNED:
            # Include desired heading first, then absolute axis headings to "reset" during detours
            candidates.append(desired_heading_world)
            candidates.extend([0.0, 90.0, -90.0, 180.0])
        else:
            offsets = [0.0]
            angle = DETOUR_ANGLE_STEP_DEG
            while angle <= DETOUR_MAX_ANGLE_DEG:
                offsets.extend([angle, -angle])
                angle += DETOUR_ANGLE_STEP_DEG
            candidates = [normalize_angle_deg(desired_heading_world + off) for off in offsets]

        best_heading = None
        best_score = -math.inf

        for candidate_world in candidates:
            if not self._step_static_clear(pose, candidate_world, step_distance):
                continue

            clearance = self._heading_clearance(
                candidate_world,
                pose_heading_deg,
                DETOUR_SCAN_ANGLE_DEG,
                OBSTACLE_LOOKAHEAD_M,
            )
            if clearance < required_clearance:
                continue

            if AXIS_ALIGNED_MOVES or DETOUR_AXIS_ALIGNED:
                delta = abs(normalize_angle_deg(candidate_world - desired_heading_world))
                if delta < 1.0:
                    progress = 1.0
                elif delta <= 90.0:
                    progress = 0.5  # perpendicular detour is acceptable
                else:
                    progress = 0.1
            else:
                progress = max(0.0, math.cos(math.radians(normalize_angle_deg(candidate_world - desired_heading_world))))
                if progress <= 0.0:
                    continue

            score = clearance * progress
            if score > best_score:
                best_score = score
                best_heading = candidate_world

        return best_heading

    def _front_clear(self, step_distance, heading_world=None):
        pose = self._get_pose()
        heading = pose['heading_deg'] if heading_world is None else heading_world
        if not self._step_static_clear(pose, heading, step_distance):
            self.logger.info(
                "Static map blocks forward step: pose=(%.2f, %.2f) heading=%.1f° step=%.2f",
                pose['x'],
                pose['y'],
                heading,
                step_distance,
            )
            return False
        clearance = self._heading_clearance(
            heading, pose['heading_deg'], FORWARD_SCAN_ANGLE_DEG, step_distance + CLEARANCE_MARGIN_M
        )
        # Require clearance at least for the planned step, but not stricter than stop distance
        required = min(step_distance, OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M)
        return clearance >= required

    def _side_has_obstacle(self, side):
        """
        side: "RIGHT" or "LEFT"
        Returns True if LIDAR sees obstacle in side cone within [SIDE_WALL_MIN_M, SIDE_WALL_MAX_M].
        """
        scan = self._get_scan_snapshot()
        if not scan:
            return False
        target_rel = -90.0 if side == "RIGHT" else 90.0
        half_fov = SIDE_CONE_DEG / 2.0
        for _, raw_angle_deg, dist_mm in scan:
            if dist_mm <= 0:
                continue
            distance_m = dist_mm / 1000.0
            if distance_m < MIN_VALID_LIDAR_DIST_M:
                continue
            if distance_m < SIDE_WALL_MIN_M or distance_m > SIDE_WALL_MAX_M:
                continue
            robot_angle = lidar_angle_to_robot(raw_angle_deg)
            angle_diff = normalize_angle_deg(robot_angle - target_rel)
            if abs(angle_diff) <= half_fov:
                return True
        return False

    def _path_to_goal_clear(self, pose, segment_goal, base_heading):
        """
        Returns True if path from pose -> segment_goal does NOT intersect known obstacles
        and LIDAR forward clearance in that direction is acceptable.
        """
        start = (pose['x'], pose['y'])
        if self._segment_crosses_obstacles(start, segment_goal):
            self.logger.info(
                "Path-to-goal blocked by obstacle: %s -> %s",
                start, segment_goal
            )
            return False
        if self._segment_leaves_boundary(start, segment_goal):
            self.logger.info(
                "Path-to-goal would leave boundary: %s -> %s",
                start, segment_goal
            )
            return False
        clearance = self._heading_clearance(
            base_heading,
            pose['heading_deg'],
            FORWARD_SCAN_ANGLE_DEG,
            OBSTACLE_LOOKAHEAD_M
        )
        ok = clearance >= (OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M)
        self.logger.info(
            "Path-to-goal clearance heading=%.1f° from %s -> %s: %.2fm (required >= %.2fm) => %s",
            base_heading, start, segment_goal,
            clearance,
            OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M,
            "CLEAR" if ok else "BLOCKED"
        )
        return ok

    def _detour_heading(self, base_heading, side):
        return normalize_angle_deg(base_heading - 90.0 if side == "RIGHT" else base_heading + 90.0)

    def _detour_side_available(self, pose, base_heading, side):
        """
        Rough check if a short step in detour direction would immediately cross a known obstacle.
        """
        heading = self._detour_heading(base_heading, side)
        test_step = 0.15
        dx = test_step * math.cos(math.radians(heading))
        dy = test_step * math.sin(math.radians(heading))
        end_point = (pose['x'] + dx, pose['y'] + dy)
        if self._segment_crosses_obstacles((pose['x'], pose['y']), end_point):
            return False
        if self._segment_leaves_boundary((pose['x'], pose['y']), end_point):
            return False
        return True

    def _rotate_to_heading(self, target_heading_world):
        attempts = 0
        while True:
            pose = self._get_pose()
            current_heading = pose['heading_deg']
            # World delta (CCW positive)
            world_delta = normalize_angle_deg(target_heading_world - current_heading)
            if abs(world_delta) <= ANGLE_TOLERANCE_DEG:
                return True
            # Prefer a deterministic direction for ~180° turns to avoid oscillation
            if abs(world_delta) >= 179.0:
                world_delta = -180.0 if PREFER_CW_FOR_180 else 180.0
            # MCU expects clockwise positive, so invert sign
            command_delta = -world_delta
            command_delta = self._clamp_rotate_command(command_delta)
            world_delta = -command_delta  # keep world delta in sync after clamp

            self.logger.info(
                "Rotate_to_heading: current=%.1f°, target=%.1f°, step_world=%.1f°, command_delta=%.1f°",
                current_heading, target_heading_world, world_delta, command_delta
            )
            ok = self._send_rotate(command_delta, target_heading_world, world_delta=world_delta)
            if ok:
                # On success, apply world delta
                self._update_pose_after_rotate(world_delta)
            else:
                attempts += 1
                pose_after = self._get_pose()
                err_after = abs(normalize_angle_deg(target_heading_world - pose_after['heading_deg']))
                tol = ANGLE_TOLERANCE_DEG if abs(world_delta) < 150.0 else max(ANGLE_TOLERANCE_DEG, 5.0)
                if err_after <= tol:
                    self.logger.info(
                        "Rotation attempt failed but heading within tolerance (err=%.1f° <= %.1f°); accepting.",
                        err_after, tol
                    )
                    return True
                if attempts >= 3:
                    self.logger.warning("Rotation failed after multiple attempts.")
                    return False
            # Loop until heading error is within tolerance

    def _drive_step(self, desired_heading_world, step_distance, allow_detour=True, current_pose=None, check_static=True):
        """
        Drive one step toward desired_heading_world.
        - When allow_detour is False (perimeter verification), we rotate/move in the requested
          direction without exploring detours, but still gate by LIDAR/static safety.
        - check_static is kept for compatibility; static checks are always applied.
        """
        if current_pose:
            pose = {'x': current_pose[0], 'y': current_pose[1], 'heading_deg': self._get_pose()['heading_deg']}
        else:
            pose = self._get_pose()

        if not allow_detour:
            # Rotate then move in-place without exploring alternate headings.
            if not self._step_static_clear(pose, desired_heading_world, step_distance):
                self.logger.warning("Static map blocks requested step; aborting move.")
                return False
            if not self._rotate_to_heading(desired_heading_world):
                return False
            # Simple safety gate: if LIDAR says blocked, abort
            if self.first_scan_event.is_set():
                pose['heading_deg'] = self._get_pose()['heading_deg']
                heading_clear = self._heading_clearance(
                    desired_heading_world,
                    pose['heading_deg'],
                    FORWARD_SCAN_ANGLE_DEG,
                    step_distance + CLEARANCE_MARGIN_M,
                )
                if heading_clear < OBSTACLE_STOP_DISTANCE_M:
                    self.logger.warning("Obstacle detected ahead; stopping move step.")
                    return False
            return self._send_move(step_distance)

        attempts = 0
        while attempts <= MAX_BLOCKED_RETRIES:
            if not self.first_scan_event.is_set():
                self.logger.info("Waiting for first LIDAR scan before moving...")
                self.first_scan_event.wait(timeout=1.0)
                attempts += 1
                continue

            pose['heading_deg'] = self._get_pose()['heading_deg']
            chosen_heading = self._choose_heading_with_avoidance(
                desired_heading_world,
                step_distance,
                allow_detour=True,
                pose=pose,
            )
            if chosen_heading is None:
                attempts += 1
                self.logger.warning("Path blocked; waiting for an opening...")
                time.sleep(BLOCKED_RETRY_WAIT_SEC)
                continue

            if not self._rotate_to_heading(chosen_heading):
                return False
            if not self._send_move(step_distance):
                return False
            return True

        self.logger.warning("Path remained blocked after multiple retries.")
        return False

    # --- Navigation logic ---
    def _navigate_with_planner(self, goal):
        """Navigate to goal using A* over static + live LIDAR obstacles with replans on interruption."""
        replan_count = 0
        while True:
            pose = self._get_pose()
            dx = goal[0] - pose['x']
            dy = goal[1] - pose['y']
            distance = math.hypot(dx, dy)
            if distance < DISTANCE_TOLERANCE_M:
                return True

            # Try visibility-graph planner first for small/tight maps
            path = self._plan_path_visibility((pose['x'], pose['y']), goal)
            if path is None:
                path = self._plan_path_astar((pose['x'], pose['y']), goal)
            if not path:
                self.logger.warning("Planner failed to find path to %s; falling back to reactive LIDAR detours.", goal)
                # Reactive fallback: local navigation using LIDAR avoidance (no static checks)
                return self._navigate_direct(
                    goal,
                    allow_detour=True,
                    check_static=False,
                )
            self.logger.info("Planner produced %d waypoint(s). Executing...", len(path))

            if self._follow_path(path):
                continue  # Check distance again; may need another short plan

            replan_count += 1
            if replan_count >= PLANNER_MAX_REPLANS:
                self.logger.warning("Navigation aborted after %d replans.", replan_count)
                return False
            self.logger.info("Replanning after interruption (attempt %d/%d)...", replan_count, PLANNER_MAX_REPLANS)
            time.sleep(0.1)

    def navigate_to(self, goal, allow_detour=True):
        if not self.first_scan_event.is_set():
            self.logger.info("Waiting for initial LIDAR data...")
            self.first_scan_event.wait(timeout=3.0)

        if allow_detour and not self._goal_valid(goal):
            self.logger.warning("navigate_to(%s) rejected: goal invalid wrt map.", goal)
            raise ValueError("Goal lies inside an obstacle")

        # Prefer planner-based navigation (uses live LIDAR + static map)
        if self.boundary_polygon:
            if self._navigate_with_planner(goal):
                self.logger.info("Goal reached.")
            else:
                self.logger.warning("Navigation ended without reaching goal.")
            return

        # Fallback when no boundary/map available: drive direct with detours
        if not self._navigate_direct(
            goal,
            allow_detour=allow_detour,
            check_static=False,
        ):
            return
        self.logger.info("Goal reached.")


    # --- CLI loop ---
    def run(self):
        self.logger.info("Config loaded from: robot_config.json")
        self.logger.info(
            "Startup config serial_port=%s baud=%s lidar_port=%s kafka_bootstrap=%s robot_id=%s axis_aligned=%s",
            SERIAL_PORT,
            BAUD_RATE,
            LIDAR_PORT,
            KAFKA_BOOTSTRAP_SERVERS,
            ROBOT_ID,
            AXIS_ALIGNED_MOVES,
        )
        if not self._connect():
            return

        # Start Kafka bridge if configured
        if ROBOT_ID and Producer and Consumer:
            topics = {
                "command": KAFKA_TOPIC_COMMAND,
                "telemetry": KAFKA_TOPIC_TELEMETRY,
                "map": KAFKA_TOPIC_MAP,
                "events": KAFKA_TOPIC_EVENTS,
            }
            self.kafka_bridge = KafkaBridge(
                robot_id=ROBOT_ID,
                bootstrap=KAFKA_BOOTSTRAP_SERVERS,
                topics=topics,
                on_command=self._handle_kafka_command,
                on_map_def=self._handle_kafka_map_def,
                logger=lambda msg: self.logger.info("[Kafka] %s", msg)
            )
            self.kafka_bridge.start()
        else:
            if not ROBOT_ID:
                self.logger.warning("ROBOT_ID not set; Kafka bridge disabled.")

        try:
            if self.kafka_bridge:
                # Kafka-driven loop
                self.status_running = True
                self.status_thread = threading.Thread(target=self._status_loop, daemon=True)
                self.status_thread.start()
                while True:
                    try:
                        cmd = self.command_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    self._execute_command(cmd)
            else:
                # CLI fallback
                while True:
                    try:
                        entry = input("Enter goal 'x,y' in meters (or 'q' to quit): ").strip()
                    except EOFError:
                        break
                    if entry.lower() in ('q', 'quit', 'exit'):
                        break
                    try:
                        x_str, y_str = entry.split(',')
                        goal = (float(x_str), float(y_str))
                    except ValueError:
                        self.logger.warning("Invalid format. Use x,y (e.g., 0.5,-0.2).")
                        continue
                    self.navigate_to(goal)
        finally:
            if self.kafka_bridge:
                self.status_running = False
                if self.status_thread:
                    self.status_thread.join(timeout=2.0)
                self.kafka_bridge.stop()
            self._disconnect()

    def _status_loop(self):
        while self.status_running:
            try:
                self._send_status()
            except Exception:
                pass
            time.sleep(1.0)

    # --- Kafka command handlers ---
    def _handle_kafka_command(self, envelope):
        payload = envelope.get("payload") or {}
        self.logger.info(
            "Kafka CMD queued cid=%s command=%s args=%s",
            envelope.get("correlationId"),
            payload.get("command"),
            payload.get("args"),
        )
        self.command_queue.put(envelope)

    def _handle_kafka_map_def(self, envelope):
        self.map_definition = envelope.get("payload")
        self.map_definition_correlation = envelope.get("correlationId")
        incoming_map_id = self.map_definition.get('mapId') if self.map_definition else None
        if self.map_anchor_applied and self.current_map_id == incoming_map_id:
            # Already anchored this mapId; keep existing anchor to avoid "teleporting" pose
            self.logger.info(
                "Map definition received again for mapId=%s; keeping existing anchor.",
                incoming_map_id
            )
        else:
            self.current_map_id = incoming_map_id
            # Force re-anchor on first receipt or map change
            self._load_static_map_data(force_reanchor=True)
            self.logger.info(
                "Map definition received: mapId=%s",
                incoming_map_id if incoming_map_id else 'unknown'
            )
        self.map_loaded_event.set()

    # --- Map helpers (obstacles & POIs) ---
    def _load_static_map_data(self, force_reanchor=False):
        self.obstacles = []
        self.boundary_polygon = None
        self.points_of_interest = []
        # Only reset anchor metadata when explicitly re-anchoring
        if force_reanchor or self.anchor_pose_at_load is None:
            self.map_anchor_applied = False
            self.anchor_rotation_deg = None
            self.anchor_p0_raw = None
            self.anchor_pose_at_load = None
        if not self.map_definition:
            return
        # Decide which pose to use for anchoring
        if force_reanchor or self.anchor_pose_at_load is None:
            pose_for_anchor = self._get_pose()
        else:
            pose_for_anchor = self.anchor_pose_at_load
        boundary_anchor_set = False

        raw_pois = self.map_definition.get("pointsOfInterest") or []
        obstacles = self.map_definition.get("obstacles") or []
        boundary_pts = (self.map_definition.get("boundary") or {}).get("points") or []

        # Anchor boundary to current pose
        if len(boundary_pts) >= 2:
            raw_boundary = [(float(p.get("x", 0)), float(p.get("y", 0))) for p in boundary_pts if p.get("x") is not None and p.get("y") is not None]
            if len(raw_boundary) >= 2:
                p0 = raw_boundary[0]
                p1 = raw_boundary[1]
                vx = p1[0] - p0[0]
                vy = p1[1] - p0[1]
                map_edge_angle_deg = normalize_angle_deg(math.degrees(math.atan2(vy, vx)))
                pose_heading_deg = pose_for_anchor['heading_deg']
                rotation_offset_deg = normalize_angle_deg(pose_heading_deg - map_edge_angle_deg)
                rot = math.radians(rotation_offset_deg)
                cos_r = math.cos(rot)
                sin_r = math.sin(rot)

                def _rotate_translate_point(pt):
                    rx = pt[0] - p0[0]
                    ry = pt[1] - p0[1]
                    x_rot = rx * cos_r - ry * sin_r
                    y_rot = rx * sin_r + ry * cos_r
                    x_world = x_rot + p0[0]
                    y_world = y_rot + p0[1]
                    x_world += pose_for_anchor['x'] - p0[0]
                    y_world += pose_for_anchor['y'] - p0[1]
                    return (x_world, y_world)

                boundary_poly = [_rotate_translate_point(pt) for pt in raw_boundary]
                self.boundary_polygon = boundary_poly
                boundary_anchor_set = True
                self.map_anchor_applied = True
                # Only overwrite anchor metadata when (re)anchoring
                if force_reanchor or self.anchor_pose_at_load is None:
                    self.anchor_rotation_deg = rotation_offset_deg
                    self.anchor_p0_raw = p0
                    self.anchor_pose_at_load = {
                        'x': pose_for_anchor['x'],
                        'y': pose_for_anchor['y'],
                        'heading_deg': pose_for_anchor['heading_deg'],
                    }
                self.logger.info(
                    "Anchoring map: edgeAngle=%.1f°, poseHeading=%.1f°, rotOffset=%.1f°",
                    map_edge_angle_deg, pose_heading_deg, rotation_offset_deg
                )
                self.logger.info(
                    "Boundary anchored: firstVertexWorld=(%.2f, %.2f), vertices=%d",
                    boundary_poly[0][0], boundary_poly[0][1], len(boundary_poly)
                )

        # Anchor obstacles using same offset if boundary exists
        for obs in obstacles:
            pts = obs.get("points") or []
            polygon = [(float(p.get("x", 0)), float(p.get("y", 0))) for p in pts if p.get("x") is not None and p.get("y") is not None]
            if not polygon:
                continue
            if boundary_anchor_set:
                polygon = [_rotate_translate_point(pt) for pt in polygon]
            self.obstacles.append(polygon)

        # Anchor POIs using same offset if boundary exists
        self.points_of_interest = []
        for poi in raw_pois:
            x = float(poi.get("x", 0.0))
            y = float(poi.get("y", 0.0))
            if boundary_anchor_set:
                x, y = _rotate_translate_point((x, y))
            new_poi = dict(poi)
            new_poi["x"] = x
            new_poi["y"] = y
            self.points_of_interest.append(new_poi)
        # boundary logging after anchoring
        if self.boundary_polygon:
            self.logger.info(
                "Boundary loaded: %d vertices, first=%s, last=%s",
                len(self.boundary_polygon),
                self.boundary_polygon[0],
                self.boundary_polygon[-1]
            )
        else:
            self.logger.info("Boundary not present in map definition.")
        self.logger.info(
            "Loaded map data: obstacles=%s pois=%s",
            len(self.obstacles),
            len(self.points_of_interest),
        )

    def _pose_anchor_to_raw(self, pose_anchor):
        """
        Convert a pose from anchored frame back to raw map frame.
        If no anchor metadata is available, return the pose as-is.
        """
        if (
            self.anchor_rotation_deg is None
            or self.anchor_p0_raw is None
            or self.anchor_pose_at_load is None
        ):
            return {
                'x': pose_anchor.get('x', 0.0),
                'y': pose_anchor.get('y', 0.0),
                'heading_deg': pose_anchor.get('heading_deg', 0.0),
            }

        rot_deg = self.anchor_rotation_deg
        rad = math.radians(-rot_deg)  # inverse rotation
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)

        dx = pose_anchor.get('x', 0.0) - self.anchor_pose_at_load['x']
        dy = pose_anchor.get('y', 0.0) - self.anchor_pose_at_load['y']

        x_raw = cos_r * dx - sin_r * dy + self.anchor_p0_raw[0]
        y_raw = sin_r * dx + cos_r * dy + self.anchor_p0_raw[1]
        # Heading in raw frame = heading_anchor - rotOffset
        heading_raw = normalize_angle_deg(pose_anchor.get('heading_deg', 0.0) - rot_deg)

        return {'x': x_raw, 'y': y_raw, 'heading_deg': heading_raw}

    def _point_in_polygon(self, point, polygon, epsilon=1e-3):
        x, y = point
        inside = False
        n = len(polygon)
        if n < 3:
            return False
        px1, py1 = polygon[0]
        for i in range(n + 1):
            px2, py2 = polygon[i % n]
            # Check if point lies on edge (inclusive with epsilon)
            dx = px2 - px1
            dy = py2 - py1
            if abs(dx) < epsilon and abs(dy) < epsilon:
                px1, py1 = px2, py2
                continue
            t = ((x - px1) * dx + (y - py1) * dy) / ((dx * dx) + (dy * dy) + 1e-12)
            t_clamped = max(0.0, min(1.0, t))
            proj_x = px1 + t_clamped * dx
            proj_y = py1 + t_clamped * dy
            if math.hypot(proj_x - x, proj_y - y) <= epsilon:
                return True
            if ((py1 > y) != (py2 > y)) and (x < (px2 - px1) * (y - py1) / ((py2 - py1) + 1e-9) + px1):
                inside = not inside
            px1, py1 = px2, py2
        return inside

    def _segments_intersect(self, p1, p2, q1, q2):
        def ccw(a, b, c):
            return (c[1]-a[1]) * (b[0]-a[0]) > (b[1]-a[1]) * (c[0]-a[0])
        return (ccw(p1, q1, q2) != ccw(p2, q1, q2)) and (ccw(p1, p2, q1) != ccw(p1, p2, q2))

    def _segment_crosses_obstacles(self, start, end):
        for poly_idx, poly in enumerate(self.obstacles):
            m = len(poly)
            if m < 2:
                continue
            for i in range(m):
                a = poly[i]
                b = poly[(i + 1) % m]
                if self._segments_intersect(start, end, a, b):
                    self.logger.info(
                        "Segment %s -> %s intersects obstacle[%d] edge %s -> %s",
                        start, end, poly_idx, a, b
                    )
                    return True
            if self._point_in_polygon(end, poly):
                self.logger.info(
                    "Segment %s -> %s ends INSIDE obstacle[%d].",
                    start, end, poly_idx
                )
                return True
        self.logger.debug("Segment %s -> %s does NOT cross any obstacle.", start, end)
        return False

    def _in_boundary(self, point):
        if not self.boundary_polygon:
            return True
        return self._point_in_polygon(point, self.boundary_polygon)

    def _point_free(self, point):
        """Return True if point is inside boundary and not inside any obstacle."""
        if not self._in_boundary(point):
            return False
        for poly in self.obstacles:
            if self._point_in_polygon(point, poly):
                return False
        return True

    def _direct_path_clear(self, start, goal):
        """Check if straight line from start->goal is free of static obstacles and within boundary."""
        if not self._in_boundary(goal):
            return False
        if self._segment_crosses_obstacles(start, goal):
            return False
        if self._segment_leaves_boundary(start, goal):
            return False
        return True

    def _plan_path_visibility(self, start, goal):
        """
        Simple visibility-graph planner using polygon vertices.
        Returns list of waypoints (including goal) or None if no path.
        """
        if self._direct_path_clear(start, goal):
            return [goal]

        nodes = [start, goal]
        # Add boundary vertices
        if self.boundary_polygon:
            nodes.extend(self.boundary_polygon)
        # Add obstacle vertices
        for obs in self.obstacles:
            nodes.extend(obs)

        # Build visibility edges
        n = len(nodes)
        graph = {i: [] for i in range(n)}

        for i in range(n):
            for j in range(i + 1, n):
                a = nodes[i]
                b = nodes[j]
                if self._segment_crosses_obstacles(a, b):
                    continue
                if self._segment_leaves_boundary(a, b):
                    continue
                dist = math.hypot(b[0] - a[0], b[1] - a[1])
                graph[i].append((j, dist))
                graph[j].append((i, dist))

        # Dijkstra from start (0) to goal (1)
        import heapq
        start_idx = 0
        goal_idx = 1
        heap = [(0.0, start_idx)]
        dist_map = {start_idx: 0.0}
        prev = {}

        while heap:
            d, u = heapq.heappop(heap)
            if u == goal_idx:
                break
            if d != dist_map.get(u, math.inf):
                continue
            for v, w in graph.get(u, []):
                nd = d + w
                if nd < dist_map.get(v, math.inf):
                    dist_map[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))

        if goal_idx not in prev and goal_idx != start_idx:
            return None

        # Reconstruct path (exclude start)
        path_indices = [goal_idx]
        while path_indices[-1] != start_idx:
            path_indices.append(prev[path_indices[-1]])
        path_indices.reverse()
        waypoints = [nodes[i] for i in path_indices[1:]]
        return waypoints

    def _path_clear_with_lidar(self, start, goal):
        """Check whether LIDAR sees a clear corridor from start->goal."""
        if not self.first_scan_event.is_set():
            return True
        distance = math.hypot(goal[0] - start[0], goal[1] - start[1])
        if distance < DISTANCE_TOLERANCE_M:
            return True
        heading_world = math.degrees(math.atan2(goal[1] - start[1], goal[0] - start[0]))
        pose_heading = self._get_pose()['heading_deg']
        clearance = self._heading_clearance(
            heading_world,
            pose_heading,
            FORWARD_SCAN_ANGLE_DEG,
            distance + CLEARANCE_MARGIN_M,
        )
        required = min(distance, OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M)
        return clearance >= required

    def _lidar_blocked_cells(self, min_x, min_y, max_x, max_y):
        """
        Build a set of grid cells blocked by live LIDAR returns (inflated by robot radius).
        Returns grid coords (gx, gy) consistent with planner's to_grid() rounding.
        """
        if not self.first_scan_event.is_set():
            return set()
        scan = self._get_scan_snapshot()
        if not scan:
            return set()

        pose = self._get_pose()
        heading = pose['heading_deg']
        inflation = ROBOT_RADIUS_M + CLEARANCE_MARGIN_M
        inflation_cells = max(1, math.ceil(inflation / ASTAR_GRID_STEP_M))
        blocked = set()

        def to_grid(val, offset):
            # Use floor to keep monotonic grid indexing and avoid skipping indices due to rounding.
            return int(math.floor((val - offset) / ASTAR_GRID_STEP_M + 1e-6))

        for _, raw_angle_deg, dist_mm in scan:
            if dist_mm <= 0:
                continue
            dist_m = dist_mm / 1000.0
            if dist_m < MIN_VALID_LIDAR_DIST_M or dist_m > OBSTACLE_LOOKAHEAD_M:
                continue

            robot_angle = lidar_angle_to_robot(raw_angle_deg)
            world_angle_rad = math.radians(normalize_angle_deg(heading + robot_angle))
            wx = pose['x'] + dist_m * math.cos(world_angle_rad)
            wy = pose['y'] + dist_m * math.sin(world_angle_rad)

            if wx < min_x - inflation or wx > max_x + inflation or wy < min_y - inflation or wy > max_y + inflation:
                continue

            cx = to_grid(wx, min_x)
            cy = to_grid(wy, min_y)

            for dx in range(-inflation_cells, inflation_cells + 1):
                for dy in range(-inflation_cells, inflation_cells + 1):
                    gx = cx + dx
                    gy = cy + dy
                    center_x = min_x + gx * ASTAR_GRID_STEP_M
                    center_y = min_y + gy * ASTAR_GRID_STEP_M
                    if math.hypot(center_x - wx, center_y - wy) <= inflation + (ASTAR_GRID_STEP_M * 0.5):
                        blocked.add((gx, gy))

        if blocked:
            self.logger.info("Planner: LIDAR contributed %d blocked cells.", len(blocked))
        return blocked

    def _plan_path_astar(self, start, goal):
        """
        Very simple grid-based A* planner on anchored frame.
        Returns list of waypoints (including goal) or None if no path.
        """
        if self._direct_path_clear(start, goal):
            if self._path_clear_with_lidar(start, goal):
                return [goal]
            self.logger.info("Direct path blocked by live LIDAR; computing detour via A*.") 

        if not self.boundary_polygon:
            self.logger.info("Planner abort: no boundary polygon.")
            return None

        xs = [p[0] for p in self.boundary_polygon]
        ys = [p[1] for p in self.boundary_polygon]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        margin = ASTAR_GRID_STEP_M

        def to_grid(val, offset):
            # Use floor to keep monotonic grid indexing and avoid skipping indices due to rounding.
            return int(math.floor((val - offset) / ASTAR_GRID_STEP_M + 1e-6))

        start_g = (to_grid(start[0], min_x), to_grid(start[1], min_y))
        goal_g = (to_grid(goal[0], min_x), to_grid(goal[1], min_y))

        lidar_blocked = self._lidar_blocked_cells(min_x, min_y, max_x, max_y)
        lidar_blocked.discard(start_g)
        lidar_blocked.discard(goal_g)

        # Precompute free cells
        free = set()
        x = min_x + ASTAR_GRID_STEP_M * 0.5
        count_nodes = 0
        while x <= max_x + margin:
            y = min_y + ASTAR_GRID_STEP_M * 0.5
            while y <= max_y + margin:
                cell = (to_grid(x, min_x), to_grid(y, min_y))
                if cell in lidar_blocked:
                    y += ASTAR_GRID_STEP_M
                    count_nodes += 1
                    continue
                if self._point_free((x, y)):
                    free.add(cell)
                y += ASTAR_GRID_STEP_M
                count_nodes += 1
                if count_nodes > ASTAR_MAX_NODES:
                    self.logger.warning("Planner abort: exceeded node cap while sampling free space.")
                    break
            if count_nodes > ASTAR_MAX_NODES:
                break
            x += ASTAR_GRID_STEP_M

        # Ensure start/goal are considered free if they lie on boundary edges but not sampled
        if start_g not in free and self._point_free(start):
            free.add(start_g)
        if goal_g not in free and self._point_free(goal):
            free.add(goal_g)

        # If start/goal still not free, snap to nearest free cell (within sampled space)
        def nearest_free(cell):
            if cell in free:
                return cell
            if not free:
                return None
            cx, cy = cell
            best = min(free, key=lambda f: abs(f[0] - cx) + abs(f[1] - cy))
            return best

        start_snap = nearest_free(start_g)
        goal_snap = nearest_free(goal_g)
        if start_snap is None or goal_snap is None:
            self.logger.info(
                "Planner abort: no free cells after sampling (start_free=%s, goal_free=%s)",
                start_g in free,
                goal_g in free,
            )
            return None
        if start_snap != start_g:
            self.logger.info("Planner snapped start cell %s -> %s", start_g, start_snap)
            start_g = start_snap
        if goal_snap != goal_g:
            self.logger.info("Planner snapped goal cell %s -> %s", goal_g, goal_snap)
            goal_g = goal_snap

        import heapq
        open_set = []
        heapq.heappush(open_set, (0, start_g))
        came = {}
        g_score = {start_g: 0}

        def heuristic(a, b):
            return abs(a[0] - b[0]) + abs(a[1] - b[1])

        moves = [(1,0), (-1,0), (0,1), (0,-1)]

        visited = 0
        while open_set and visited < ASTAR_MAX_NODES:
            _, current = heapq.heappop(open_set)
            visited += 1
            if current == goal_g:
                # Reconstruct
                path_cells = [current]
                while current in came:
                    current = came[current]
                    path_cells.append(current)
                path_cells.reverse()
                waypoints = []
                for cx, cy in path_cells[1:]:
                    wx = min_x + cx * ASTAR_GRID_STEP_M
                    wy = min_y + cy * ASTAR_GRID_STEP_M
                    waypoints.append((wx, wy))
                # ensure final goal
                waypoints[-1] = goal
                return waypoints

            for dx, dy in moves:
                nb = (current[0] + dx, current[1] + dy)
                if nb not in free:
                    continue
                tentative = g_score[current] + 1
                if tentative < g_score.get(nb, math.inf):
                    came[nb] = current
                    g_score[nb] = tentative
                    f = tentative + heuristic(nb, goal_g)
                    heapq.heappush(open_set, (f, nb))

        self.logger.info("Planner failed to find path after visiting %d nodes (cap=%d).", visited, ASTAR_MAX_NODES)
        return None

    def _path_to_segments(self, start, waypoints):
        """Convert waypoints into straight segments with heading + total distance."""
        segments = []
        prev = start
        for wp in waypoints:
            dx = wp[0] - prev[0]
            dy = wp[1] - prev[1]
            distance = math.hypot(dx, dy)
            if distance < DISTANCE_TOLERANCE_M:
                prev = wp
                continue
            heading = normalize_angle_deg(math.degrees(math.atan2(dy, dx)))
            if segments and abs(normalize_angle_deg(heading - segments[-1]['heading'])) < 1.0:
                segments[-1]['distance'] += distance
                segments[-1]['target'] = wp
            else:
                segments.append({'heading': heading, 'distance': distance, 'target': wp})
            prev = wp
        return segments

    def _segment_blocked_by_lidar(self, heading_world, distance):
        """
        Quick live check: does the corridor for this segment already look blocked by LIDAR?
        If yes, we bail early to allow a replan that incorporates the current scan.
        """
        if not self.first_scan_event.is_set():
            return False
        pose = self._get_pose()
        end = (
            pose['x'] + distance * math.cos(math.radians(heading_world)),
            pose['y'] + distance * math.sin(math.radians(heading_world)),
        )
        # Use a slightly looser requirement than move-stop threshold so we can creep away from walls.
        clear = self._heading_clearance(
            heading_world,
            pose['heading_deg'],
            FORWARD_SCAN_ANGLE_DEG,
            distance + CLEARANCE_MARGIN_M,
        )
        required = min(distance, OBSTACLE_STOP_DISTANCE_M)
        if clear < required:
            self.logger.info(
                "Live LIDAR blocks segment: start=(%.2f, %.2f) heading=%.1f° dist=%.2f (clear=%.2f<%.2f) -> replan",
                pose['x'], pose['y'], heading_world, distance, clear, required
            )
            return True
        return False

    def _escape_right_detour(self, forward_heading, forward_step):
        """
        Reactive escape when front is blocked: try a right-hand sidestep, then return
        to original heading. Returns True if, after the detour, the original heading
        looks clear enough to continue.
        """
        pose = self._get_pose()
        base_heading = pose['heading_deg']
        side_heading = normalize_angle_deg(base_heading - 90.0)
        sidestep = max(MIN_MOVE_COMMAND_M, min(0.25, MAX_MOVE_COMMAND_M))

        # Check static & LIDAR for the sidestep itself
        if not self._step_static_clear(pose, side_heading, sidestep):
            return False
        side_clear = self._heading_clearance(
            side_heading,
            base_heading,
            FORWARD_SCAN_ANGLE_DEG,
            sidestep + CLEARANCE_MARGIN_M,
        )
        if side_clear < min(sidestep, OBSTACLE_STOP_DISTANCE_M * 0.8):
            return False

        # Execute sidestep
        if not self._rotate_to_heading(side_heading):
            return False
        if not self._send_move(sidestep):
            return False

        # Return to original heading
        if not self._rotate_to_heading(base_heading):
            return False
        time.sleep(0.2)  # let LIDAR settle

        # Check whether forward is now clear
        pose = self._get_pose()
        forward_clear = self._heading_clearance(
            forward_heading,
            pose['heading_deg'],
            FORWARD_SCAN_ANGLE_DEG,
            forward_step + CLEARANCE_MARGIN_M,
        )
        required = min(forward_step, OBSTACLE_STOP_DISTANCE_M)
        return forward_clear >= required

    def _escape_clockwise_loop(self, forward_heading, forward_step):
        """
        Stronger escape: try up to 4 clockwise headings (90° increments), moving a short
        distance if clear, then returning to the original forward heading.
        """
        base_heading = self._get_pose()['heading_deg']
        step = max(MIN_MOVE_COMMAND_M, min(0.25, MAX_MOVE_COMMAND_M))
        for i in range(4):
            side_heading = normalize_angle_deg(base_heading - 90.0 * (i + 1))
            pose = self._get_pose()
            if not self._step_static_clear(pose, side_heading, step):
                continue
            side_clear = self._heading_clearance(
                side_heading,
                pose['heading_deg'],
                FORWARD_SCAN_ANGLE_DEG,
                step + CLEARANCE_MARGIN_M,
            )
            if side_clear < min(step, OBSTACLE_STOP_DISTANCE_M * 0.8):
                continue

            self.logger.info(
                "Escape clockwise attempt %d: heading=%.1f°, step=%.2fm (clear=%.2f)",
                i + 1, side_heading, step, side_clear
            )

            if not self._rotate_to_heading(side_heading):
                continue
            if not self._send_move(step):
                continue
            if not self._rotate_to_heading(base_heading):
                continue
            time.sleep(0.2)

            pose = self._get_pose()
            forward_clear = self._heading_clearance(
                forward_heading,
                pose['heading_deg'],
                FORWARD_SCAN_ANGLE_DEG,
                forward_step + CLEARANCE_MARGIN_M,
            )
            required = min(forward_step, OBSTACLE_STOP_DISTANCE_M)
            self.logger.info(
                "Post-escape forward check: heading=%.1f°, clear=%.2f required=%.2f",
                forward_heading, forward_clear, required
            )
            if forward_clear >= required:
                return True
        return False

    def _execute_segment_move(self, heading_world, distance):
        """Rotate to heading_world then move distance, chunked by min/max move command."""
        remaining = distance
        while remaining > DISTANCE_TOLERANCE_M:
            if not self._close_enough_heading(heading_world):
                if not self._rotate_to_heading(heading_world):
                    return False
            step = min(remaining, MAX_MOVE_COMMAND_M)
            step = self._clamp_move_command(step)
            if self._segment_blocked_by_lidar(heading_world, step):
                # Try a quick right-hand sidestep to clear the blockage
                if self._escape_right_detour(heading_world, step):
                    continue  # After detour, re-evaluate the same segment from new pose
                if self._escape_clockwise_loop(heading_world, step):
                    continue
                return False
            if not self._send_move(step):
                return False
            remaining = max(0.0, remaining - step)
        return True

    def _follow_path(self, waypoints):
        """Execute a path (list of waypoints) using rotate+move primitives."""
        start_pose = self._get_pose()
        segments = self._path_to_segments((start_pose['x'], start_pose['y']), waypoints)
        for seg in segments:
            if not self._rotate_to_heading(seg['heading']):
                return False
            if not self._execute_segment_move(seg['heading'], seg['distance']):
                return False
        return True

    def _goal_valid(self, goal):
        if self.boundary_polygon and not self._point_in_polygon(goal, self.boundary_polygon):
            self.logger.warning(
                "Goal %s is OUTSIDE boundary polygon; rejecting.",
                goal,
            )
            return False
        for poly_idx, poly in enumerate(self.obstacles):
            if self._point_in_polygon(goal, poly):
                self.logger.warning(
                    "Goal %s lies INSIDE obstacle index=%d; rejecting.",
                    goal, poly_idx
                )
                return False
        self.logger.debug("Goal %s is valid w.r.t. boundary + obstacles.", goal)
        return True

    def _segment_leaves_boundary(self, start, end):
        """
        Returns True if moving from start to end would leave the boundary polygon.
        Assumes start should be inside; if boundary is absent, returns False.
        """
        if not self.boundary_polygon:
            return False
        inside_start = self._point_in_polygon(start, self.boundary_polygon)
        inside_end = self._point_in_polygon(end, self.boundary_polygon)
        if inside_start and not inside_end:
            self.logger.info(
                "Segment %s -> %s leaves boundary: start inside, end outside.",
                start, end
            )
            return True
        if not inside_start and not inside_end:
            self.logger.warning(
                "Segment %s -> %s is outside boundary (start and end).",
                start,
                end,
            )
            return True
        self.logger.debug(
            "Segment %s -> %s stays within boundary (start_inside=%s, end_inside=%s).",
            start, end, inside_start, inside_end
        )
        return False

    def _execute_command(self, envelope):
        payload = envelope.get("payload") or {}
        cmd = (payload.get("command") or "").lower()
        correlation_id = envelope.get("correlationId")
        self.logger.info("Execute command=%s cid=%s payload=%s", cmd, correlation_id, payload)
        if cmd == "navigate_to_xy":
            args = payload.get("args") or {}
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                self._send_ack(correlation_id, cmd, False, "Missing x,y")
                return
            try:
                self.navigate_to((float(x), float(y)))
                self._send_ack(correlation_id, cmd, True, None)
                self._send_status()
            except Exception as exc:
                self._send_ack(correlation_id, cmd, False, str(exc))
        elif cmd == "perimeter_validate":
            args = payload.get("args") or {}
            map_def = args.get("mapDefinition")
            if map_def:
                self.map_definition = map_def
                self.map_definition_correlation = correlation_id
                # Allow re-anchor when perimeter validation explicitly sends a map
                self._load_static_map_data(force_reanchor=True)
                self.map_loaded_event.set()
            ok, reason = self._verify_perimeter()
            self._send_ack(correlation_id, cmd, ok, reason)
        elif cmd == "navigate_to_poi":
            args = payload.get("args") or {}
            poi_id = args.get("poiId")
            # Ensure map is loaded before resolving POI (first command can race map definition)
            if not self.map_loaded_event.is_set():
                self.logger.info("Waiting for map definition before executing navigate_to_poi...")
                self.map_loaded_event.wait(timeout=3.0)
            goal = self._find_poi(poi_id)
            if goal is None:
                self._send_ack(correlation_id, cmd, False, f"POI {poi_id} not found or map not loaded")
                return
            try:
                self.navigate_to(goal)
                self._send_ack(correlation_id, cmd, True, None)
                self._send_status()
            except Exception as exc:
                self._send_ack(correlation_id, cmd, False, str(exc))
        elif cmd in ("dock",):
            dock = self._find_poi_by_category("dock")
            if dock is None:
                self._send_ack(correlation_id, cmd, False, "Dock POI not found")
                return
            try:
                self.navigate_to(dock)
                self._send_ack(correlation_id, cmd, True, None)
                self._send_status()
            except Exception as exc:
                self._send_ack(correlation_id, cmd, False, str(exc))
        elif cmd in ("stop", "pause"):
            # Basic stop: send zero move; could be enhanced with motor stop command if available
            self._send_raw_command("STOP")
            self._send_ack(correlation_id, cmd, True, "Stopped")
        elif cmd == "resume":
            self._send_ack(correlation_id, cmd, True, "No-op resume")
        elif cmd == "ping":
            self._send_ack(correlation_id, cmd, True, "pong")
        else:
            self._send_ack(correlation_id, cmd, False, "Unsupported command")

    def _send_ack(self, correlation_id, command, ok, note=None):
        if self.kafka_bridge:
            self.logger.info(
                "ACK command=%s cid=%s status=%s note=%s",
                command,
                correlation_id,
                "OK" if ok else "FAILED",
                note,
            )
            self.kafka_bridge.send_ack(correlation_id, command, ok, note)

    def _send_status(self):
        if not self.kafka_bridge:
            return
        pose_anchor = self._get_pose()

        # If a MOVE is in-flight, fuse live progress for status only (do not mutate pose)
        current_odom = self._get_stm32_odom()
        self._accumulate_active_motion_progress(current_odom)
        with self.active_motion_lock:
            active = dict(self.active_motion) if self.active_motion else None
        if active:
            progress_capped = min(
                active.get('progress', 0.0),
                active.get('target', float('inf'))
            )
            if active.get('mode') == 'rotate':
                fused_pose = self._compose_rotation_pose(
                    active['start_pose'],
                    progress_capped,
                    sign=active.get('sign', 1.0),
                )
            else:
                fused_pose = self._compose_progress_pose(
                    active['start_pose'],
                    progress_capped,
                    heading_deg=active.get('heading', active['start_pose']['heading_deg']),
                )
            pose_anchor = fused_pose

        pose_raw = self._pose_anchor_to_raw(pose_anchor)
        # Publish heading in raw map frame; do not add hardware offset here
        heading_deg = normalize_angle_deg(pose_raw['heading_deg'])
        payload = {
            "pose": {
                "x": pose_raw['x'],
                "y": pose_raw['y'],
                "thetaDeg": heading_deg,
                "headingDeg": heading_deg,
                "heading": heading_deg,
            },
            "state": "IDLE",
        }
        self.logger.info(
            "STATUS anchor=(%.2f,%.2f,%.1fdeg) raw=(%.2f,%.2f,%.1fdeg) state=%s",
            pose_anchor['x'],
            pose_anchor['y'],
            pose_anchor['heading_deg'],
            pose_raw['x'],
            pose_raw['y'],
            heading_deg,
            payload["state"],
        )
        self.kafka_bridge.send_status(payload)

    def _is_polygon_clockwise(self, points):
        """
        Detect if polygon is clockwise using shoelace formula.
        Returns True if clockwise, False if counter-clockwise.
        """
        if len(points) < 3:
            return True  # Default to clockwise for invalid polygons
        signed_area = 0.0
        n = len(points)
        for i in range(n):
            j = (i + 1) % n
            # Shoelace formula: sum of (x_i * y_j - x_j * y_i)
            # Negative area = clockwise, positive = counter-clockwise
            signed_area += points[i][0] * points[j][1] - points[j][0] * points[i][1]
        return signed_area < 0.0

    # --- Perimeter verification (no detours) ---
    def _verify_perimeter(self):
        if not self.map_definition:
            self.logger.info("Waiting for map definition before perimeter verification...")
            self.map_loaded_event.wait(timeout=3.0)
        if not self.map_definition:
            self.logger.warning("No map loaded; cannot verify perimeter.")
            if self.kafka_bridge:
                self.kafka_bridge.send_map_verdict(self.map_definition_correlation, None, "INVALID", reason="NO_MAP")
            return False, "No map"
        map_id = self.map_definition.get("mapId")
        if self.boundary_polygon is None or len(self.boundary_polygon) < 2:
            self.logger.warning("Map boundary invalid; cannot verify perimeter.")
            if self.kafka_bridge:
                self.kafka_bridge.send_map_verdict(self.map_definition_correlation, map_id, "INVALID", reason="INVALID_BOUNDARY")
            return False, "Invalid boundary"

        start_pose = self._get_pose()
        start_heading = start_pose['heading_deg']

        self.logger.info(
            "Starting perimeter verification from pose=(%.2f, %.2f), anchored boundary size=%d",
            self._get_pose()['x'],
            self._get_pose()['y'],
            len(self.boundary_polygon)
        )
        points = list(self.boundary_polygon)
        # Ensure closed loop
        if points[0] != points[-1]:
            points.append(points[0])
        
        # Detect orientation and reverse if counter-clockwise (quẹo trái)
        # Robot cần quẹo phải (clockwise) để phù hợp với STM32
        is_clockwise = self._is_polygon_clockwise(points)
        self.logger.info(
            "Perimeter boundary orientation: %s (vertices=%d)",
            "CLOCKWISE" if is_clockwise else "COUNTERCLOCKWISE",
            len(points)
        )
        if not is_clockwise:
            self.logger.info("Boundary is counter-clockwise; reversing to clockwise for right-turn navigation.")
            points = list(reversed(points))
            # Remove duplicate first point if exists, then re-add at end
            if len(points) > 1 and points[0] == points[-1]:
                points = points[:-1]
            if len(points) > 1 and points[0] != points[-1]:
                points.append(points[0])

        for idx, goal in enumerate(points[1:], start=1):
            self.logger.info("Verifying segment %s/%s -> %s", idx, len(points) - 1, goal)
            success = self._navigate_segment(goal, snap_heading=False)
            if not success:
                self.logger.warning("Perimeter blocked; marking INVALID.")
                if self.kafka_bridge:
                    self.kafka_bridge.send_map_verdict(
                        self.map_definition_correlation, map_id, "INVALID", reason="BLOCKED_PATH",
                        details={"blockedAt": {"x": goal[0], "y": goal[1]}}
                    )
                return False, "Blocked path"

        # Rotate back to initial heading so robot ends where it started
        self.logger.info(
            "Perimeter complete at pose=(%.2f, %.2f, %.1f°); rotating back to start heading %.1f°",
            self._get_pose()['x'],
            self._get_pose()['y'],
            self._get_pose()['heading_deg'],
            start_heading,
        )
        self._rotate_to_heading(start_heading)

        self.logger.info("Perimeter verification complete: VALID.")
        if self.kafka_bridge:
            self.kafka_bridge.send_map_verdict(self.map_definition_correlation, map_id, "VALID", reason="CLEAR", details={})
        return True, None

    def _find_poi(self, poi_id):
        if not self.points_of_interest:
            return None
        pois = self.points_of_interest
        for poi in pois:
            if poi.get("id") == poi_id:
                return float(poi.get("x", 0)), float(poi.get("y", 0))
        return None

    def _find_poi_by_category(self, category):
        if not self.points_of_interest:
            return None
        pois = self.points_of_interest
        for poi in pois:
            if str(poi.get("category", "")).lower() == category.lower():
                return float(poi.get("x", 0)), float(poi.get("y", 0))
        return None

    def _navigate_segment(self, goal, snap_heading=True):
        """
        Navigate to a goal without detours; single rotate + single move.
        LIDAR only used to reject if something is directly ahead; no path reshaping.
        """
        # Compute vector in odom frame
        pose = self._get_pose()
        dx = goal[0] - pose['x']
        dy = goal[1] - pose['y']
        distance = math.hypot(dx, dy)
        if distance < DISTANCE_TOLERANCE_M:
            return True

        # Axis-aligned simplification: snap desired heading to nearest 90°
        heading_world_raw = math.degrees(math.atan2(dy, dx))
        if snap_heading:
            heading_world = round(heading_world_raw / 90.0) * 90.0
            heading_world = normalize_angle_deg(heading_world)
        else:
            heading_world = normalize_angle_deg(heading_world_raw)
        heading_label = "snapped" if snap_heading else "raw"
        self.logger.info(f"Navigate segment: from ({pose['x']:.2f}, {pose['y']:.2f}) to ({goal[0]:.2f}, {goal[1]:.2f})")
        self.logger.info(f"  Distance: {distance:.2f}m, Target heading ({heading_label}): {heading_world:.1f}° "
                         f"(raw {heading_world_raw:.1f}°)")

        # Rotate first to face the goal
        # NOTE: Rotation should NOT be interrupted by LIDAR - robot spins in place
        if not self._rotate_to_heading(heading_world):
            self.logger.warning("Rotation failed!")
            return False
        
        self.logger.info(f"Rotation complete. Current heading: {self._get_pose()['heading_deg']:.1f}°")

        # LIDAR check AFTER rotation - now robot faces the goal direction
        # Check forward (0°) relative to current heading
        if self.first_scan_event.is_set():
            current_heading = self._get_pose()['heading_deg']
            self.logger.info(f"Checking LIDAR in forward direction (heading {current_heading:.1f}°, FOV ±{FORWARD_SCAN_ANGLE_DEG/2:.0f}°)")
            heading_clear = self._heading_clearance(
                current_heading, current_heading, FORWARD_SCAN_ANGLE_DEG, distance + CLEARANCE_MARGIN_M
            )
            # Require clearance at least for the distance we plan to move, but not stricter than stop distance
            required_start_clearance = min(distance, OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M)
            self.logger.info(f"LIDAR clearance: {heading_clear:.2f}m (need {required_start_clearance:.2f}m min)")
            
            if heading_clear < required_start_clearance:
                # Log full LIDAR scan for debugging
                self._log_lidar_scan_debug(current_heading, heading_clear, distance)
                self.logger.warning(
                    f"Obstacle detected at {heading_clear:.2f}m ahead (need {required_start_clearance:.2f}m); stopping segment."
                )
                return False
        else:
            self.logger.warning("No LIDAR data available - proceeding without obstacle check")

        return self._send_move(distance, monitor_lidar=True)

    def _navigate_direct(self, goal, allow_detour=True, check_static=True):
        """
        Navigate toward goal using incremental steps (non-axis-aligned), with optional detour avoidance.
        """
        while True:
            pose = self._get_pose()
            dx = goal[0] - pose['x']
            dy = goal[1] - pose['y']
            distance = math.hypot(dx, dy)
            if distance < DISTANCE_TOLERANCE_M:
                return True
            desired_heading = math.degrees(math.atan2(dy, dx))
            step_distance = min(MOVE_STEP_M, distance)
            if step_distance < DISTANCE_TOLERANCE_M:
                continue
            if not self._drive_step(
                desired_heading,
                step_distance,
                allow_detour=allow_detour,
                current_pose=(pose['x'], pose['y']),
                check_static=check_static,
            ):
                self.logger.warning("Navigation aborted due to repeated blockages or command errors.")
                return False
        return True


if __name__ == '__main__':
    configure_logging()
    navigator = OdomOnlyNavigator()
    navigator.run()
