#!/usr/bin/env python3
"""
Scan-Matching Calibration Script for Discrete Rotation System

Uses RPLidar scan-matching to accurately calibrate rotation angles.
Much more accurate than protractor-based calibration (±0.5-1.0° vs ±5°).

Prerequisites:
- Featureful environment (walls, shelves)
- Robot stationary before/after scan capture
- RPLidar connected and working

Usage:
    python scan_matching_calibration.py              # Calibrate all angles
    python scan_matching_calibration.py 30           # Calibrate 30° only
    python scan_matching_calibration.py 30 --cw      # Calibrate 30° clockwise only
    python scan_matching_calibration.py 30 --ccw     # Calibrate 30° counter-clockwise only
"""

import json
import logging
import logging.handlers
import math
import numpy as np
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, List

# Try to import RPLidar
try:
    from rplidar import RPLidar, RPLidarException
    LIDAR_AVAILABLE = True
    
    # Patch for rplidar library that returns 3 values from get_health() instead of 2
    _orig_get_health = RPLidar.get_health

    def _get_health_two_fields(self):
        result = _orig_get_health(self)
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            return result[0], result[1]
        return result, 0

    RPLidar.get_health = _get_health_two_fields
    
except ImportError:
    LIDAR_AVAILABLE = False
    RPLidar = None
    RPLidarException = Exception

# Import test_rotate for STM32 communication
from test_rotate import STM32Tester, load_config, configure_logging


# =============================================================================
# CONFIGURATION
# =============================================================================

SCAN_MATCH_CONFIG = {
    'min_confidence': 0.7,        # Minimum scan-matching confidence to accept
    'scan_settle_time_sec': 0.5,  # Wait time for robot to settle before scan
    'trials_per_angle': 10,       # Number of trials (each = 1 rotation)
    'angle_search_range_deg': 15, # Search range around expected (±15°)
    'angle_resolution_deg': 0.5,  # Resolution for correlative matching
}

# =============================================================================
# SCAN MATCHING FUNCTIONS
# =============================================================================

def capture_lidar_scan(lidar: 'RPLidar', num_scans: int = 3) -> Optional[np.ndarray]:
    """Capture and average multiple LIDAR scans.
    
    Args:
        lidar: RPLidar instance
        num_scans: Number of scans to average
    
    Returns:
        Array of (angle, distance) tuples, or None if failed
    """
    if not LIDAR_AVAILABLE or lidar is None:
        return None
    
    all_points = []
    scan_count = 0
    
    try:
        for scan in lidar.iter_scans():
            points = np.array([(angle, distance) for _, angle, distance in scan if distance > 0])
            if len(points) > 0:
                all_points.append(points)
                scan_count += 1
                if scan_count >= num_scans:
                    break
    except RPLidarException as e:
        logging.error(f"LIDAR scan error: {e}")
        return None
    
    if not all_points:
        return None
    
    # Combine all scans (simple concatenation for now)
    combined = np.vstack(all_points)
    return combined


def scan_to_cartesian(scan: np.ndarray) -> np.ndarray:
    """Convert polar scan (angle, distance) to Cartesian (x, y).
    
    Args:
        scan: Array of (angle_deg, distance_mm) 
    
    Returns:
        Array of (x, y) in mm
    """
    angles_rad = np.radians(scan[:, 0])
    distances = scan[:, 1]
    
    x = distances * np.cos(angles_rad)
    y = distances * np.sin(angles_rad)
    
    return np.column_stack([x, y])


def rotate_points(points: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate 2D points by angle.
    
    Args:
        points: Array of (x, y) points
        angle_deg: Rotation angle in degrees (CCW positive)
    
    Returns:
        Rotated points
    """
    angle_rad = np.radians(angle_deg)
    cos_a = np.cos(angle_rad)
    sin_a = np.sin(angle_rad)
    
    rotation_matrix = np.array([
        [cos_a, -sin_a],
        [sin_a, cos_a]
    ])
    
    return points @ rotation_matrix.T


def correlative_scan_match(scan_before: np.ndarray, scan_after: np.ndarray,
                           search_range_deg: float = 30.0,
                           resolution_deg: float = 0.5,
                           center_deg: float = 0.0) -> Tuple[float, float]:
    """Find rotation angle between two scans using correlative matching.
    
    This is a simple but robust method for rotation-only matching.
    
    Args:
        scan_before: Scan before rotation (polar: angle, distance)
        scan_after: Scan after rotation (polar: angle, distance)
        search_range_deg: Search range ±degrees around center
        resolution_deg: Search resolution
        center_deg: Center of search (expected rotation angle)
    
    Returns:
        Tuple of (best_angle_deg, confidence)
        - best_angle_deg: Estimated rotation (positive = CCW)
        - confidence: Match quality (0-1)
    """
    if scan_before is None or scan_after is None:
        return 0.0, 0.0
    
    # Convert to Cartesian
    points_before = scan_to_cartesian(scan_before)
    points_after = scan_to_cartesian(scan_after)
    
    if len(points_before) < 10 or len(points_after) < 10:
        return 0.0, 0.0
    
    # Try different rotation angles and find best match
    best_angle = center_deg
    best_score = -float('inf')
    
    # Search around center (expected angle)
    angles_to_try = np.arange(center_deg - search_range_deg, 
                              center_deg + search_range_deg + resolution_deg, 
                              resolution_deg)
    
    for test_angle in angles_to_try:
        # Rotate "before" points by test angle
        rotated_before = rotate_points(points_before, test_angle)
        
        # Compute match score (simple nearest-neighbor distance)
        score = compute_scan_match_score(rotated_before, points_after)
        
        if score > best_score:
            best_score = score
            best_angle = test_angle
    
    # Refine with finer resolution around best angle
    fine_resolution = resolution_deg / 4
    fine_angles = np.arange(best_angle - resolution_deg, best_angle + resolution_deg, fine_resolution)
    
    for test_angle in fine_angles:
        rotated_before = rotate_points(points_before, test_angle)
        score = compute_scan_match_score(rotated_before, points_after)
        
        if score > best_score:
            best_score = score
            best_angle = test_angle
    
    # Convert score to confidence (0-1)
    # Higher score = better match = higher confidence
    # This is a rough heuristic - adjust based on testing
    max_possible_score = len(points_after)
    confidence = min(1.0, best_score / (max_possible_score * 0.5))
    
    return best_angle, confidence


def compute_scan_match_score(points_a: np.ndarray, points_b: np.ndarray,
                             max_dist_mm: float = 50.0) -> float:
    """Compute match score between two point sets.
    
    Uses simple nearest-neighbor matching with distance threshold.
    
    Args:
        points_a: First point set (x, y)
        points_b: Second point set (x, y)
        max_dist_mm: Maximum distance for a point to be considered a match
    
    Returns:
        Match score (higher = better)
    """
    if len(points_a) == 0 or len(points_b) == 0:
        return 0.0
    
    score = 0.0
    
    # For each point in A, find nearest point in B
    for pa in points_a:
        dists = np.linalg.norm(points_b - pa, axis=1)
        min_dist = np.min(dists)
        
        if min_dist < max_dist_mm:
            # Score inversely proportional to distance
            score += 1.0 - (min_dist / max_dist_mm)
    
    return score


# =============================================================================
# CALIBRATION FUNCTIONS
# =============================================================================

class ScanMatchingCalibrator:
    """Calibrator using LIDAR scan-matching for accurate rotation measurement."""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("calibration")
        self.lidar = None
        self.tester = None
        
    def connect(self) -> bool:
        """Connect to LIDAR and STM32."""
        # Connect to STM32
        self.tester = STM32Tester(self.config)
        if not self.tester.connect():
            self.logger.error("Failed to connect to STM32")
            return False
        
        # Connect to LIDAR
        if LIDAR_AVAILABLE:
            lidar_port = self.config.get('lidar', {}).get('port', '/dev/ttyUSB0')
            try:
                self.lidar = RPLidar(lidar_port)
                info = self.lidar.get_info()
                health = self.lidar.get_health()
                self.logger.info(f"LIDAR connected: {info}")
                self.logger.info(f"LIDAR health: {health}")
                
                # Start motor
                self.lidar.start_motor()
                time.sleep(2.0)  # Wait for motor to spin up
                
            except Exception as e:
                self.logger.warning(f"LIDAR connection failed: {e}")
                self.logger.warning("Falling back to encoder-only calibration")
                self.lidar = None
        else:
            self.logger.warning("RPLidar library not available, using encoder-only calibration")
        
        return True
    
    def disconnect(self):
        """Disconnect from LIDAR and STM32."""
        if self.lidar:
            try:
                self.lidar.stop_motor()
                self.lidar.stop()
                self.lidar.disconnect()
            except:
                pass
        
        if self.tester:
            self.tester.disconnect()
    
    def calibrate_angle(self, angle_deg: int, direction: str = 'both',
                        trials: int = 10) -> dict:
        """Calibrate a specific rotation angle using multiple trials.
        
        Args:
            angle_deg: Angle to calibrate (30, 60, 90, etc.)
            direction: 'cw', 'ccw', or 'both'
            trials: Number of trials (each = 1 rotation)
        
        Returns:
            Dict with calibration results
        """
        results = {
            'angle': angle_deg,
            'direction': direction,
            'trials': trials,
            'tests': [],
            'scale_cw': None,
            'scale_ccw': None,
            'scale_combined': None,
            'confidence': 0.0,
        }
        
        if direction in ['cw', 'both']:
            # Clockwise test (positive angle in robot frame) - multiple trials
            result = self._run_calibration_trials(angle_deg, trials)
            results['tests'].append({'direction': 'cw', **result})
            if result['confidence'] >= SCAN_MATCH_CONFIG['min_confidence']:
                results['scale_cw'] = result['scale']
        
        if direction in ['ccw', 'both']:
            # Counter-clockwise test (negative angle in robot frame) - multiple trials
            result = self._run_calibration_trials(-angle_deg, trials)
            results['tests'].append({'direction': 'ccw', **result})
            if result['confidence'] >= SCAN_MATCH_CONFIG['min_confidence']:
                results['scale_ccw'] = result['scale']
        
        # Calculate combined scale if both directions tested
        if results['scale_cw'] is not None and results['scale_ccw'] is not None:
            results['scale_combined'] = (results['scale_cw'] + results['scale_ccw']) / 2
        elif results['scale_cw'] is not None:
            results['scale_combined'] = results['scale_cw']
        elif results['scale_ccw'] is not None:
            results['scale_combined'] = results['scale_ccw']
        
        # Overall confidence
        if results['tests']:
            results['confidence'] = sum(t['confidence'] for t in results['tests']) / len(results['tests'])
        
        return results
    
    def _run_calibration_trials(self, angle_deg: int, trials: int) -> dict:
        """Run multiple calibration trials and average results.
        
        Args:
            angle_deg: Target angle (positive = CW in robot frame)
            trials: Number of trials (each = 1 rotation)
        
        Returns:
            Dict with {expected, measured_encoder, measured_lidar, scale, confidence}
        """
        self.logger.info(f"Running {trials} trials of {angle_deg}°...")
        
        trial_results = []
        for trial_num in range(trials):
            self.logger.info(f"\n--- Trial {trial_num + 1}/{trials} ---")
            result = self._run_single_trial(angle_deg)
            trial_results.append(result)
        
        # Average the measurements
        valid_lidar = [r['measured_lidar'] for r in trial_results 
                       if r['measured_lidar'] is not None and r['confidence'] >= SCAN_MATCH_CONFIG['min_confidence']]
        
        if valid_lidar:
            avg_lidar = np.mean([abs(m) for m in valid_lidar])
            avg_confidence = np.mean([r['confidence'] for r in trial_results if r['measured_lidar'] is not None])
            scale = abs(angle_deg) / avg_lidar if avg_lidar > 0.1 else 1.0
            measurement_source = 'lidar'
        else:
            # Fallback to encoder
            avg_encoder = np.mean([abs(r['measured_encoder']) for r in trial_results])
            avg_lidar = None
            avg_confidence = 0.5
            scale = abs(angle_deg) / avg_encoder if avg_encoder > 0.1 else 1.0
            measurement_source = 'encoder'
        
        self.logger.info(
            f"Trials complete: expected={abs(angle_deg):.1f}°, "
            f"measured={avg_lidar if avg_lidar else avg_encoder:.1f}° ({measurement_source}), "
            f"scale={scale:.4f}, confidence={avg_confidence:.2f}"
        )
        
        return {
            'expected': abs(angle_deg),
            'measured_encoder': avg_encoder if not valid_lidar else trial_results[0]['measured_encoder'],
            'measured_lidar': avg_lidar,
            'measured_used': avg_lidar if avg_lidar else avg_encoder,
            'measurement_source': measurement_source,
            'scale': scale,
            'confidence': avg_confidence,
        }
    
    def _run_single_trial(self, angle_deg: int) -> dict:
        """Run a single calibration trial (1 rotation).
        
        Args:
            angle_deg: Target angle (positive = CW in robot frame)
        
        Returns:
            Dict with {expected, measured_encoder, measured_lidar, scale, confidence}
        """
        # Capture initial scan
        settle_time = SCAN_MATCH_CONFIG['scan_settle_time_sec']
        time.sleep(settle_time)
        
        scan_before = None
        if self.lidar:
            # Flush serial buffer before scan
            try:
                if hasattr(self.lidar, '_serial'):
                    self.lidar._serial.reset_input_buffer()
                    self.lidar._serial.reset_output_buffer()
            except:
                pass
            
            scan_before = capture_lidar_scan(self.lidar)
            if scan_before is not None:
                self.logger.info(f"Captured {len(scan_before)} points (before)")
        
        # Execute single rotation (no need to stop LIDAR for 1 rotation)
        success = self.tester.rotate_deg(angle_deg, repeat_count=1)
        
        if not success:
            self.logger.warning("Rotation command failed during execution")
        
        # Capture final scan
        time.sleep(settle_time)
        
        scan_after = None
        if self.lidar:
            # Flush serial buffer before scan
            try:
                if hasattr(self.lidar, '_serial'):
                    self.lidar._serial.reset_input_buffer()
                    self.lidar._serial.reset_output_buffer()
            except:
                pass
            
            time.sleep(0.2)  # Brief settle
            scan_after = capture_lidar_scan(self.lidar)
            if scan_after is not None:
                self.logger.info(f"Captured {len(scan_after)} points (after)")
        
        # Get encoder measurement
        odom = self.tester.get_odom()
        measured_encoder = odom['theta_deg']
        
        # Compute scan-matching measurement
        measured_lidar = None
        confidence = 0.5  # Default confidence for encoder-only
        
        if scan_before is not None and scan_after is not None:
            search_range = SCAN_MATCH_CONFIG['angle_search_range_deg']
            
            # Use commanded angle as search center (prior)
            # Note: angle_deg is in robot frame (CW+), scan-match returns world frame (CCW+)
            # So we expect scan-match to return approximately -angle_deg
            expected_scan_match = -angle_deg  # Robot CW+ → World CCW+
            
            measured_lidar, confidence = correlative_scan_match(
                scan_before, scan_after,
                search_range_deg=search_range,
                center_deg=expected_scan_match,
                resolution_deg=SCAN_MATCH_CONFIG['angle_resolution_deg']
            )
            self.logger.info(
                f"Scan-match: commanded={angle_deg:.1f}°, measured={measured_lidar:.2f}°, "
                f"confidence={confidence:.2f}"
            )
        
        # Use LIDAR measurement if available and confident, otherwise encoder
        if measured_lidar is not None and confidence >= SCAN_MATCH_CONFIG['min_confidence']:
            measured = abs(measured_lidar)
            measurement_source = 'lidar'
        else:
            measured = abs(measured_encoder)
            measurement_source = 'encoder'
            if measured_lidar is not None:
                self.logger.warning(f"Low confidence ({confidence:.2f}), using encoder measurement")
        
        # Calculate scale factor (for single rotation)
        expected_single = abs(angle_deg)
        if measured > 0.1:
            scale = expected_single / measured
        else:
            scale = 1.0
            self.logger.warning("Measured rotation near zero, using default scale")
        
        result = {
            'expected': expected_single,
            'measured_encoder': measured_encoder,
            'measured_lidar': measured_lidar,
            'measured_used': measured,
            'measurement_source': measurement_source,
            'scale': scale,
            'confidence': confidence,
        }
        

        
        return result
    
    def calibrate_all(self, angles: List[int] = None, direction: str = 'both') -> dict:
        """Calibrate all specified angles.
        
        Args:
            angles: List of angles to calibrate (default: all from config)
            direction: 'cw', 'ccw', or 'both'
        
        Returns:
            Dict with all calibration results
        """
        if angles is None:
            primitives = self.config.get('rotation_primitives', {})
            angles = primitives.get('allowed_angles', [30, 60, 90, 120, 150, 180])
        
        trials = SCAN_MATCH_CONFIG['trials_per_angle']
        all_results = {}
        
        for angle in angles:
            self.logger.info(f"\n{'='*60}")
            self.logger.info(f"Calibrating {angle}°")
            self.logger.info(f"{'='*60}")
            
            result = self.calibrate_angle(angle, direction, trials)
            all_results[angle] = result
            
            # Short pause between angles
            time.sleep(1.0)
        
        return all_results
    
    def generate_config_update(self, results: dict) -> dict:
        """Generate updated rotation_primitives config from calibration results.
        
        Args:
            results: Calibration results from calibrate_all()
        
        Returns:
            Updated rotation_primitives config
        """
        primitives = self.config.get('rotation_primitives', {}).copy()
        
        for angle, result in results.items():
            angle_key = str(angle)
            
            if result['scale_combined'] is not None:
                if angle_key not in primitives or not isinstance(primitives[angle_key], dict):
                    primitives[angle_key] = {
                        'scale': result['scale_combined'],
                        'drift_x_m': 0.0,
                        'drift_y_m': 0.0,
                    }
                else:
                    primitives[angle_key]['scale'] = result['scale_combined']
                
                self.logger.info(f"{angle}°: scale = {result['scale_combined']:.4f}")
        
        return primitives


# =============================================================================
# MAIN
# =============================================================================

def main():
    config = load_config()
    logger = configure_logging(config)
    
    calibrator = ScanMatchingCalibrator(config)
    
    if not calibrator.connect():
        sys.exit(1)
    
    try:
        # Parse command line arguments
        angles = None
        direction = 'both'
        
        if len(sys.argv) >= 2:
            try:
                angles = [int(sys.argv[1])]
            except ValueError:
                pass
        
        if '--cw' in sys.argv:
            direction = 'cw'
        elif '--ccw' in sys.argv:
            direction = 'ccw'
        
        # Run calibration
        results = calibrator.calibrate_all(angles, direction)
        
        # Print summary
        print("\n" + "="*60)
        print("CALIBRATION SUMMARY")
        print("="*60)
        
        for angle, result in results.items():
            if result['scale_combined'] is not None:
                print(f"{angle}°: scale = {result['scale_combined']:.4f} (confidence: {result['confidence']:.2f})")
            else:
                print(f"{angle}°: FAILED (confidence too low)")
        
        # Generate config update
        print("\n" + "="*60)
        print("SUGGESTED CONFIG UPDATE")
        print("="*60)
        
        updated_primitives = calibrator.generate_config_update(results)
        
        for angle_key in ['30', '60', '90', '120', '150', '180']:
            if angle_key in updated_primitives and isinstance(updated_primitives[angle_key], dict):
                prim = updated_primitives[angle_key]
                print(f'"{angle_key}": {{"scale": {prim["scale"]:.4f}, "drift_x_m": {prim["drift_x_m"]:.3f}, "drift_y_m": {prim["drift_y_m"]:.3f}}}')
        
        print("\nCopy the above values to robot_config.json -> rotation_primitives")
        
    finally:
        calibrator.disconnect()


if __name__ == "__main__":
    main()
