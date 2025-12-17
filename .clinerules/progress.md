# Progress

## Working
- Map ingestion/anchoring and publication of anchored status pose.
- Basic move and rotate command sequencing with LIDAR monitoring for MOVE.
- Kafka integration for command and telemetry topics.

## Known Issues
- `perimeter_validate` does not reliably complete in practice due to rotation TIMEOUTs.
- Perimeter traversal logic assumes segments can be executed as a single MOVE; with `max_move_command_m=1.0`, it can deviate from the boundary and cut diagonals.

## What’s Next
- Fix perimeter verification semantics (segment stepping) and tighten completion criteria so verdicts are produced deterministically.

