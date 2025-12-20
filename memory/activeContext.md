---
trigger: always_on
---

# Active Context

## Current Work Focus
- Interactive map features with robot/POI selection completed
- POI duplication issue fixed with update-or-create logic
- Robot visual design enhanced with modern circular appearance
- Navigation point input precision improved to 2 decimal places


## Recent Changes (2025-12-20 Evening)

### Interactive Map Features
- **Implemented clickable robots and POIs on map**
  - Robots can be selected by clicking on the map (only online robots)
  - Selected robot displays with pulsing yellow ring animation
  - POIs can be clicked to trigger robot navigation
  - Validates robot online status and area verification before navigation
  - Auto-deselects robot if it goes offline
  - Added "Selected Robot: [name]" badge in map header with proper i18n support

- **Enhanced robot visual design**
  - Replaced simple diamond shape with modern circular design
  - Outer circle (12px radius) with inner circle (8px) for depth effect
  - Enhanced directional arrow with shaft and arrowhead
  - Better visual hierarchy and professional appearance
  - Improved hover effects with drop-shadow
  - Fixed pulse ring animation offset bug (changed from transform:scale to stroke-width animation)

- **Fixed POI duplication issue**
  - Root cause: Backend was deleting all navigation points and recreating them with new IDs on every area update
  - Solution: Implemented update-or-create logic to preserve existing POI IDs
  - Frontend changes: Include `poi.id` in navigationPoints when updating areas (`AreaEditPage.tsx`, `AreaEditModal.tsx`)
  - Backend changes: Added `id` field to `NavigationPointInlineRequest` DTO, updated `AreaServiceImpl` to update existing points instead of delete+recreate
  - Result: No more duplicate POIs, existing IDs preserved, efficient updates

- **Fixed navigation points deletion bug**
  - Root cause: Navigation points were soft-deleted when viewing/editing area without changes
  - Bug chain: Soft-deleted POIs → API returns `[]` → Frontend skips loading → Sends `[]` to backend → Backend deletes all → Cycle repeats
  - Frontend fix: Only include `navigationPoints` field when they were successfully loaded (`AreaEditPage.tsx` lines 123-140)
  - Backend fix: Only process navigation points when explicitly provided (null check with logging in `AreaServiceImpl.java` lines 103-167)
  - Database fix: Restored soft-deleted navigation points with `UPDATE navigation_points SET deleted_at = NULL`
  - Result: Navigation points no longer deleted when viewing/editing area metadata

- **Improved navigation point input precision**
  - Changed `step` attribute from `0.1` to `0.01` in all number inputs in `AreaDefinitionDesigner.tsx`
  - Allows 2 decimal place precision for coordinates (e.g., 1.35, 0.25)
  - Affects width, height, boundary points, obstacle points, and POI coordinates

### Files Modified
- Frontend: `MapCanvas.tsx`, `MapCanvas.scss`, `AreaDetailPage.tsx`, `Area.scss`, `AreaEditPage.tsx`, `AreaEditModal.tsx`, `AreaDefinitionDesigner.tsx`
- Backend: `CreateAreaRequest.java`, `AreaServiceImpl.java`
- Translations: `vi/area.json`, `en/area.json`
- Database: Manual restoration of soft-deleted navigation points

## Recent Changes (2025-12-20 Morning)

### Frontend Map View Improvements
- **Fixed map zoom behavior**
  - Prevented page scroll during map zoom by implementing native wheel event listener with `passive: false`
  - Replaced fixed 10% zoom increments with dynamic zoom factor (0.002 per pixel) for smooth, proportional scaling
  - Converted `handleWheel` to `useCallback` for better performance

- **Implemented dynamic zoom limits**
  - Removed hardcoded MIN_SCALE (0.1) and MAX_SCALE (5.0) constants
  - Min zoom now dynamically calculated to fit entire map in viewport
  - Max zoom set to 10x min zoom for relative scaling
  - Zoom limits adapt automatically to map dimensions

- **Fixed map preview aspect ratio issues**
  - Removed fixed height constraint (320px) from preview container
  - Implemented `aspect-ratio: 1/1` with `max-height: 600px` for responsive container
  - Added ResizeObserver to dynamically measure container size
  - MapCanvas now receives actual container dimensions, working correctly with sidebar expanded/collapsed states

- **Styled robot ID as badge**
  - Changed robot ID display from plain text to Badge component with `secondary` variant
  - Applied monospace font (`JetBrains Mono`) at smaller size (0.75rem)
  - Removed "ID:" label and outer border for cleaner appearance
  - Increased robot card minimum width from 240px to 280px to accommodate full IDs
  - Added `white-space: nowrap` to prevent ID text wrapping

### Backend Area Initialization
- **Initialized area_definition_json for all bank areas**
  - Added SQL UPDATE statements in `be/brand-management/src/main/resources/data.sql` to populate `area_definition_json` for 10 areas
  - Created realistic non-square polygon boundaries: L-shaped, trapezoidal, hexagonal, pentagonal, T-shaped, and U-shaped areas
  - Demo area (aaaa1111-1111-1111-1111-111111111101) set to 1.35m × 1.35m with 4 corner POI navigation points
  - Removed automatic navigation point generation that was creating duplicate POIs
  - Manually created 20 COUNTER navigation points for all branches (Hanoi, HCMC, Da Nang Floor 1 + Hanoi/HCMC Floor 2 VIP)
  - Restored 20 counters with original UUIDs and configurations, referencing new manual navigation points

- **Fixed multiple PostgreSQL initialization errors**
  - Fixed invalid UUID format (changed from 7-4-4-4-12 to proper 8-4-4-4-12 format)
  - Added missing required columns: `name` and `type` (POI/COUNTER) to navigation_points table
  - Removed unsafe DELETE statement that violated foreign key constraints

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

- **Frontend build fix**
  - Removed stray top-level `@include mobile` block in `fe/src/features/areas/components/MapCanvas.scss` that caused Sass "Declarations may only be used within style rules" errors
  - `npm run build` now succeeds; only Rollup chunk-size warnings remain

- **Map zoom usability**
  - Prevented page scroll while zooming map with mouse wheel by stopping event propagation, capturing wheel on container, and disabling overscroll/touch actions in `MapCanvas`

## Recent Changes (2025-12-18)
- **Implemented LIDAR-based pose correction system** for POI navigation
  - Auto-detects boundary corners from polygon geometry (angle deviation >45°)
  - Detects wall segments in LIDAR scans
  - Corrects heading (threshold: 3°) and position (threshold: 5cm) when reaching POIs near corners
  - Integrated into `navigate_to()` - triggers automatically after reaching any goal near a corner (within 30cm)

## Key Learnings
- **Frontend responsive design patterns**:
  - ResizeObserver is essential for components that need to adapt to dynamic container sizes (e.g., sidebar collapse/expand)
  - Native event listeners with `passive: false` required to prevent default browser behaviors like page scroll
  - Dynamic calculations (zoom limits based on content) provide better UX than hardcoded constants
  - aspect-ratio CSS property better than fixed heights for maintaining proportions across screen sizes

- **POI management patterns**:
  - Update-or-create pattern prevents duplicates and preserves references
  - Always include IDs when updating entities to enable in-place updates
  - Backend should check for ID existence before deciding to update vs create
  - Logging is crucial for debugging entity lifecycle (create/update/delete)

- **Obstacle reset workflow**: When obstacle detected, resetting pose/odom to (0,0,0) allows clean manual re-alignment without script interaction
- **Dead-reckoning for position tracking**: Raw STM32 odometry has significant Y-axis noise during forward movement; using accumulated X-axis magnitude (dead-reckoning) prevents position offsets
- **STM32 state management**: STOP command must be sent between motion commands to prevent timeout loops
- **Calibration methodology**: Repeated rotation tests (12x 30°) reveal cumulative error patterns that single rotations miss
- **Tolerance tuning**: `rotate_tol_angle=0.087` rad (~5°) is tight for repeated rotations; cumulative error can exceed this after 10+ rotations
- **Positioning drift root cause**: Accumulated odometry errors during obstacle detours cause 10-20cm position errors at POIs
- **Solution approach**: Use LIDAR wall detection at known corners to correct pose rather than relying solely on dead reckoning

## Next Steps
- Test POI duplication fix in development environment
- Monitor for any issues with navigation point updates
- Test interactive map features with real robot telemetry
- Consider cleaning up existing duplicate POIs in database
