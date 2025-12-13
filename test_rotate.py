#!/usr/bin/env python3
"""
Test script for ROTATE_DEG command - sends rotation commands directly to STM32.
Replicates serial communication from odom_only_navigator.py for debugging.

Usage:
    python test_rotate.py              # Interactive mode
    python test_rotate.py 90           # Rotate 90 degrees
    python test_rotate.py -45          # Rotate -45 degrees
"""

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


# =============================================================================
# TEST OVERRIDES - Hardcode values here to test before applying to config
# These override values from robot_config.json when not None
# =============================================================================
TEST_OVERRIDES = {
    # All calibration values now in robot_config.json
    # Uncomment below to test new values:
    # 'odom_angular_scale': 0.95,  # Calibrated: makes physical rotation accurate
    # 'rotate_tol_angle': 0.10,    # Tolerance fix (if needed)
    # 'angular_k': 1.2,            # Higher angular gain
    # 'min_duty_rotate': 0.35,     # Lower deadband
}
# =============================================================================


def load_config():
    """Load configuration from robot_config.json. Raises error if file missing."""
    config_path = Path(__file__).parent / "robot_config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Please create robot_config.json with required settings."
        )
    with open(config_path, 'r') as f:
        return json.load(f)


def configure_logging(config):
    """Configure console + rotating file logging (same as odom_only_navigator.py)."""
    log_file = config.get('logging', {}).get('file', 'robot.log')
    log_level_str = config.get('logging', {}).get('level', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    if root.handlers:
        root.handlers.clear()
    root.setLevel(logging.DEBUG)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(formatter)

    # File handler (rotating)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)
    
    logger = logging.getLogger("test.rotate")
    logger.info(f"Logging configured: Console={log_level_str}, File=INFO -> {log_file}")
    return logger


class STM32Tester:
    """Minimal STM32 communication for testing - mirrors odom_only_navigator.py"""
    
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("test.rotate")
        self.serial_conn = None
        self.response_queue = queue.Queue()
        self._serial_running = False
        self._serial_thread = None
        
        # Latest odometry from STM32
        self.odom = {'x': 0.0, 'y': 0.0, 'theta_rad': 0.0}
        
        # Pose tracking (like odom_only_navigator.py)
        self.pose = {'x': 0.0, 'y': 0.0, 'heading_deg': 0.0}
    
    def connect(self):
        """Connect to STM32 via serial"""
        port = self.config['serial']['port']
        baud = self.config['serial']['baud_rate']
        
        self.logger.info(f"Connecting to STM32 on {port} @ {baud}...")
        try:
            self.serial_conn = serial.Serial(port, baud, timeout=1)
            time.sleep(1.0)
        except serial.SerialException as e:
            self.logger.error(f"Serial connection error: {e}")
            return False
        
        self._start_serial_listener()
        time.sleep(0.2)
        
        # Configure STM32 params if available
        stm32_params = self.config.get('stm32_params', {})
        if stm32_params:
            self._configure_stm32_params(stm32_params)
        
        # Apply test overrides (for debugging - these override config values)
        self._apply_test_overrides()
        
        # Reset odometry
        self._send_command("RESET_ODOM")
        self._wait_for_response(["OK RESET_ODOM"], ["ERR"], timeout=2.0)
        
        self.logger.info("Connected!")
        return True
    
    def disconnect(self):
        """Disconnect from STM32"""
        self._serial_running = False
        if self._serial_thread:
            self._serial_thread.join(timeout=2.0)
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        self.logger.info("Disconnected.")
    
    def _start_serial_listener(self):
        """Start background thread to read serial responses"""
        self._serial_running = True
        self._serial_thread = threading.Thread(target=self._serial_reader_loop, daemon=True)
        self._serial_thread.start()
    
    def _serial_reader_loop(self):
        """Background thread: read lines from STM32, parse odometry"""
        while self._serial_running and self.serial_conn and self.serial_conn.is_open:
            try:
                raw = self.serial_conn.readline()
            except serial.SerialException:
                break
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            
            # Parse odometry lines: x,y,theta,vx,vy,omega,m1,m2,m3
            if line[0].isdigit() or line[0] == '-':
                parts = line.split(',')
                if len(parts) >= 3:
                    try:
                        self.odom['x'] = float(parts[0])
                        self.odom['y'] = float(parts[1])
                        self.odom['theta_rad'] = float(parts[2])
                    except ValueError:
                        pass
                    continue
            
            self.logger.info(f"[STM32] {line}")
            self.response_queue.put(line)
    
    def _send_command(self, cmd):
        """Send command to STM32"""
        if not self.serial_conn:
            return
        full_cmd = cmd.strip() + "\r\n"
        self.serial_conn.write(full_cmd.encode('utf-8'))
        self.logger.info(f">>> {cmd}")
    
    def _clear_response_queue(self):
        """Clear any pending responses"""
        while not self.response_queue.empty():
            try:
                self.response_queue.get_nowait()
            except queue.Empty:
                break
    
    def _wait_for_response(self, success_tokens, failure_tokens, timeout):
        """Wait for a response matching success or failure tokens"""
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
    
    def _configure_stm32_params(self, params):
        """Send SET_PARAM commands for all parameters"""
        self.logger.info("Configuring STM32 parameters...")
        for name, value in params.items():
            if name.startswith('_'):
                continue
            if 'timeout' in name:
                value_str = str(int(value))
            else:
                value_str = f"{value:.6f}"
            
            self._send_command(f"SET_PARAM {name} {value_str}")
            result, _ = self._wait_for_response(["OK SET_PARAM"], ["ERR"], timeout=0.5)
            if result:
                self.logger.debug(f"  {name} = {value}")
            else:
                self.logger.warning(f"  Failed to set {name}")
        self.logger.info("Parameters configured.")
    
    def _apply_test_overrides(self):
        """Apply TEST_OVERRIDES for debugging (overrides config values)"""
        if not TEST_OVERRIDES:
            return
        
        self.logger.info("Applying TEST OVERRIDES...")
        for name, value in TEST_OVERRIDES.items():
            if value is None:
                continue
            if 'timeout' in name:
                value_str = str(int(value))
            else:
                value_str = f"{value:.6f}"
            
            self._send_command(f"SET_PARAM {name} {value_str}")
            result, _ = self._wait_for_response(["OK SET_PARAM"], ["ERR"], timeout=0.5)
            if result:
                self.logger.info(f"  [OVERRIDE] {name} = {value}")
            else:
                self.logger.warning(f"  [OVERRIDE] Failed to set {name}")
        self.logger.info("Test overrides applied.")
    
    @staticmethod
    def normalize_angle_deg(angle):
        """Wrap angle to [-180, 180]"""
        while angle > 180.0:
            angle -= 360.0
        while angle < -180.0:
            angle += 360.0
        return angle
    
    def quantize_rotation_angle(self, angle_deg):
        """
        Quantize rotation angle to nearest allowed discrete angle.
        
        Returns:
            Quantized angle (one of: 30, 60, 90, 120, 150, 180) with original sign
            Returns 0.0 if angle is too small (<15°)
        """
        abs_angle = abs(angle_deg)
        sign = 1 if angle_deg >= 0 else -1
        
        # Get allowed angles from config
        rotation_config = self.config.get('rotation_calibration', {})
        allowed_angles = rotation_config.get('allowed_angles', [30, 60, 90, 120, 150, 180])
        
        # Too small to rotate
        if abs_angle < 15:
            self.logger.info(f"Angle {angle_deg:.1f}° too small (<15°), skipping rotation")
            return 0.0
        
        # Find nearest allowed angle
        nearest = min(allowed_angles, key=lambda x: abs(x - abs_angle))
        quantized = sign * nearest
        
        if abs(quantized - angle_deg) > 0.1:
            self.logger.info(f"Quantized {angle_deg:.1f}° → {quantized:.1f}°")
        
        return quantized
    
    def get_calibration_scale(self, angle_deg):
        """
        Get calibration scale factor for a specific rotation angle.
        
        Args:
            angle_deg: Quantized rotation angle (should be one of allowed_angles)
        
        Returns:
            Calibration scale factor (default 1.0 if not found)
        """
        rotation_config = self.config.get('rotation_calibration', {})
        abs_angle = abs(int(angle_deg))
        scale = float(rotation_config.get(str(abs_angle), 1.0))
        return scale
    
    # =========================================================================
    # ROTATE_DEG COMMAND - Replicates odom_only_navigator.py behavior EXACTLY
    # =========================================================================
    
    def rotate_deg(self, angle_deg, repeat_count=1):
        """
        Send ROTATE_DEG command to STM32 with discrete angle quantization and calibration.
        
        This replicates _send_rotate() from odom_only_navigator.py with enhancements:
        - Quantizes requested angle to nearest allowed discrete angle (30°, 60°, 90°, etc.)
        - Applies angle-specific calibration factor from robot_config.json
        - Can repeat rotation multiple times without resetting odometry (for testing)
        - "OK ROTATE_DEG" = command received, rotation started
        - "TARGET_REACHED" = rotation completed successfully
        - "TIMEOUT" = rotation timed out
        
        Args:
            angle_deg: Requested rotation angle in degrees (will be quantized)
            repeat_count: Number of times to repeat the rotation (default: 1)
        
        Returns:
            True if all rotations completed successfully
        """
        # Step 1: Quantize to nearest allowed angle
        quantized_angle = self.quantize_rotation_angle(angle_deg)
        if abs(quantized_angle) < 0.1:
            return True
        
        # Step 2: Apply calibration for this specific angle
        calibration_scale = self.get_calibration_scale(quantized_angle)
        calibrated_angle = quantized_angle * calibration_scale
        
        if repeat_count > 1:
            self.logger.info(
                f"Rotation: requested={angle_deg:.1f}°, quantized={quantized_angle:.1f}°, "
                f"scale={calibration_scale:.4f}, calibrated={calibrated_angle:.2f}°, "
                f"repeat={repeat_count}x"
            )
        else:
            self.logger.info(
                f"Rotation: requested={angle_deg:.1f}°, quantized={quantized_angle:.1f}°, "
                f"scale={calibration_scale:.4f}, calibrated={calibrated_angle:.2f}°"
            )
        
        rotate_timeout = self.config.get('motion', {}).get('rotate_timeout_sec', 15.0)
        rotate_timeout_tol = self.config.get('obstacle_avoidance', {}).get('rotate_timeout_tolerance_deg', 15.0)
        
        # Reset STM32 odom before first rotation
        self._send_command("RESET_ODOM")
        self._wait_for_response(["OK"], ["ERR"], timeout=1.0)
        time.sleep(0.1)
        
        # Execute rotation repeat_count times
        all_success = True
        for i in range(repeat_count):
            if repeat_count > 1:
                self.logger.info(f"--- Rotation {i+1}/{repeat_count} ---")
            
            # Step 3: Send calibrated angle to STM32
            cmd = f"ROTATE_DEG {calibrated_angle:.2f}"
            self._clear_response_queue()
            self._send_command(cmd)
            
            # Step 4: Wait for OK ROTATE_DEG (command acknowledged)
            result, line = self._wait_for_response(
                success_tokens=["OK ROTATE_DEG"],
            failure_tokens=["ERR"],
            timeout=2.0
        )
            if not result:
                self.logger.warning(f"ROTATE_DEG not acknowledged: {line}")
                all_success = False
                break
            
            if repeat_count == 1:
                self.logger.info(f"Rotation started. Waiting for completion (timeout={rotate_timeout}s)...")
            
            # Step 5: Wait for TARGET_REACHED or TIMEOUT
            result, line = self._wait_for_response(
            success_tokens=["TARGET_REACHED"],
            failure_tokens=["TIMEOUT"],
            timeout=rotate_timeout
        )
            
            if result:
                if repeat_count > 1:
                    self.logger.info(f"Rotation {i+1} completed: {line}")
                else:
                    self.logger.info(f"Rotation completed: {line}")
                # Don't reset odom between repetitions - only at the end
                if i == repeat_count - 1:
                    # Final rotation done - show cumulative result
                    time.sleep(0.1)
                    final_odom = self.get_odom()
                    self.logger.info(
                        f"=== After {repeat_count}x {quantized_angle:.1f}° rotations ==="
                    )
                    self.logger.info(
                        f"Cumulative STM32 Odom: theta={final_odom['theta_deg']:.2f}° "
                        f"(expected: {quantized_angle * repeat_count:.1f}°)"
                    )
                    self._send_command("RESET_ODOM")
                    time.sleep(0.1)
                continue
            else:
                # Rotation failed
                all_success = False
                break
        
        return all_success
        
        # Old single-rotation code below (unreachable now)
        if False:  # Keep for reference
            pass
        
        # Rotation timed out - check how far it got
        actual_rotation = math.degrees(self.odom['theta_rad'])
        rotation_err = abs(actual_rotation - angle_deg)
        
        if rotation_err <= rotate_timeout_tol:
            self.logger.warning(
                f"Rotation TIMEOUT but within tolerance (actual={actual_rotation:.1f}°, "
                f"target={angle_deg:.1f}°, err={rotation_err:.1f}° <= {rotate_timeout_tol}°)"
            )
            self._send_command("RESET_ODOM")
            time.sleep(0.1)
            return True
        
        if abs(actual_rotation) > 0.5:
            self.logger.info(f"Rotation interrupted at {actual_rotation:.1f}° (commanded: {angle_deg:.1f}°)")
        
        self._send_command("RESET_ODOM")
        time.sleep(0.1)
        
        if result is False:
            self.logger.warning(f"Rotation failed: {line}")
        else:
            self.logger.warning("Rotation timeout with no MCU response.")
        return False
    
    def rotate_to_heading(self, target_heading_world):
        """
        Rotate to absolute heading.
        Replicates _rotate_to_heading() from odom_only_navigator.py
        
        IMPORTANT: MCU expects clockwise positive, navigator uses CCW positive
        So: command_delta = -world_delta
        """
        current_heading = self.pose['heading_deg']
        
        # World delta (CCW positive)
        world_delta = self.normalize_angle_deg(target_heading_world - current_heading)
        
        # MCU expects clockwise positive, so invert sign
        command_delta = -world_delta
        
        self.logger.info(
            f"rotate_to_heading: current={current_heading:.1f}°, target={target_heading_world:.1f}°, "
            f"world_delta={world_delta:.1f}°, command_delta={command_delta:.1f}°"
        )
        
        ok = self.rotate_deg(command_delta)
        
        if ok:
            # Update internal pose tracking
            self.pose['heading_deg'] = self.normalize_angle_deg(
                self.pose['heading_deg'] + world_delta
            )
            self.logger.info(f"Pose updated: heading={self.pose['heading_deg']:.1f}°")
        
        return ok
    
    def stop(self):
        """Emergency stop"""
        self.logger.warning("EMERGENCY STOP!")
        self._send_command("STOP")
        result, _ = self._wait_for_response(["OK STOP"], [], timeout=1.0)
        return result
    
    def get_odom(self):
        """Get current STM32 odometry"""
        return {
            'x': self.odom['x'],
            'y': self.odom['y'],
            'theta_deg': math.degrees(self.odom['theta_rad'])
        }
    
    def print_odom(self):
        """Print current odometry"""
        odom = self.get_odom()
        self.logger.info(f"STM32 Odom: x={odom['x']:.4f}m, y={odom['y']:.4f}m, theta={odom['theta_deg']:.2f}°")
        self.logger.info(f"Pose Track: x={self.pose['x']:.4f}m, y={self.pose['y']:.4f}m, heading={self.pose['heading_deg']:.2f}°")


def main():
    config = load_config()
    configure_logging(config)
    
    tester = STM32Tester(config)
    
    if not tester.connect():
        sys.exit(1)
    
    try:
        # Check command line args
        if len(sys.argv) >= 2:
            # Command line mode
            angle_deg = float(sys.argv[1])
            repeat_count = int(sys.argv[2]) if len(sys.argv) >= 3 else 1
            tester.rotate_deg(angle_deg, repeat_count)
            tester.print_odom()
        else:
            # Interactive mode
            print("\n=== ROTATE_DEG Command Tester ===")
            print("Commands:")
            print("  rotate <angle> [count]  - Rotate by angle (degrees, MCU frame), optionally repeat count times")
            print("  to <heading>            - Rotate to absolute heading (world frame)")
            print("  param <name> <val>      - Set STM32 parameter (e.g. param rotate_tol_angle 0.10)")
            print("  stop               - Emergency stop")
            print("  odom               - Print current odometry")
            print("  reset              - Reset odometry and pose")
            print("  q / quit           - Exit")
            print()
            print("NOTE: MCU uses clockwise-positive convention")
            print("      Navigator uses counter-clockwise positive")
            print("      'rotate' sends direct to MCU, 'to' converts like navigator")
            print()
            print(f"TEST_OVERRIDES applied: {TEST_OVERRIDES}")
            print()
            
            while True:
                try:
                    cmd = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                
                if not cmd:
                    continue
                
                parts = cmd.split()
                command = parts[0].lower()
                
                if command in ('q', 'quit', 'exit'):
                    break
                elif command == 'stop':
                    tester.stop()
                elif command == 'odom':
                    tester.print_odom()
                elif command == 'reset':
                    tester._send_command("RESET_ODOM")
                    tester._wait_for_response(["OK"], ["ERR"], timeout=1.0)
                    tester.pose = {'x': 0.0, 'y': 0.0, 'heading_deg': 0.0}
                    tester.logger.info("Odometry and pose reset.")
                elif command == 'rotate':
                    if len(parts) >= 2:
                        angle = float(parts[1])
                        repeat_count = int(parts[2]) if len(parts) >= 3 else 1
                        tester.rotate_deg(angle, repeat_count)
                    else:
                        print("Usage: rotate <angle_deg> [repeat_count]")
                    tester.print_odom()
                elif command == 'to':
                    if len(parts) >= 2:
                        heading = float(parts[1])
                        tester.rotate_to_heading(heading)
                    else:
                        print("Usage: to <target_heading_deg>")
                    tester.print_odom()
                elif command == 'param':
                    if len(parts) >= 3:
                        param_name = parts[1]
                        param_value = parts[2]
                        tester._send_command(f"SET_PARAM {param_name} {param_value}")
                        tester._wait_for_response(["OK SET_PARAM"], ["ERR"], timeout=1.0)
                    else:
                        print("Usage: param <name> <value>")
                else:
                    # Send raw command
                    tester._send_command(cmd)
                    tester._wait_for_response(["OK", "TARGET", "TIMEOUT"], ["ERR"], timeout=15.0)
    
    finally:
        tester.disconnect()


if __name__ == '__main__':
    main()
