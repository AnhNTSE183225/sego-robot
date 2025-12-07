#!/usr/bin/env python3
"""
Test script for MOVE command - sends forward movement commands directly to STM32.
Replicates serial communication from odom_only_navigator.py for debugging.

Usage:
    python test_run.py              # Interactive mode
    python test_run.py 0.5          # Move forward 0.5 meters
    python test_run.py 1.0          # Move forward 1.0 meter
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
    # Drift calibration history:
    #   move_skew=0.015 → drifts LEFT 4.5cm/90cm
    #   move_skew=0.018 → drifts RIGHT 2.4cm/90cm
    #   move_skew=0.017 → calibrated (in config)
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
    
    logger = logging.getLogger("test.move")
    logger.info(f"Logging configured: Console={log_level_str}, File=INFO -> {log_file}")
    return logger


class STM32Tester:
    """Minimal STM32 communication for testing - mirrors odom_only_navigator.py"""
    
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger("test.move")
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
        
        has_overrides = any(v is not None for v in TEST_OVERRIDES.values())
        if not has_overrides:
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
    
    # =========================================================================
    # MOVE COMMAND - Replicates odom_only_navigator.py _send_move() EXACTLY
    # =========================================================================
    
    def send_move(self, distance_m):
        """
        Send MOVE command to STM32.
        
        This replicates _send_move() from odom_only_navigator.py:
        - "OK MOVE" = command received, movement started
        - "MOVE TARGET_REACHED" = movement completed successfully
        - "MOVE TIMEOUT" = movement timed out
        
        Args:
            distance_m: Distance to move forward in meters
        
        Returns:
            True if movement completed successfully
        """
        if distance_m < 0.01:  # Less than 1cm
            self.logger.info("Distance too small, skipping move.")
            return True
        
        move_timeout = self.config.get('motion', {}).get('move_timeout_sec', 25.0)
        
        # Reset STM32 odom before move (like navigator does)
        self._send_command("RESET_ODOM")
        self._wait_for_response(["OK"], ["ERR"], timeout=1.0)
        time.sleep(0.1)
        
        cmd = f"MOVE {distance_m:.3f}"
        self._clear_response_queue()
        self._send_command(cmd)
        
        # Step 1: Wait for OK MOVE (command acknowledged)
        result, line = self._wait_for_response(
            success_tokens=["OK MOVE"],
            failure_tokens=["ERR"],
            timeout=2.0
        )
        if not result:
            self.logger.warning(f"MOVE not acknowledged: {line}")
            return False
        
        self.logger.info(f"Move started. Waiting for completion (timeout={move_timeout}s)...")
        
        # Step 2: Wait for MOVE TARGET_REACHED or MOVE TIMEOUT
        result, line = self._wait_for_response(
            success_tokens=["MOVE TARGET_REACHED"],
            failure_tokens=["MOVE TIMEOUT", "OK STOP"],
            timeout=move_timeout
        )
        
        if result and line and "TARGET_REACHED" in line:
            self.logger.info(f"Move completed: {line}")
            # Update pose tracking
            heading_rad = math.radians(self.pose['heading_deg'])
            self.pose['x'] += distance_m * math.cos(heading_rad)
            self.pose['y'] += distance_m * math.sin(heading_rad)
            self.logger.info(f"Pose updated: ({self.pose['x']:.2f}, {self.pose['y']:.2f}, {self.pose['heading_deg']:.1f}°)")
            # Reset STM32 odom after successful move
            self._send_command("RESET_ODOM")
            time.sleep(0.1)
            return True
        
        # Movement was interrupted/timed out - check actual distance from odom
        time.sleep(0.1)
        actual_distance = self.odom['x']  # Forward distance in robot frame
        
        if actual_distance > 0.01:
            self.logger.info(f"Move interrupted at {actual_distance:.3f}m (commanded: {distance_m:.3f}m)")
            # Update pose with actual distance
            heading_rad = math.radians(self.pose['heading_deg'])
            self.pose['x'] += actual_distance * math.cos(heading_rad)
            self.pose['y'] += actual_distance * math.sin(heading_rad)
            self.logger.info(f"Pose updated: ({self.pose['x']:.2f}, {self.pose['y']:.2f}, {self.pose['heading_deg']:.1f}°)")
        else:
            self.logger.info(f"Move barely started (odom x={self.odom['x']:.3f}m)")
        
        self._send_command("RESET_ODOM")
        time.sleep(0.1)
        
        if result is False:
            self.logger.warning(f"Move failed: {line}")
        else:
            self.logger.warning("Move timeout with no MCU response.")
        return False
    
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
            distance_m = float(sys.argv[1])
            tester.send_move(distance_m)
            tester.print_odom()
        else:
            # Interactive mode
            print("\n=== MOVE Command Tester ===")
            print("Commands:")
            print("  move <distance>    - Move forward by distance (meters)")
            print("  param <name> <val> - Set STM32 parameter (e.g. param odom_scale 0.90)")
            print("  stop               - Emergency stop")
            print("  odom               - Print current odometry")
            print("  reset              - Reset odometry and pose")
            print("  q / quit           - Exit")
            print()
            print(f"TEST_OVERRIDES: {TEST_OVERRIDES}")
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
                elif command == 'move':
                    if len(parts) >= 2:
                        distance = float(parts[1])
                        tester.send_move(distance)
                    else:
                        print("Usage: move <distance_meters>")
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
                    tester._wait_for_response(["OK", "TARGET", "TIMEOUT"], ["ERR"], timeout=25.0)
    
    finally:
        tester.disconnect()


if __name__ == '__main__':
    main()
