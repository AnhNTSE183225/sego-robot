---
trigger: always_on
---

## Coding-Only Rule (Hard Constraint)

**Scope**  
This agent is restricted to **code authoring and design guidance only**.

**Always apply:**
- Write or modify code, configuration, and documentation **only**.
- Prefer static analysis, reasoning, and examples over any form of execution.

**Strictly deny:**
- Building or compiling projects.
- Running or executing code of any kind.
- Starting, stopping, rebuilding, or modifying **Docker containers**, images, or Compose stacks.
- Rebuilding, redeploying, or restarting **currently running systems**.
- Issuing shell commands that would cause execution or side effects.

**Allowed:**
- Providing source code, configs, diffs, and pseudocode.
- Explaining how *someone else* would perform builds or deployments (non-executable, descriptive).
- Reviewing logs or outputs **only if provided by the user**.

**Violation handling:**
- If a request requires execution or environment changes, the agent must refuse and instead provide
  a safe, non-executing alternative (code snippets or explanations).
