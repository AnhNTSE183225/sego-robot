import serial
import threading
import time
import math
import queue


# Serial configuration
SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200

# Motion tolerances
DISTANCE_TOLERANCE_M = 0.02
ANGLE_TOLERANCE_DEG = 2.0

# Command timeouts
MOVE_TIMEOUT_SEC = 25.0
ROTATE_TIMEOUT_SEC = 15.0

# Movement mode: True -> use axis-aligned L-shape moves (X then Y)
AXIS_ALIGNED_MOVES = True


class OdomOnlyNavigator:
    """
    Minimal navigator that relies purely on STM32 odometry.
    Accepts (x, y) goals in meters, rotates toward the goal, and drives straight.
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

    # --- Connection management ---
    def _connect(self):
        print("Connecting to STM32 controller...")
        try:
            self.serial_conn = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            time.sleep(1.0)
        except serial.SerialException as exc:
            print(f"Error opening {SERIAL_PORT}: {exc}")
            return False

        self._start_odometry_listener()
        self._send_raw_command("RESET_ODOM")
        print("Connected. Odometry reset to (0,0,0).")
        return True

    def _disconnect(self):
        print("Disconnecting...")
        self._stop_odometry_listener()
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
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
                # Show MCU responses (OK, TARGET_REACHED, etc.)
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

    # --- Navigation logic ---
    def navigate_to(self, goal):
        pose = self._get_pose()
        dx = goal[0] - pose['x']
        dy = goal[1] - pose['y']
        distance = math.hypot(dx, dy)
        if distance < DISTANCE_TOLERANCE_M:
            print("Already at goal.")
            return

        if AXIS_ALIGNED_MOVES:
            self._navigate_axis_aligned(goal)
        else:
            self._navigate_direct(dx, dy)

    def _navigate_direct(self, dx, dy):
        pose = self._get_pose()
        target_heading = math.degrees(math.atan2(dy, dx))
        current_heading = math.degrees(pose['theta'])
        rotate_angle = target_heading - current_heading
        rotate_angle = ((rotate_angle + 180) % 360) - 180
        distance = math.hypot(dx, dy)

        if not self._send_rotate(rotate_angle):
            print("Rotation failed; aborting.")
            return
        if not self._send_move(distance):
            print("Move failed; aborting.")
            return
        final_pose = self._get_pose()
        print(f"Goal reached. Pose≈({final_pose['x']:.2f}, {final_pose['y']:.2f}, {math.degrees(final_pose['theta']):.1f}°)")

    def _navigate_axis_aligned(self, goal):
        pose = self._get_pose()
        # Move in X first
        remaining_x = goal[0] - pose['x']
        if abs(remaining_x) >= DISTANCE_TOLERANCE_M:
            target_heading = 0.0 if remaining_x >= 0 else 180.0
            current_heading = math.degrees(pose['theta'])
            rotate_angle = target_heading - current_heading
            rotate_angle = ((rotate_angle + 180) % 360) - 180
            if not self._send_rotate(rotate_angle):
                print("Rotation failed; aborting.")
                return
            if not self._send_move(abs(remaining_x)):
                print("Move failed; aborting.")
                return
            pose = self._get_pose()
        # Move in Y second
        remaining_y = goal[1] - pose['y']
        if abs(remaining_y) >= DISTANCE_TOLERANCE_M:
            target_heading = 90.0 if remaining_y >= 0 else -90.0
            current_heading = math.degrees(pose['theta'])
            rotate_angle = target_heading - current_heading
            rotate_angle = ((rotate_angle + 180) % 360) - 180
            if not self._send_rotate(rotate_angle):
                print("Rotation failed; aborting.")
                return
            if not self._send_move(abs(remaining_y)):
                print("Move failed; aborting.")
                return
        final_pose = self._get_pose()
        print(f"Goal reached. Pose≈({final_pose['x']:.2f}, {final_pose['y']:.2f}, {math.degrees(final_pose['theta']):.1f}°)")

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
