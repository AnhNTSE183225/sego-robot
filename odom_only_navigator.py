import json
import logging
import logging.handlers
import math
import os
import queue
import serial
import sys
import threading
import time
from typing import Optional

LOG_FILE = os.environ.get("ROBOT_LOG_FILE", "robot.log")
LOG_LEVEL = os.environ.get("ROBOT_LOG_LEVEL", "DEBUG").upper()


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

    # File: DEBUG (luôn luôn để debug chi tiết)
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


# Serial configuration
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

# LIDAR configuration
LIDAR_PORT = '/dev/ttyUSB0'

# Kafka configuration (shared topics)
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC_COMMAND = os.environ.get("KAFKA_TOPIC_COMMAND", "robot.cmd")
KAFKA_TOPIC_TELEMETRY = os.environ.get("KAFKA_TOPIC_TELEMETRY", "robot.telemetry")
KAFKA_TOPIC_MAP = os.environ.get("KAFKA_TOPIC_MAP", "robot.map")
KAFKA_TOPIC_EVENTS = os.environ.get("KAFKA_TOPIC_EVENTS", "robot.events")
ROBOT_ID = os.environ.get("ROBOT_ID")

# Motion tolerances
DISTANCE_TOLERANCE_M = 0.02
ANGLE_TOLERANCE_DEG = 2.0

# Command timeouts
MOVE_TIMEOUT_SEC = 25.0
ROTATE_TIMEOUT_SEC = 15.0

# Movement mode: True -> use axis-aligned L-shape moves (X then Y)
AXIS_ALIGNED_MOVES = True

# Heading offset (deg) to align MCU frame with map frame if needed (e.g., set to 90 to face +Y by default)
HEADING_OFFSET_DEG = float(os.environ.get("HEADING_OFFSET_DEG", "0"))

# Obstacle avoidance parameters
MOVE_STEP_M = 0.25                 # Distance per motion burst; keeps reactiveness high
CLEARANCE_MARGIN_M = 0.10          # Buffer added to the intended step distance
OBSTACLE_STOP_DISTANCE_M = 0.35    # Stop sooner to avoid “bumping” obstacles
OBSTACLE_LOOKAHEAD_M = 1.2         # Max range to consider when scoring headings
SIDE_WALL_MIN_M = 0.05             # Min distance to consider “along the wall”
SIDE_WALL_MAX_M = 0.80             # Max distance to consider “along the wall”
SIDE_CONE_DEG = 60.0               # ±30° cone for side wall detection
FORWARD_SCAN_ANGLE_DEG = 100.0     # Width of the forward corridor to check; widened to catch obstacles off-axis
DETOUR_SCAN_ANGLE_DEG = 80.0       # Wider cone used when looking for alternate headings
DETOUR_ANGLE_STEP_DEG = 15.0       # Angular resolution when sampling detour headings
DETOUR_MAX_ANGLE_DEG = 90.0        # How far left/right we are willing to turn for a detour
BLOCKED_RETRY_WAIT_SEC = 0.4
MAX_BLOCKED_RETRIES = 25
MAX_SIDE_SWITCHES = 5
START_MIN_CLEARANCE_M = 0.50       # Minimum clearance required before starting a MOVE
ROTATE_TIMEOUT_TOLERANCE_DEG = 7.0  # Increased tolerance for timeout acceptance

STATE_GOAL_FOLLOW = "GOAL_FOLLOW"
STATE_OBSTACLE_FOLLOW = "OBSTACLE_FOLLOW"


def normalize_angle_deg(angle):
    """Wrap an angle in degrees to [-180, 180]."""
    while angle > 180.0:
        angle -= 360.0
    while angle < -180.0:
        angle += 360.0
    return angle


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
        self.obstacles = []
        self.boundary_polygon = None
        self.points_of_interest = []
        self.map_anchor_applied = False
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
                    except ValueError:
                        pass
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

    # --- Command helpers ---
    def _send_raw_command(self, text):
        if not self.serial_conn:
            return
        cmd = text.strip() + "\r\n"
        self.serial_conn.write(cmd.encode('utf-8'))

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
        
        # Step 2: wait for TARGET_REACHED or TIMEOUT
        result, line = self._wait_for_response(
            success_tokens=["TARGET_REACHED"],
            failure_tokens=["TIMEOUT"],
            timeout=ROTATE_TIMEOUT_SEC
        )
        
        if result:
            self.logger.info(f"Rotation completed: {line}")
            # Reset STM32 odometry so the next MOVE starts from (0,0,0)
            self._send_raw_command("RESET_ODOM")
            time.sleep(0.1)
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
            self._send_raw_command("RESET_ODOM")
            time.sleep(0.1)
            return True
        
        if abs(actual_rotation) > 0.5:  # At least some rotation happened
            self.logger.info(f"Rotation interrupted at {actual_rotation:.1f}deg (commanded: {desired_angle_deg:.1f}deg)")
        
        # Reset STM32 odometry for the next command
        self._send_raw_command("RESET_ODOM")
        time.sleep(0.1)
        
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
        
        # Reset STM32 odom cache trước khi MOVE để đo được khoảng cách thực tế
        self._reset_stm32_odom()
        
        # Flag để signal stop từ LIDAR monitor thread
        self._move_stop_flag = False
        self._move_stop_reason = None
        
        cmd = f"MOVE {target_distance:.3f}"
        self.logger.info(f"MOVE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        
        # Bước 1: Chờ OK MOVE (command acknowledged)
        result, line = self._wait_for_response(
            success_tokens=["OK MOVE"],
            failure_tokens=["ERR"],
            timeout=2.0
        )
        if not result:
            self.logger.warning(f"MOVE not acknowledged: {line}")
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
            return True
        
        # Movement bị gián đoạn -> dùng STM32 odom.x để biết thực tế đã đi bao xa
        # Chỉ đọc odom.x vì MOVE = forward motion = robot's local X axis
        # odom.y ≈ 0 (lateral drift), không cần đọc
        time.sleep(0.1)  # Cho STM32 cập nhật odom
        stm32_odom = self._get_stm32_odom()
        actual_distance = stm32_odom['x']  # Forward distance
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
        
        if self._move_stop_reason:
            self.logger.warning(f"Move stopped by LIDAR: {self._move_stop_reason}")
        elif result is False:
            self.logger.warning(f"Move failed ({line}).")
        else:
            self.logger.warning("Move timeout with no MCU response.")
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
        
        # Log points in forward cone
        forward_points = []
        half_fov = FORWARD_SCAN_ANGLE_DEG / 2.0
        for quality, angle_deg, dist_mm in scan:
            if dist_mm <= 0:
                continue
            dist_m = dist_mm / 1000.0
            # LIDAR angle is relative to robot's forward direction
            # Normalize to check if in forward cone
            angle_diff = angle_deg  # LIDAR 0° = robot forward
            while angle_diff > 180:
                angle_diff -= 360
            while angle_diff < -180:
                angle_diff += 360
            
            if abs(angle_diff) <= half_fov and dist_m < distance_needed + CLEARANCE_MARGIN_M:
                forward_points.append((angle_deg, dist_m, quality))
        
        self.logger.info(f"  Points in forward cone ({len(forward_points)} total):")
        for angle, dist, quality in sorted(forward_points, key=lambda x: x[0]):
            marker = " <-- BLOCKING" if dist < OBSTACLE_STOP_DISTANCE_M else ""
            self.logger.info(f"    {angle:6.1f}° : {dist:.2f}m (q={quality}){marker}")
        
        if not forward_points:
            self.logger.warning("  NO points in forward cone - check LIDAR alignment!")
        self.logger.info("=" * 60)
    
    def _lidar_move_monitor(self, target_distance):
        """Background thread to monitor LIDAR during MOVE and send STOP if obstacle detected."""
        check_interval = 0.1  # 100ms check interval
        stop_distance = OBSTACLE_STOP_DISTANCE_M
        check_count = 0
        min_clearance_seen = math.inf
        
        self.logger.info(f"[LIDAR Monitor] Started - target_distance={target_distance:.2f}m, stop_distance={stop_distance:.2f}m")
        
        while not self._move_stop_flag:
            if self.first_scan_event.is_set():
                # Check forward direction for obstacles
                current_heading = self._get_pose()['heading_deg']
                clearance = self._heading_clearance(
                    current_heading, current_heading,
                    FORWARD_SCAN_ANGLE_DEG, target_distance
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
        Returns the closest obstacle distance (meters) inside a wedge centered on heading_world_deg.
        heading_world_deg and pose_heading_deg are expressed in the odom/world frame.
        
        LIDAR convention: angle_deg from LIDAR is relative to robot's forward direction
        - LIDAR 0° = robot forward
        - LIDAR 90° = robot left
        - LIDAR 270° = robot right
        """
        scan = self._get_scan_snapshot()
        if not scan:
            self.logger.warning("_heading_clearance: No scan data!")
            return math.inf  # No data = assume clear (better than blocking blindly)

        # When heading_world_deg == pose_heading_deg, relative_heading = 0
        # This means we're checking robot's forward direction
        relative_heading = normalize_angle_deg(heading_world_deg - pose_heading_deg)
        half_fov = fov_deg / 2.0
        min_dist = math.inf
        points_in_cone = 0

        for _, angle_deg, distance_mm in scan:
            if distance_mm <= 0:
                continue
            # angle_deg from LIDAR is already robot-relative (0° = forward)
            # relative_heading is also robot-relative
            angle_diff = normalize_angle_deg(angle_deg - relative_heading)
            if abs(angle_diff) > half_fov:
                continue
            points_in_cone += 1
            distance_m = distance_mm / 1000.0
            if max_range_m is not None and distance_m > max_range_m:
                distance_m = max_range_m
            if distance_m < min_dist:
                min_dist = distance_m

        # If no points in cone, path is clear (return infinity)
        if min_dist == math.inf:
            self.logger.debug(f"_heading_clearance: No obstacles in ±{half_fov:.0f}° cone ({points_in_cone} points)")
        
        return min_dist

    def _choose_heading_with_avoidance(self, desired_heading_world, step_distance, allow_detour=True):
        """Pick a heading that is both safe (clear corridor) and still progresses toward the goal."""
        pose_heading_deg = self._get_pose()['heading_deg']
        required_clearance = max(step_distance + CLEARANCE_MARGIN_M, OBSTACLE_STOP_DISTANCE_M)

        forward_clear = self._heading_clearance(
            desired_heading_world, pose_heading_deg, FORWARD_SCAN_ANGLE_DEG, OBSTACLE_LOOKAHEAD_M
        )
        if forward_clear >= required_clearance:
            return desired_heading_world

        if not allow_detour:
            return None

        offsets = [0.0]
        angle = DETOUR_ANGLE_STEP_DEG
        while angle <= DETOUR_MAX_ANGLE_DEG:
            offsets.extend([angle, -angle])
            angle += DETOUR_ANGLE_STEP_DEG

        best_heading = None
        best_score = -math.inf

        for offset in offsets:
            candidate_world = normalize_angle_deg(desired_heading_world + offset)
            clearance = self._heading_clearance(
                candidate_world, pose_heading_deg, DETOUR_SCAN_ANGLE_DEG, OBSTACLE_LOOKAHEAD_M
            )
            progress = max(0.0, math.cos(math.radians(offset)))  # Penalize turns that face away from goal

            if clearance < required_clearance or progress <= 0.0:
                continue

            score = clearance * progress
            if score > best_score:
                best_score = score
                best_heading = candidate_world

        return best_heading

    def _front_clear(self, step_distance, heading_world=None):
        pose = self._get_pose()
        heading = pose['heading_deg'] if heading_world is None else heading_world
        clearance = self._heading_clearance(
            heading, pose['heading_deg'], FORWARD_SCAN_ANGLE_DEG, step_distance + CLEARANCE_MARGIN_M
        )
        required = max(step_distance, OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M)
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
        for _, angle_deg, dist_mm in scan:
            if dist_mm <= 0:
                continue
            distance_m = dist_mm / 1000.0
            if distance_m < SIDE_WALL_MIN_M or distance_m > SIDE_WALL_MAX_M:
                continue
            angle_diff = normalize_angle_deg(angle_deg - target_rel)
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
        pose = self._get_pose()
        current_heading = pose['heading_deg']
        # World delta (CCW positive)
        world_delta = normalize_angle_deg(target_heading_world - current_heading)
        # MCU expects clockwise positive, so invert sign
        command_delta = -world_delta

        self.logger.info(
            "Rotate_to_heading: current=%.1f°, target=%.1f°, world_delta=%.1f°, command_delta=%.1f°",
            current_heading, target_heading_world, world_delta, command_delta
        )
        ok = self._send_rotate(command_delta, target_heading_world, world_delta=world_delta)
        if ok:
            # On success, apply world delta
            self._update_pose_after_rotate(world_delta)
            return True
        # If rotation reported failure, check if we're already close enough
        pose_after = self._get_pose()
        err = abs(normalize_angle_deg(target_heading_world - pose_after['heading_deg']))
        if err <= ANGLE_TOLERANCE_DEG:
            self.logger.info(
                "Rotation reported failure but heading within tolerance (err=%.1f° <= %.1f°); accepting.",
                err, ANGLE_TOLERANCE_DEG
            )
            return True
        return False

    def _drive_step(self, desired_heading_world, step_distance, allow_detour=True, current_pose=None):
        """
        Drive one step toward desired_heading_world.
        - When allow_detour is False (perimeter verification), we bypass LIDAR gating to guarantee
          the robot keeps tracing the boundary; obstacle avoidance is not applied.
        """
        start_point = current_pose if current_pose else (self._get_pose()['x'], self._get_pose()['y'])

        if not allow_detour:
            # Pure odom move: rotate then move, ignore LIDAR/obstacle sampling.
            if not self._rotate_to_heading(desired_heading_world):
                return False
            # Simple safety gate: if LIDAR says blocked, abort
            if self.first_scan_event.is_set():
                heading_clear = self._heading_clearance(
                    desired_heading_world, self._get_pose()['heading_deg'],
                    FORWARD_SCAN_ANGLE_DEG, OBSTACLE_LOOKAHEAD_M
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

            chosen_heading = self._choose_heading_with_avoidance(desired_heading_world, step_distance, allow_detour)
            if chosen_heading is None:
                attempts += 1
                self.logger.warning("Path blocked; waiting for an opening...")
                time.sleep(BLOCKED_RETRY_WAIT_SEC)
                continue

            dx = step_distance * math.cos(math.radians(chosen_heading))
            dy = step_distance * math.sin(math.radians(chosen_heading))
            end_point = (start_point[0] + dx, start_point[1] + dy)
            if self._segment_crosses_obstacles(start_point, end_point):
                attempts += 1
                self.logger.warning("Static obstacle blocks the segment; trying another heading...")
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
    def navigate_to(self, goal, allow_detour=True):
        if not self.first_scan_event.is_set():
            self.logger.info("Waiting for initial LIDAR data...")
            self.first_scan_event.wait(timeout=3.0)

        if allow_detour and not self._goal_valid(goal):
            self.logger.warning("navigate_to(%s) rejected: goal invalid wrt map.", goal)
            raise ValueError("Goal lies inside an obstacle")

        # Non-axis-aligned fallback: preserve previous behavior
        if not AXIS_ALIGNED_MOVES:
            while True:
                pose = self._get_pose()
                dx = goal[0] - pose['x']
                dy = goal[1] - pose['y']
                distance = math.hypot(dx, dy)
                if distance < DISTANCE_TOLERANCE_M:
                    self.logger.info("Goal reached.")
                    return
                desired_heading = math.degrees(math.atan2(dy, dx))
                step_distance = min(MOVE_STEP_M, distance)
                if step_distance < DISTANCE_TOLERANCE_M:
                    continue
                if not self._drive_step(desired_heading, step_distance, allow_detour=allow_detour, current_pose=(pose['x'], pose['y'])):
                    self.logger.warning("Navigation aborted due to repeated blockages or command errors.")
                    return
            return

        # Axis-aligned L-shape: two segments (X then Y)
        pose = self._get_pose()
        segments = [
            (goal[0], pose['y']),  # X segment
            (goal[0], goal[1]),    # Y segment
        ]

        for segment_goal in segments:
            state = STATE_GOAL_FOLLOW
            detour_side = "RIGHT"
            side_switches = 0

            # If detour is disabled, perform simple rotate+move with LIDAR gate
            if not allow_detour:
                while True:
                    pose = self._get_pose()
                    dx = segment_goal[0] - pose['x']
                    dy = segment_goal[1] - pose['y']
                    distance = math.hypot(dx, dy)
                    if distance < DISTANCE_TOLERANCE_M:
                        break
                    if abs(dx) >= DISTANCE_TOLERANCE_M:
                        base_heading = 0.0 if dx >= 0 else 180.0
                    else:
                        base_heading = 90.0 if dy >= 0 else -90.0
                    if not self._close_enough_heading(base_heading):
                        self._rotate_to_heading(base_heading)
                    if self.first_scan_event.is_set() and not self._front_clear(distance, heading_world=base_heading):
                        self.logger.warning("Path blocked and detour disabled; aborting segment.")
                        return
                    if not self._send_move(distance):
                        self.logger.warning("Move failed and detour disabled; aborting segment.")
                        return
                continue

            while True:
                pose = self._get_pose()
                dx = segment_goal[0] - pose['x']
                dy = segment_goal[1] - pose['y']
                distance = math.hypot(dx, dy)
                if distance < DISTANCE_TOLERANCE_M:
                    break  # Segment complete

                # Determine axis and base heading for this segment
                if abs(dx) >= DISTANCE_TOLERANCE_M:
                    base_heading = 0.0 if dx >= 0 else 180.0
                else:
                    base_heading = 90.0 if dy >= 0 else -90.0

                if state == STATE_GOAL_FOLLOW:
                    if not self._close_enough_heading(base_heading):
                        self._rotate_to_heading(base_heading)

                    step = min(MOVE_STEP_M, distance)
                    if step < DISTANCE_TOLERANCE_M:
                        continue

                    if self._front_clear(step):
                        if not self._send_move(step):
                            # Treat failed move as obstacle encounter
                            state = STATE_OBSTACLE_FOLLOW
                            detour_side = "RIGHT"
                            side_switches = 0
                        continue

                    # Blocked ahead: choose detour side
                    preferred = "RIGHT"
                    if not self._detour_side_available(pose, base_heading, preferred):
                        preferred = "LEFT"
                    detour_side = preferred
                    detour_heading = self._detour_heading(base_heading, detour_side)
                    self._rotate_to_heading(detour_heading)
                    state = STATE_OBSTACLE_FOLLOW
                    side_switches = 0
                    continue

                if state == STATE_OBSTACLE_FOLLOW:
                    # Can we return to base path?
                    if self._path_to_goal_clear(pose, segment_goal, base_heading):
                        self._rotate_to_heading(base_heading)
                        state = STATE_GOAL_FOLLOW
                        detour_side = "RIGHT"
                        side_switches = 0
                        continue

                    detour_heading = self._detour_heading(base_heading, detour_side)
                    # Side contact check (for debugging wall-follow quality)
                    if not self._side_has_obstacle(detour_side):
                        self.logger.debug(f"Side {detour_side} appears clear while following obstacle.")
                    if not self._close_enough_heading(detour_heading):
                        self._rotate_to_heading(detour_heading)

                    step = min(MOVE_STEP_M, distance)
                    if step < DISTANCE_TOLERANCE_M:
                        continue

                    if self._front_clear(step, heading_world=detour_heading):
                        if not self._send_move(step):
                            # treat as blockage even though front_clear said clear
                            pass
                        continue

                    # Need to switch side
                    detour_side = "LEFT" if detour_side == "RIGHT" else "RIGHT"
                    side_switches += 1
                    detour_heading = self._detour_heading(base_heading, detour_side)
                    self._rotate_to_heading(detour_heading)
                    if side_switches > MAX_SIDE_SWITCHES:
                        self.logger.warning("Too many detour side switches; aborting segment.")
                        return

        self.logger.info("Goal reached.")


    # --- CLI loop ---
    def run(self):
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
            time.sleep(5.0)

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
        self._load_static_map_data()
        self.map_loaded_event.set()
        self.logger.info(
            "Map definition received: mapId=%s",
            self.map_definition.get('mapId') if self.map_definition else 'unknown'
        )

    # --- Map helpers (obstacles & POIs) ---
    def _load_static_map_data(self):
        self.obstacles = []
        self.boundary_polygon = None
        self.points_of_interest = []
        self.map_anchor_applied = False
        if not self.map_definition:
            return
        pose = self._get_pose()
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
                pose_heading_deg = pose['heading_deg']
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
                    x_world += pose['x'] - p0[0]
                    y_world += pose['y'] - p0[1]
                    return (x_world, y_world)

                boundary_poly = [_rotate_translate_point(pt) for pt in raw_boundary]
                self.boundary_polygon = boundary_poly
                boundary_anchor_set = True
                self.map_anchor_applied = True
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

    def _point_in_polygon(self, point, polygon):
        x, y = point
        inside = False
        n = len(polygon)
        if n < 3:
            return False
        px1, py1 = polygon[0]
        for i in range(n + 1):
            px2, py2 = polygon[i % n]
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
                self._load_static_map_data()
                self.map_loaded_event.set()
            ok, reason = self._verify_perimeter()
            self._send_ack(correlation_id, cmd, ok, reason)
        elif cmd == "navigate_to_poi":
            args = payload.get("args") or {}
            poi_id = args.get("poiId")
            goal = self._find_poi(poi_id)
            if goal is None:
                self._send_ack(correlation_id, cmd, False, f"POI {poi_id} not found")
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
        pose = self._get_pose()
        heading_deg = normalize_angle_deg(pose['heading_deg'] + HEADING_OFFSET_DEG)
        payload = {
            "pose": {
                "x": pose['x'],
                "y": pose['y'],
                "thetaDeg": heading_deg,
                "headingDeg": heading_deg,
                "heading": heading_deg,
            },
            "state": "IDLE",
        }
        self.logger.info(
            "STATUS pose=(%.2f,%.2f,%.1fdeg) state=%s",
            pose['x'],
            pose['y'],
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
            required_start_clearance = max(OBSTACLE_STOP_DISTANCE_M + CLEARANCE_MARGIN_M, START_MIN_CLEARANCE_M)
            self.logger.info(f"LIDAR clearance: {heading_clear:.2f}m (need {required_start_clearance:.2f}m min)")
            
            if heading_clear < required_start_clearance:
                # Log full LIDAR scan for debugging
                self._log_lidar_scan_debug(current_heading, heading_clear, distance)
                self.logger.warning(f"Obstacle detected at {heading_clear:.2f}m ahead (need {distance:.2f}m); stopping segment.")
                return False
        else:
            self.logger.warning("No LIDAR data available - proceeding without obstacle check")

        return self._send_move(distance, monitor_lidar=True)


if __name__ == '__main__':
    configure_logging()
    navigator = OdomOnlyNavigator()
    navigator.run()
