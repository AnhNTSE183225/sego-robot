import math
import queue
import serial
import threading
import time

try:
    from rplidar import RPLidar, RPLidarException
except ImportError:
    print("Error: RPLidar library not found. Install with 'pip install rplidar-roboticia'.")
    RPLidar = None

    class RPLidarException(Exception):
        pass


# Serial configuration
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

# LIDAR configuration
LIDAR_PORT = '/dev/ttyUSB0'

# Motion tolerances
DISTANCE_TOLERANCE_M = 0.02
ANGLE_TOLERANCE_DEG = 2.0

# Command timeouts
MOVE_TIMEOUT_SEC = 25.0
ROTATE_TIMEOUT_SEC = 15.0

# Movement mode: True -> use axis-aligned L-shape moves (X then Y)
AXIS_ALIGNED_MOVES = True

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

    # --- Connection management ---
    def _connect(self):
        if RPLidar is None:
            print("RPLidar dependency missing; cannot perform obstacle avoidance.")
            return False

        print("Connecting to STM32 controller and RPLidar...")
        try:
            self.serial_conn = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            time.sleep(1.0)
        except serial.SerialException as exc:
            print(f"Error opening {SERIAL_PORT}: {exc}")
            return False

        try:
            self.lidar = RPLidar(LIDAR_PORT)
            self.lidar.start_motor()
            self._start_lidar_listener()
        except RPLidarException as exc:
            print(f"Error connecting to LIDAR on {LIDAR_PORT}: {exc}")
            self._safe_close_serial()
            return False

        self._start_odometry_listener()
        self._send_raw_command("RESET_ODOM")

        # Give the scanner a moment to deliver its first frames
        if not self.first_scan_event.wait(timeout=3.0):
            print("Warning: no LIDAR data received yet; motion will pause until scans arrive.")

        print("Connected. Odometry reset to (0,0,0).")
        return True

    def _safe_close_serial(self):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass

    def _disconnect(self):
        print("Disconnecting...")
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
        print("Disconnected.")

    # --- Odometry reading ---
    def _start_odometry_listener(self):
        if self.odom_running:
            return
        self.odom_running = True
        self.odom_thread = threading.Thread(target=self._odometry_reader_loop, daemon=True)
        self.odom_thread.start()

    def _stop_odometry_listener(self):
        self.odom_running = False
        if self.odom_thread:
            self.odom_thread.join(timeout=2.0)
            self.odom_thread = None

    def _odometry_reader_loop(self):
        while self.odom_running and self.serial_conn and self.serial_conn.is_open:
            try:
                raw = self.serial_conn.readline()
            except serial.SerialException as exc:
                print(f"Serial read error: {exc}")
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
                    print(f"[STM32] {line}")
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
        self.lidar_running = True
        self.lidar_thread = threading.Thread(target=self._lidar_reader_loop, daemon=True)
        self.lidar_thread.start()

    def _stop_lidar_listener(self):
        self.lidar_running = False
        if self.lidar_thread:
            self.lidar_thread.join(timeout=2.0)
            self.lidar_thread = None

    def _lidar_reader_loop(self):
        while self.lidar_running and self.lidar:
            try:
                for scan in self.lidar.iter_scans(scan_type='express'):
                    if not self.lidar_running:
                        break
                    with self.scan_lock:
                        self.latest_scan = scan
                    if not self.first_scan_event.is_set():
                        self.first_scan_event.set()
            except RPLidarException as exc:
                print(f"LIDAR read error: {exc}")
                time.sleep(0.5)
            except Exception as exc:
                print(f"Unexpected LIDAR error: {exc}")
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

    def _send_rotate(self, desired_angle_deg):
        """Rotate by desired_angle_deg relative to current heading."""
        if abs(desired_angle_deg) < ANGLE_TOLERANCE_DEG:
            return True
        cmd = f"ROTATE_DEG {desired_angle_deg:.2f}"
        print(f"ROTATE: {cmd}")
        self._clear_response_queue()
        self._send_raw_command(cmd)
        result, line = self._wait_for_response(
            success_tokens=["TARGET_REACHED"],
            failure_tokens=["TIMEOUT"],
            timeout=ROTATE_TIMEOUT_SEC
        )
        if result:
            return True
        if result is False:
            if line and line.startswith("TIMEOUT"):
                print("Rotation reported TIMEOUT but assuming success (motors idle).")
                return True
            print(f"Warning: rotation failed ({line}).")
            return False
        print("Warning: rotation timeout with no MCU response.")
        return False

    def _send_move(self, target_distance):
        if target_distance < DISTANCE_TOLERANCE_M:
            return True
        cmd = f"MOVE {target_distance:.3f}"
        print(f"MOVE: {cmd}")
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
            print(f"Warning: move failed ({line}).")
            return False
        print("Warning: move timeout with no MCU response.")
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

    def _choose_heading_with_avoidance(self, desired_heading_world, step_distance):
        """Pick a heading that is both safe (clear corridor) and still progresses toward the goal."""
        pose_heading_deg = math.degrees(self._get_pose()['theta'])
        required_clearance = max(step_distance + CLEARANCE_MARGIN_M, OBSTACLE_STOP_DISTANCE_M)

        forward_clear = self._heading_clearance(
            desired_heading_world, pose_heading_deg, FORWARD_SCAN_ANGLE_DEG, OBSTACLE_LOOKAHEAD_M
        )
        if forward_clear >= required_clearance:
            return desired_heading_world

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
        return self._send_rotate(rotate_angle)

    def _drive_step(self, desired_heading_world, step_distance):
        attempts = 0
        while attempts <= MAX_BLOCKED_RETRIES:
            if not self.first_scan_event.is_set():
                print("Waiting for first LIDAR scan before moving...")
                self.first_scan_event.wait(timeout=1.0)
                attempts += 1
                continue

            chosen_heading = self._choose_heading_with_avoidance(desired_heading_world, step_distance)
            if chosen_heading is None:
                attempts += 1
                print("Path blocked; waiting for an opening...")
                time.sleep(BLOCKED_RETRY_WAIT_SEC)
                continue

            if not self._rotate_to_heading(chosen_heading):
                return False
            if not self._send_move(step_distance):
                return False
            return True

        print("Path remained blocked after multiple retries.")
        return False

    # --- Navigation logic ---
    def navigate_to(self, goal):
        if not self.first_scan_event.is_set():
            print("Waiting for initial LIDAR data...")
            self.first_scan_event.wait(timeout=3.0)

        while True:
            pose = self._get_pose()
            dx = goal[0] - pose['x']
            dy = goal[1] - pose['y']
            distance = math.hypot(dx, dy)
            if distance < DISTANCE_TOLERANCE_M:
                print("Goal reached.")
                final_pose = self._get_pose()
                print(
                    f"Pose=({final_pose['x']:.2f}, {final_pose['y']:.2f}, "
                    f"{math.degrees(final_pose['theta']):.1f} deg)"
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

            if not self._drive_step(desired_heading, step_distance):
                print("Navigation aborted due to repeated blockages or command errors.")
                return

    # --- CLI loop ---
    def run(self):
        if not self._connect():
            return
        try:
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
                    print("Invalid format. Use x,y (e.g., 0.5,-0.2).")
                    continue
                self.navigate_to(goal)
        finally:
            self._disconnect()


if __name__ == '__main__':
    navigator = OdomOnlyNavigator()
    navigator.run()
