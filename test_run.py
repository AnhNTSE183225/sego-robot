#!/usr/bin/env python3
"""
Test script for RUN command - sends motor commands directly to STM32.
Replicates serial communication from odom_only_navigator.py for debugging.

Usage:
    python test_run.py                     # Interactive mode
    python test_run.py F 1000              # Run all motors Forward for 1000ms
    python test_run.py F R S 500           # Motor1=Forward, Motor2=Reverse, Motor3=Stop for 500ms
"""

import json
import math
import queue
import serial
import sys
import threading
import time
from pathlib import Path


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


class STM32Tester:
    """Minimal STM32 communication for testing - mirrors odom_only_navigator.py"""
    
    def __init__(self, config):
        self.config = config
        self.serial_conn = None
        self.response_queue = queue.Queue()
        self._serial_running = False
        self._serial_thread = None
        
        # Latest odometry from STM32
        self.odom = {'x': 0.0, 'y': 0.0, 'theta_rad': 0.0}
    
    def connect(self):
        """Connect to STM32 via serial"""
        port = self.config['serial']['port']
        baud = self.config['serial']['baud_rate']
        
        print(f"Connecting to STM32 on {port} @ {baud}...")
        try:
            self.serial_conn = serial.Serial(port, baud, timeout=1)
            time.sleep(1.0)  # Wait for connection to stabilize
        except serial.SerialException as e:
            print(f"Error: {e}")
            return False
        
        self._start_serial_listener()
        time.sleep(0.2)
        
        # Configure STM32 params if available
        stm32_params = self.config.get('stm32_params', {})
        if stm32_params:
            self._configure_stm32_params(stm32_params)
        
        # Reset odometry
        self._send_command("RESET_ODOM")
        self._wait_for_response(["OK RESET_ODOM"], ["ERR"], timeout=2.0)
        
        print("Connected!")
        return True
    
    def disconnect(self):
        """Disconnect from STM32"""
        self._serial_running = False
        if self._serial_thread:
            self._serial_thread.join(timeout=2.0)
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        print("Disconnected.")
    
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
                    continue  # Don't put odom in response queue
            
            print(f"[STM32] {line}")
            self.response_queue.put(line)
    
    def _send_command(self, cmd):
        """Send command to STM32"""
        if not self.serial_conn:
            return
        full_cmd = cmd.strip() + "\r\n"
        self.serial_conn.write(full_cmd.encode('utf-8'))
        print(f">>> {cmd}")
    
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
        print("Configuring STM32 parameters...")
        for name, value in params.items():
            if name.startswith('_'):  # Skip comments
                continue
            if 'timeout' in name:
                value_str = str(int(value))
            else:
                value_str = f"{value:.6f}"
            
            self._send_command(f"SET_PARAM {name} {value_str}")
            result, _ = self._wait_for_response(["OK SET_PARAM"], ["ERR"], timeout=0.5)
            if not result:
                print(f"  Warning: Failed to set {name}")
        print("Parameters configured.")
    
    # =========================================================================
    # RUN COMMAND - Replicates odom_only_navigator.py behavior
    # =========================================================================
    
    def run_motors(self, dir1, dir2, dir3, duration_ms):
        """
        Send RUN command to STM32.
        
        Args:
            dir1, dir2, dir3: Direction for each motor ('F', 'R', 'S')
            duration_ms: Duration in milliseconds
        """
        cmd = f"RUN {dir1},{dir2},{dir3} {duration_ms}"
        self._clear_response_queue()
        self._send_command(cmd)
        
        # Wait for acknowledgment
        result, line = self._wait_for_response(
            success_tokens=["OK RUN"],
            failure_tokens=["ERR"],
            timeout=2.0
        )
        
        if result:
            print(f"RUN started. Waiting {duration_ms}ms...")
            # Wait for duration + buffer
            time.sleep(duration_ms / 1000.0 + 0.5)
            print("RUN complete.")
            return True
        else:
            print(f"RUN failed: {line}")
            return False
    
    def run_all(self, direction, duration_ms):
        """Run all motors in same direction"""
        return self.run_motors(direction, direction, direction, duration_ms)
    
    def stop(self):
        """Emergency stop"""
        self._send_command("STOP")
        result, _ = self._wait_for_response(["OK STOP"], [], timeout=1.0)
        return result
    
    def get_odom(self):
        """Get current odometry"""
        return {
            'x': self.odom['x'],
            'y': self.odom['y'],
            'theta_deg': math.degrees(self.odom['theta_rad'])
        }
    
    def print_odom(self):
        """Print current odometry"""
        odom = self.get_odom()
        print(f"Odometry: x={odom['x']:.4f}m, y={odom['y']:.4f}m, theta={odom['theta_deg']:.2f}°")


def main():
    config = load_config()
    tester = STM32Tester(config)
    
    if not tester.connect():
        sys.exit(1)
    
    try:
        # Check command line args
        if len(sys.argv) >= 3:
            # Command line mode
            if len(sys.argv) == 3:
                # RUN dir duration
                direction = sys.argv[1].upper()
                duration_ms = int(sys.argv[2])
                tester.run_all(direction, duration_ms)
            elif len(sys.argv) == 5:
                # RUN dir1 dir2 dir3 duration
                dir1 = sys.argv[1].upper()
                dir2 = sys.argv[2].upper()
                dir3 = sys.argv[3].upper()
                duration_ms = int(sys.argv[4])
                tester.run_motors(dir1, dir2, dir3, duration_ms)
            else:
                print("Usage: python test_run.py [dir] [duration_ms]")
                print("       python test_run.py [dir1] [dir2] [dir3] [duration_ms]")
            tester.print_odom()
        else:
            # Interactive mode
            print("\n=== RUN Command Tester ===")
            print("Commands:")
            print("  run <dir> <ms>           - Run all motors (F/R/S)")
            print("  run <d1> <d2> <d3> <ms>  - Run each motor separately")
            print("  stop                     - Emergency stop")
            print("  odom                     - Print current odometry")
            print("  reset                    - Reset odometry")
            print("  q / quit                 - Exit")
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
                elif command == 'run':
                    if len(parts) == 3:
                        tester.run_all(parts[1].upper(), int(parts[2]))
                    elif len(parts) == 5:
                        tester.run_motors(parts[1].upper(), parts[2].upper(), 
                                         parts[3].upper(), int(parts[4]))
                    else:
                        print("Usage: run <dir> <ms> OR run <d1> <d2> <d3> <ms>")
                    tester.print_odom()
                else:
                    # Send raw command
                    tester._send_command(cmd)
                    tester._wait_for_response(["OK", "TARGET", "TIMEOUT"], ["ERR"], timeout=5.0)
    
    finally:
        tester.disconnect()


if __name__ == '__main__':
    main()
