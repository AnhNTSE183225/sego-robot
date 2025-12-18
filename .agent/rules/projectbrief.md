---
trigger: always_on
---

# Project Brief

## Overview
- **Project**: SeGo Robot (`c:\GIT\sego-robot`)
- **Goal**: Control and navigate an omni-wheel robot using STM32 firmware + a Python “odom-only” navigator, with RPLidar obstacle sensing and Kafka telemetry/commands.

## Core Capabilities
- Execute motion primitives via STM32 serial protocol: `MOVE`, `ROTATE_DEG`, `STOP`, `RESET_ODOM`, `SET_PARAM`.
- Maintain an anchored “map frame” on the Python side to interpret map boundaries, obstacles, and POIs.
- Support Kafka-driven commands such as `navigate_to_xy`, `navigate_to_poi`, and `perimeter_validate`.

## Current Focus
- Investigating why `perimeter_validate` (“map verification”) does not reliably complete and does not accurately follow the map perimeter when motion commands are clamped (e.g., `motion.max_move_command_m=1.0`).

