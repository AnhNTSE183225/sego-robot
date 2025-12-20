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
- **Backend area JSON initialization** - all 10 bank areas have `area_definition_json` with realistic non-square polygon boundaries and manual navigation points.
- **Interactive map with robot/POI selection** - click robots to select, click POIs to navigate, with online status validation and auto-deselect.
- **POI duplication fix** - navigation points now update in-place instead of delete+recreate, preserving IDs and preventing duplicates.


## Recent Fixes (2025-12-20 Evening)

### Interactive Map Features
- **Clickable robots and POIs**
  - Robots selectable by clicking (online robots only)
  - Selected robot shows pulsing yellow ring animation
  - POIs clickable to trigger navigation
  - Validates online status and area verification
  - Auto-deselects offline robots
  - Selected robot badge in map header with i18n

- **Enhanced robot design**
  - Modern circular design with depth effect (12px outer, 8px inner circle)
  - Enhanced directional arrow (shaft + arrowhead)
  - Fixed pulse animation offset bug (stroke-width instead of transform:scale)
  - Professional appearance with drop-shadow hover effects

- **POI duplication fix**
  - Implemented update-or-create logic instead of delete+recreate
  - Frontend preserves POI IDs when updating areas
  - Backend updates existing navigation points in-place
  - No more duplicate POIs with different IDs

- **Navigation points deletion bug fix**
  - Fixed bug where navigation points were soft-deleted when viewing/editing area
  - Root cause: Frontend sent empty array when POIs weren't loaded → Backend deleted all
  - Frontend now only sends navigationPoints when successfully loaded
  - Backend only processes navigationPoints when explicitly provided (null check)
  - Database restoration of soft-deleted navigation points completed

- **Input precision improvement**
  - Navigation point inputs now support 2 decimal places (step="0.01")
  - Applies to all coordinate inputs in AreaDefinitionDesigner

## Recent Fixes (2025-12-20 Morning)

### Frontend Map View Improvements
- **Map zoom and scroll behavior**
  - Fixed page scrolling during map zoom by using native wheel event listener with `passive: false`
  - Implemented dynamic zoom factor (0.002 per pixel) replacing fixed 10% increments for smoother scaling
  - Zoom speed now proportional to scroll amount and current zoom level

- **Dynamic zoom limits**
  - Min zoom automatically calculated to fit entire map in viewport (no more arbitrary 0.1 minimum)
  - Max zoom set to 10x min zoom for consistent relative scaling
  - Zoom limits adapt to map dimensions automatically

- **Responsive map preview**
  - Fixed preview container aspect ratio using `aspect-ratio: 1/1` instead of fixed 320px height
  - Implemented ResizeObserver to dynamically measure container size
  - MapCanvas dimensions now match actual container size
  - Preview works correctly whether sidebar is expanded or collapsed

- **Robot ID badge styling**
  - Robot IDs now display as Badge components with monospace font
  - Smaller font size (0.75rem) for better visual hierarchy
  - Removed "ID:" label and outer border for cleaner appearance
  - Increased robot card width (240px → 280px) to accommodate full UUIDs without wrapping

### Backend Area Initialization
- **Backend area JSON initialization**
  - Populated `area_definition_json` for all 10 areas in `be/brand-management/src/main/resources/data.sql`
  - Created realistic non-square polygon boundaries (L-shaped, trapezoidal, hexagonal, pentagonal, T-shaped, U-shaped)
  - Demo area set to 1.35m × 1.35m with 4 corner POI navigation points
  - Removed automatic navigation point generation to prevent duplicate POIs
  - Manually created 20 COUNTER navigation points for all branches
  - Restored 20 counters with original UUIDs maintaining 1-to-1 compatibility
  - Fixed PostgreSQL errors: invalid UUID format, missing required columns (`name`, `type`), foreign key constraint violations

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

- **Frontend build fix**
  - Removed stray top-level `@include mobile` in `fe/src/features/areas/components/MapCanvas.scss` that caused Sass declaration errors
  - Frontend `npm run build` now passes; remaining warnings are just Rollup chunk-size notices

- **Map zoom usability**
  - Map zoom now consumes wheel events and disables overscroll/touch actions on the canvas container to keep the page from scrolling while zooming
  - Changes in `MapCanvas.tsx` and `MapCanvas.scss`

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
