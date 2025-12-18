---
trigger: always_on
---

# Active Context

## Current Work Focus
- Robot motion calibration completed for 30°/45°/60°/90°/120°/180° rotation primitives.
- Testing and validation of calibrated parameters.

## Recent Changes (2025-12-19)
- **Implemented automatic pose/odometry reset on obstacle detection**
  - When LIDAR detects an obstacle during movement, system now automatically resets pose and odometry to (0,0,0)
  - Resets both Python pose (`_reset_pose()`) and STM32 odometry (`RESET_ODOM` command)
  - Allows user to manually re-align robot physically before sending next command
  - No user interaction required in script - simply waits for next command after reset
  - Modified `_send_move()` in `odom_only_navigator.py` (lines 1173-1187)

- **Fixed emergency stop position accuracy**
  - Changed obstacle-triggered emergency stop to use accumulated dead-reckoning progress instead of raw STM32 odometry
  - Eliminates position offset errors caused by Y-axis noise during forward movement
  - Both normal completion and emergency stop now use consistent dead-reckoning approach
  - Modified `_send_move()` in `odom_only_navigator.py` (lines 1143-1159)

- **Completed rotation primitive calibration**
  - Updated all rotation primitive scales in `robot_config.json` (30°: 1.083, 45°: 0.98, 60°: 0.956, 90°: 0.947, 120°: 0.949, 180°: 0.955)
  - Set uniform motor parameters across all angles: `min_duty_rotate=0.56`, `angular_k=0.56`
  - Adjusted odometry scale to `0.89` for better linear motion accuracy
  - Fine-tuned `move_skew=0.0075` to compensate for rightward drift in straight-line motion

- **Fixed test_rotate.py timeout issue**
  - Added STOP commands between consecutive rotation commands to clear STM32 internal state
  - Prevents timeout errors that occurred due to state carryover between commands
  - Uses 100ms cooldown after STOP (matching main navigator pattern)

## Recent Changes (2025-12-18)
- **Implemented LIDAR-based pose correction system** for POI navigation
  - Auto-detects boundary corners from polygon geometry (angle deviation >45°)
  - Detects wall segments in LIDAR scans
  - Corrects heading (threshold: 3°) and position (threshold: 5cm) when reaching POIs near corners
  - Integrated into `navigate_to()` - triggers automatically after reaching any goal near a corner (within 30cm)

## Key Learnings
- **Obstacle reset workflow**: When obstacle detected, resetting pose/odom to (0,0,0) allows clean manual re-alignment without script interaction
- **Dead-reckoning for position tracking**: Raw STM32 odometry has significant Y-axis noise during forward movement; using accumulated X-axis magnitude (dead-reckoning) prevents position offsets
- **STM32 state management**: STOP command must be sent between motion commands to prevent timeout loops
- **Calibration methodology**: Repeated rotation tests (12x 30°) reveal cumulative error patterns that single rotations miss
- **Tolerance tuning**: `rotate_tol_angle=0.087` rad (~5°) is tight for repeated rotations; cumulative error can exceed this after 10+ rotations
- **Positioning drift root cause**: Accumulated odometry errors during obstacle detours cause 10-20cm position errors at POIs
- **Solution approach**: Use LIDAR wall detection at known corners to correct pose rather than relying solely on dead reckoning

## Next Steps
- Test calibrated parameters in real navigation scenarios (not just isolated rotation tests)
- Monitor for any drift patterns in extended operation
- Consider slight tolerance increase if timeout issues persist in 12+ rotation sequences