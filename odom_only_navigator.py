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
    """Configure console + rotating file logging for deep debugging."""
    level = getattr(logging, LOG_LEVEL, logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)


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
CLEARANCE_MARGIN_M = 0.05          # Buffer added to the intended step distance
OBSTACLE_STOP_DISTANCE_M = 0.45    # Anything closer than this in the corridor blocks motion
OBSTACLE_LOOKAHEAD_M = 1.2         # Max range to consider when scoring headings
FORWARD_SCAN_ANGLE_DEG = 50.0      # Width of the forward corridor to check
DETOUR_SCAN_ANGLE_DEG = 80.0       # Wider cone used when looking for alternate headings
DETOUR_ANGLE_STEP_DEG = 15.0       # Angular resolution when sampling detour headings
DETOUR_MAX_ANGLE_DEG = 90.0        # How far left/right we are willing to turn for a detour
BLOCKED_RETRY_WAIT_SEC = 0.4
MAX_BLOCKED_RETRIES = 25


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
        self.odom_thread = None
        self.odom_running = False
        self.pose_lock = threading.Lock()
        self.latest_pose = {
            'x': 0.0,
            'y': 0.0,
            'theta': 0.0,
            'timestamp': time.time()
        }
        self.response_queue = queue.Queue()

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
        self.points_of_interest = []
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

        self._start_odometry_listener()
        self._send_raw_command("RESET_ODOM")

        # Give the scanner a moment to deliver its first frames
        if not self.first_scan_event.wait(timeout=3.0):
            self.logger.warning("No LIDAR data received yet; motion will pause until scans arrive.")

        self.logger.info("Connected. Odometry reset to (0,0,0).")
        return True

    def _safe_close_serial(self):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass

    def _disconnect(self):
        self.logger.info("Disconnecting...")
        self._stop_odometry_listener()
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

    # --- Odometry reading ---
    def _start_odometry_listener(self):
        if self.odom_running:
            return
        self.logger.debug("Starting odometry listener thread.")
        self.odom_running = True
        self.odom_thread = threading.Thread(target=self._odometry_reader_loop, daemon=True)
        self.odom_thread.start()

    def _stop_odometry_listener(self):
        self.odom_running = False
        if self.odom_thread:
            self.logger.debug("Stopping odometry listener thread.")
            self.odom_thread.join(timeout=2.0)
            self.odom_thread = None

    def _odometry_reader_loop(self):
        while self.odom_running and self.serial_conn and self.serial_conn.is_open:
            try:
                raw = self.serial_conn.readline()
            except serial.SerialException as exc:
                self.logger.error(f"Serial read error: {exc}")
                break
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            pose = self._parse_odometry_line(line)
            if pose:
                with self.pose_lock:
                    self.latest_pose.update(pose)
            else:
                if line:
                    self.logger.info(f"[STM32] {line}")
                    self.response_queue.put(line.strip())
        self.odom_running = False

    def _parse_odometry_line(self, line):
        parts = line.split(',')
        if len(parts) < 3:
            return None
        try:
            x = float(parts[0])
            y = float(parts[1])
            theta = float(parts[2])
        except ValueError:
            return None
        return {'x': x, 'y': y, 'theta': theta, 'timestamp': time.time()}

    def _get_pose(self):
        with self.pose_lock:
            return dict(self.latest_pose)

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
        current_heading = math.degrees(pose['theta'])
        err = abs(normalize_angle_deg(desired_heading_world - current_heading))
        return err <= ANGLE_TOLERANCE_DEG

    def _send_rotate(self, desired_angle_deg, target_heading_world=None):
        """Rotate by desired_angle_deg relative to current heading."""
        if abs(desired_angle_deg) < ANGLE_TOLERANCE_DEG:
            return True
        cmd = f"ROTATE_DEG {desired_angle_deg:.2f}"
        self.logger.info(f"ROTATE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        result, line = self._wait_for_response(
            # Firmware returns "OK ROTATE_DEG" then CL ...; no TARGET_REACHED, so accept OK as success.
            success_tokens=["OK ROTATE_DEG", "TARGET_REACHED"],
            failure_tokens=["ROTATE TIMEOUT", "TIMEOUT"],
            timeout=ROTATE_TIMEOUT_SEC
        )
        if result:
            return True
        if result is False and line and line.startswith("TIMEOUT"):
            # Assume success on TIMEOUT to avoid blocking; MCU already running control loop.
            self.logger.warning("Rotation TIMEOUT; assuming success to continue.")
            return True
        if result is False:
            self.logger.warning(f"Rotation failed ({line}).")
            return False
        self.logger.warning("Rotation timeout with no MCU response.")
        return False

    def _send_move(self, target_distance):
        if target_distance < DISTANCE_TOLERANCE_M:
            return True
        cmd = f"MOVE {target_distance:.3f}"
        self.logger.info(f"MOVE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        result, line = self._wait_for_response(
            success_tokens=["MOVE TARGET_REACHED"],
            failure_tokens=["MOVE TIMEOUT"],
            timeout=MOVE_TIMEOUT_SEC
        )
        if result:
            return True
        if result is False:
            self.logger.warning(f"Move failed ({line}).")
            return False
        self.logger.warning("Move timeout with no MCU response.")
        return False

    # --- Obstacle awareness helpers ---
    def _heading_clearance(self, heading_world_deg, pose_heading_deg, fov_deg, max_range_m=None):
        """
        Returns the closest obstacle distance (meters) inside a wedge centered on heading_world_deg.
        heading_world_deg and pose_heading_deg are expressed in the odom/world frame.
        """
        scan = self._get_scan_snapshot()
        if not scan:
            return 0.0  # Fail-safe: treat unknown as blocked to avoid blind driving

        relative_heading = normalize_angle_deg(heading_world_deg - pose_heading_deg)
        half_fov = fov_deg / 2.0
        min_dist = math.inf

        for _, angle_deg, distance_mm in scan:
            if distance_mm <= 0:
                continue
            angle_diff = normalize_angle_deg(angle_deg - relative_heading)
            if abs(angle_diff) > half_fov:
                continue
            distance_m = distance_mm / 1000.0
            if max_range_m is not None and distance_m > max_range_m:
                distance_m = max_range_m
            if distance_m < min_dist:
                min_dist = distance_m

        return min_dist

    def _choose_heading_with_avoidance(self, desired_heading_world, step_distance, allow_detour=True):
        """Pick a heading that is both safe (clear corridor) and still progresses toward the goal."""
        pose_heading_deg = math.degrees(self._get_pose()['theta'])
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

    def _rotate_to_heading(self, target_heading_world):
        pose = self._get_pose()
        current_heading = math.degrees(pose['theta'])
        rotate_angle = normalize_angle_deg(target_heading_world - current_heading)
        # Force quẹo phải: nếu góc quay âm (quẹo trái), cộng 360 để quẹo phải
        # STM32 chỉ hỗ trợ quẹo phải (positive degrees)
        if rotate_angle < 0:
            rotate_angle += 360.0
        return self._send_rotate(rotate_angle, target_heading_world)

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
                    desired_heading_world, math.degrees(self._get_pose()['theta']),
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
            raise ValueError("Goal lies inside an obstacle")

        while True:
            pose = self._get_pose()
            dx = goal[0] - pose['x']
            dy = goal[1] - pose['y']
            distance = math.hypot(dx, dy)
            if distance < DISTANCE_TOLERANCE_M:
                self.logger.info("Goal reached.")
                final_pose = self._get_pose()
                self.logger.info(
                    "Final pose x=%.2f y=%.2f heading=%.1f deg",
                    final_pose['x'], final_pose['y'], math.degrees(final_pose['theta'])
                )
                return

            if AXIS_ALIGNED_MOVES and abs(dx) >= DISTANCE_TOLERANCE_M:
                desired_heading = 0.0 if dx >= 0 else 180.0
                step_distance = min(MOVE_STEP_M, abs(dx))
            elif AXIS_ALIGNED_MOVES:
                desired_heading = 90.0 if dy >= 0 else -90.0
                step_distance = min(MOVE_STEP_M, abs(dy))
            else:
                desired_heading = math.degrees(math.atan2(dy, dx))
                step_distance = min(MOVE_STEP_M, distance)

            if step_distance < DISTANCE_TOLERANCE_M:
                continue

            if not self._drive_step(desired_heading, step_distance, allow_detour=allow_detour, current_pose=(pose['x'], pose['y'])):
                self.logger.warning("Navigation aborted due to repeated blockages or command errors.")
                return

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
        self.points_of_interest = []
        if not self.map_definition:
            return
        self.points_of_interest = self.map_definition.get("pointsOfInterest") or []
        obstacles = self.map_definition.get("obstacles") or []
        for obs in obstacles:
            pts = obs.get("points") or []
            polygon = [(float(p.get("x", 0)), float(p.get("y", 0))) for p in pts if p.get("x") is not None and p.get("y") is not None]
            if polygon:
                self.obstacles.append(polygon)
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
        for poly in self.obstacles:
            m = len(poly)
            if m < 2:
                continue
            for i in range(m):
                a = poly[i]
                b = poly[(i + 1) % m]
                if self._segments_intersect(start, end, a, b):
                    return True
            if self._point_in_polygon(end, poly):
                return True
        return False

    def _goal_valid(self, goal):
        for poly in self.obstacles:
            if self._point_in_polygon(goal, poly):
                return False
        return True

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
        heading_deg = normalize_angle_deg(math.degrees(pose['theta']) + HEADING_OFFSET_DEG)
        payload = {
            # keep both thetaDeg and headingDeg/heading for FE compatibility
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
            signed_area += (points[j][0] - points[i][0]) * (points[j][1] + points[i][1])
        # Negative area = clockwise, positive = counter-clockwise
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
        boundary = (self.map_definition.get("boundary") or {}).get("points") or []
        map_id = self.map_definition.get("mapId")
        if len(boundary) < 2:
            self.logger.warning("Map boundary invalid; cannot verify perimeter.")
            if self.kafka_bridge:
                self.kafka_bridge.send_map_verdict(self.map_definition_correlation, map_id, "INVALID", reason="INVALID_BOUNDARY")
            return False, "Invalid boundary"

        self.logger.info("Starting perimeter verification...")
        points = [(float(p["x"]), float(p["y"])) for p in boundary]
        # Ensure closed loop
        if points[0] != points[-1]:
            points.append(points[0])
        
        # Detect orientation and reverse if counter-clockwise (quẹo trái)
        # Robot cần quẹo phải (clockwise) để phù hợp với STM32
        is_clockwise = self._is_polygon_clockwise(points)
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
            success = self._navigate_segment(goal)
            if not success:
                self.logger.warning("Perimeter blocked; marking INVALID.")
                if self.kafka_bridge:
                    self.kafka_bridge.send_map_verdict(
                        self.map_definition_correlation, map_id, "INVALID", reason="BLOCKED_PATH",
                        details={"blockedAt": {"x": goal[0], "y": goal[1]}}
                    )
                return False, "Blocked path"

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

    def _navigate_segment(self, goal):
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

        heading_world = math.degrees(math.atan2(dy, dx))

        # Optional simple safety: if forward corridor blocked, abort (no detours)
        if self.first_scan_event.is_set():
            heading_clear = self._heading_clearance(
                heading_world, math.degrees(pose['theta']), FORWARD_SCAN_ANGLE_DEG, OBSTACLE_LOOKAHEAD_M
            )
            if heading_clear < OBSTACLE_STOP_DISTANCE_M:
                self.logger.warning("Obstacle detected ahead; stopping segment.")
                return False

        if not self._rotate_to_heading(heading_world):
            return False
        return self._send_move(distance)


if __name__ == '__main__':
    configure_logging()
    navigator = OdomOnlyNavigator()
    navigator.run()
