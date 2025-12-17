# Active Context

## Current Work Focus
- Diagnose why `perimeter_validate` (“map verification”) does not behave as intended.

## Recent Findings (from `robot.log` + code)
- Perimeter verification uses `_navigate_segment()` which issues a single `_send_move(distance)` to reach each boundary vertex.
- `_send_move()` clamps distances to `motion.max_move_command_m` (currently 1.0m), so long perimeter edges are not actually completed in one command.
- Rotations are quantized/clamped (e.g., `motion.min_rotate_deg=30` plus discrete allowed angles), which can leave large residual heading error on diagonal segments.
- Rotations frequently TIMEOUT on the STM32 side during `perimeter_validate`, preventing the routine from reaching the “VALID/INVALID” verdict path reliably.

## Next Steps
- Make `perimeter_validate` respect move/rotate clamping (split long edges into multiple steps or change semantics).
- Reduce rotation TIMEOUT rate during verification by aligning tolerances/params with observed drift during rotations.

