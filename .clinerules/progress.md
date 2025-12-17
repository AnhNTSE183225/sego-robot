# Progress

## Working
- Map ingestion/anchoring and publication of anchored status pose.
- Basic move and rotate command sequencing with LIDAR monitoring for MOVE.
- Kafka integration for command and telemetry topics.
- **LIDAR-based pose correction at boundary corners** - automatically corrects position drift (10-20cm) during POI navigation.

## Recent Fixes (2025-12-18)
- **Positioning drift during obstacle renavigation**: Implemented LIDAR-based pose correction that triggers when reaching POIs near boundary corners
  - Auto-detects corners from polygon geometry
  - Uses LIDAR wall detection to measure actual position/heading
  - Applies corrections when errors exceed thresholds (3° heading, 5cm position)
  - Zero configuration - works with existing map definitions

## Known Issues
- `perimeter_validate` does not reliably complete in practice due to rotation TIMEOUTs.
- Perimeter traversal logic assumes segments can be executed as a single MOVE; with `max_move_command_m=1.0`, it can deviate from the boundary and cut diagonals.
- Pose correction not yet integrated into perimeter verification (separate navigation path).

## What's Next
- Test pose correction system with real obstacles to validate drift reduction
- Consider extending pose correction to perimeter verification
- Fix perimeter verification semantics (segment stepping) and tighten completion criteria so verdicts are produced deterministically.
