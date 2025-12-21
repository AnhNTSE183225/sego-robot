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
- Investigating recent hardware-odometry discrepancies where STM32 reports `TARGET_REACHED` but physical rotation is incomplete/incorrect.
- Improving perimeter validation stability and sensor settling after rotations.

