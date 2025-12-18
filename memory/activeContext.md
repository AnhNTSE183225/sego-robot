---
trigger: always_on
---

# Active Context

## Current Work Focus
- LIDAR-based pose correction to address positioning drift during obstacle renavigation.

## Recent Changes (2025-12-18)
- **Implemented LIDAR-based pose correction system** for POI navigation
  - Auto-detects boundary corners from polygon geometry (angle deviation >45°)
  - Detects wall segments in LIDAR scans
  - Corrects heading (threshold: 3°) and position (threshold: 5cm) when reaching POIs near corners
  - Integrated into `navigate_to()` - triggers automatically after reaching any goal near a corner (within 30cm)

## Key Learnings
- **Positioning drift root cause**: Accumulated odometry errors during obstacle detours cause 10-20cm position errors at POIs
- **Solution approach**: Use LIDAR wall detection at known corners to correct pose rather than relying solely on dead reckoning
- **Corner detection**: No map changes needed - corners auto-detected from existing boundary polygon geometry
- **Scope**: Correction currently applies to POI navigation but NOT to perimeter verification (`_navigate_segment()` is separate path)

## Next Steps
- Test pose correction with real obstacle scenarios to validate 10-20cm drift is reduced
- Consider adding pose correction to perimeter verification for more accurate map validation
- Monitor logs for correction frequency and magnitude to tune thresholds if needed