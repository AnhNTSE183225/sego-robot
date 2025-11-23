## Kafka Integration & Map Workflow

Shared-topic Kafka wiring and robot/backend behaviors for map definition, verification, and operation. All notes resolved; message shapes are minimal (no schemaVersion fields).

### Topic Model (shared)
- `robot.cmd` (backend → robots), key = `robotId`.
- `robot.telemetry` (robots → backend), key = `robotId`.
- `robot.map` (map definitions + verification verdicts), key = `robotId`.
- `robot.events` (optional fleet-wide alerts), key = `robotId`.
- Partitioning: size for fleet; per-robot ordering via keying on `robotId`.
- ACLs: robot principals restricted to their key space; backend allowed all.
- Provisioning: backend creates topics up front; robots throw if missing.

### Message Envelope
Minimal envelope; versioning handled externally (e.g., registry or topic evolution), not inside payloads.
```json
{
  "robotId": "<uuid>",
  "msgType": "CMD|ACK|STATUS|HEARTBEAT|MAP_DEF|MAP_VERDICT|EVENT",
  "correlationId": "<uuid>",
  "timestamp": 1732380000,
  "seq": 42,
  "payload": { }
}
```

### msgType meanings
- `CMD`: backend→robot command.
- `ACK`: robot acknowledges a CMD (success/fail in payload).
- `STATUS`: robot state update (pose, goal, state, errors).
- `HEARTBEAT`: lightweight liveness/presence ping; lets backend mark robots up/down.
- `MAP_DEF`: backend→robot map definition JSON.
- `MAP_VERDICT`: robot→backend result of perimeter verification (VALID/INVALID).
- `EVENT`: miscellaneous alerts/notifications.

### Map Definition (backend → robot on `robot.map`)
```json
{
  "robotId": "<uuid>",
  "msgType": "MAP_DEF",
  "correlationId": "<uuid>",
  "timestamp": 1732380000,
  "payload": {
    "mapId": "map-123",
    "frame": "local_floor_1",
    "origin": "bottom-left",
    "worldWidth": 20.0,
    "worldHeight": 10.0,
    "boundary": { "type": "polygon", "points": [ { "x":0,"y":0 }, ... ] },
    "restricted": [ { "id":"r1", "points":[...] } ],   // hard no-go
    "obstacles": [ { "id":"o1", "points":[...] } ],    // static known
    "pointsOfInterest": [
      { "id":"dock-1", "name":"Dock", "x":18, "y":1, "headingDeg":180, "category":"dock" },
      { "id":"p1", "name":"Counter", "x":5, "y":5, "category":"poi" }
    ],
    "paths": [ { "id":"patrol-1", "points":[...]} ]
  }
}
```

### Map Verdict (robot → backend on `robot.map`)
```json
{
  "robotId": "<uuid>",
  "msgType": "MAP_VERDICT",
  "correlationId": "<same-as-MAP_DEF>",
  "timestamp": 1732380500,
  "payload": {
    "mapId": "map-123",
    "status": "VALID|INVALID",
    "reason": "CLEAR|BLOCKED_PATH",
    "details": {
      "blockedAt": { "x": 9.2, "y": 4.7 }  // present only on INVALID
    }
  }
}
```

### Commands (backend → robot on `robot.cmd`)
```json
{
  "robotId": "<uuid>",
  "msgType": "CMD",
  "correlationId": "<uuid>",
  "timestamp": 1732381000,
  "payload": {
    "command": "navigate_to_poi",  // navigate_to_xy, dock, stop, pause, resume, ping, perimeter_validate
    "args": { "poiId": "p1" }
  }
}
```

### Telemetry/Status (robot → backend on `robot.telemetry`)
```json
{
  "robotId": "<uuid>",
  "msgType": "STATUS",
  "correlationId": "<cmd correlationId or heartbeat id>",
  "timestamp": 1732381010,
  "payload": {
    "pose": { "x": 4.2, "y": 1.8, "thetaDeg": 90 },
    "battery": { "soc": 0.74 },
    "currentGoal": { "type": "poi", "id": "p1" },
    "state": "MOVING|IDLE|BLOCKED|DOCKING|VERIFYING",
    "note": "blocked: moving person detected",
    "errors": []
  }
}
```

### Credentials / auth
- Reuse the existing “robot account” concept from backend/mobile if possible to avoid credential sprawl; otherwise provision one principal per robot with ACLs scoped to its key space.

### Robot Flow
1) Startup: load `ROBOT_ID` + Kafka creds; connect producer/consumer; subscribe to `robot.cmd` + `robot.map`; emit HEARTBEAT for liveness.
2) On `MAP_DEF`: validate `robotId`; store `mapId`; enter Verification mode.
3) Verification: drive a single lap along the boundary path (no detours). Use LIDAR to detect any blocking obstacle on the perimeter path. If the robot physically cannot complete the lap without rerouting, emit `MAP_VERDICT` INVALID with first blocking location; otherwise VALID. Interior obstacles not on the perimeter are ignored during verification unless they block the perimeter path.
4) Operation: handle commands (`navigate_to_poi`, `navigate_to_xy`, `dock`, `stop`, `pause`, `resume`, `ping`, `perimeter_validate`). Plan within boundary minus restricted (hard no-go; no inflation). Return-to-origin = the designated POI (program start POI).
5) Reliability: dedup via `correlationId`/`seq`; commit offsets after processing; buffer small telemetry queue when offline; emit “robot_up” on reconnect.

### Backend Flow
- Produce to shared topics keyed by `robotId`; idempotent producer, acks=all.
- Consume `robot.telemetry` for STATUS/HEARTBEAT; consume `robot.map` for verdicts.
- Publish `MAP_DEF` from UI-authored JSON; store in DB with `mapId`.
- Publish commands with `correlationId`; expect ACK/STATUS on telemetry.
- Backend initializes topics; robots fail fast if topics missing.

### Map Authoring (frontend)
- Use React + drawing tech from conversation-with-ai.txt (e.g., react-konva or similar) to build a UI that creates the JSON definition (boundary, restricted, obstacles, POIs, paths) in meters with origin bottom-left (+X right, +Y up). Save/load JSON into backend.
- Prevent self-intersecting polygons; ensure restricted/obstacles are inside boundary.

### Verification Criteria
- Single lap along the boundary path only.
- VALID if the robot can physically complete the perimeter lap without rerouting.
- INVALID if any obstacle blocks the perimeter path (as seen by LIDAR) such that the lap cannot be completed.
- No numeric tolerances/radii beyond physical feasibility; restricted areas are hard no-go.

### Failure Handling
- INVALID map: remain in Verification mode; await new `MAP_DEF` or `perimeter_validate` command.
- Blocked in operation: send STATUS `state=BLOCKED`; attempt local avoidance for moving people; if still blocked, NACK the command.
- Kafka down: continue current safe motion; buffer minimal telemetry; drop oldest on overflow; send “robot_up” on reconnect.

### Kafka Integration (KRaft)
- Deploy Kafka in KRaft mode (no Zookeeper); configure TLS/SASL as chosen; expose bootstrap servers.
- Create topics `robot.cmd`, `robot.telemetry`, `robot.map`, `robot.events` with partitions sized for fleet.
- Set retention and compaction per topic (e.g., commands short retention, telemetry moderate).
- Robots require topics to exist; otherwise they raise an exception at startup.

### Clarifications Incorporated
- No schemaVersion in payloads; minimal JSON.
- `msgType` purposes are defined above.
- Heartbeat purpose clarified (liveness).
- Restricted areas are hard no-go without inflation.
- Verification is perimeter-only, one lap, invalid if blocked; no detours in verification.
- Return-to-origin = POI (start POI).
- Commands include `perimeter_validate`.
- Localization: odometry-only frame anchored at origin bottom-left; no SLAM/ROS/Fiducials. Risk: drift over longer runs; consider reviewing STM32 odom code for drift characteristics when integrating.
