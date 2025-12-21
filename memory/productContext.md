---
trigger: always_on
---

# Product Context

## Why this exists
- Provide a robot navigation stack that can be driven by higher-level apps/services (via Kafka) while enforcing map boundaries and avoiding obstacles using LIDAR.

## How it should work (high level)
- A map definition (boundary polygon, obstacles/restricted zones, POIs) is sent to the robot.
- The Python navigator anchors the map to the robot’s current pose so that “map coordinates” align with the robot’s odometry frame.
- The robot can then:
  - Navigate to targets/POIs while staying inside the boundary and avoiding obstacles.
  - Validate a map/perimeter by traversing the boundary loop (`perimeter_validate`) and reporting a verdict (VALID/INVALID).

## UX / Operational Expectations
- Commands should ACK deterministically (success/failure) and not hang indefinitely.
- `perimeter_validate` should traverse the boundary edges (not cut diagonals) and finish with a verdict message.