---
trigger: always_on
---

# Progress

## Working
- Map ingestion/anchoring and publication of anchored status pose.
- Basic move and rotate command sequencing with LIDAR monitoring for MOVE.
- Kafka integration for command and telemetry topics.
- **Automatic pose/odometry reset on obstacle detection** - when LIDAR detects obstacle during movement, system resets to (0,0,0) for manual re-alignment.
- **LIDAR-based pose correction at boundary corners** - automatically corrects position drift (10-20cm) during POI navigation.
- **Calibrated rotation primitives** - all discrete rotation angles (30°/45°/60°/90°/120°/180°) have been calibrated for accurate execution.

## Recent Fixes (2025-12-19)
- **Automatic pose/odometry reset on obstacle detection**
  - When LIDAR detects obstacle during movement, system automatically resets pose and odometry to (0,0,0)
  - Resets both Python pose and STM32 odometry via `RESET_ODOM` command
  - Allows user to manually re-align robot before next command without script interaction
  - Modified `_send_move()` to trigger reset when `_move_stop_reason` is set

- **Emergency stop position accuracy**
  - Fixed position offset errors when robot stops due to LIDAR obstacle detection
  - Changed from using raw STM32 odometry (which has Y-axis noise) to accumulated dead-reckoning progress
  - Ensures consistent position tracking whether movement completes normally or is interrupted
  - Modified `_send_move()` to use `active_motion['progress']` on emergency stop

- **Rotation primitive calibration completed**
  - All rotation angles now have tuned scale factors for accurate rotation
  - Uniform motor parameters (`min_duty_rotate=0.56`, `angular_k=0.56`) across all angles
  - Linear motion odometry scale adjusted to `0.89`
  - Forward motion skew compensated (`move_skew=0.0075`) to eliminate rightward drift

- **test_rotate.py timeout issue fixed**
  - Added STOP commands between consecutive rotations to clear STM32 state
  - Prevents state carryover that caused timeout loops in repeated rotation tests
  - Improved from timing out at rotation 7/12 to completing 10-11/12 rotations reliably

## Recent Fixes (2025-12-18)
- **Positioning drift during obstacle renavigation**: Implemented LIDAR-based pose correction that triggers when reaching POIs near boundary corners
  - Auto-detects corners from polygon geometry
  - Uses LIDAR wall detection to measure actual position/heading
  - Applies corrections when errors exceed thresholds (3° heading, 5cm position)
  - Zero configuration - works with existing map definitions

## Known Issues
- **Cumulative rotation error in long sequences**: After 10+ consecutive rotations, cumulative error can exceed tolerance (`rotate_tol_angle=0.087` rad), causing timeout
  - Consider increasing tolerance slightly or implementing periodic odometry resets
- `perimeter_validate` does not reliably complete in practice due to rotation TIMEOUTs.
- Perimeter traversal logic assumes segments can be executed as a single MOVE; with `max_move_command_m=1.0`, it can deviate from the boundary and cut diagonals.
- Pose correction not yet integrated into perimeter verification (separate navigation path).

## What's Next
- Test calibrated rotation and motion parameters in real navigation scenarios
- Monitor straight-line motion for any remaining drift patterns
- Consider extending pose correction to perimeter verification
