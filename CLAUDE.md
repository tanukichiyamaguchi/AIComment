# AIComment - Development Guidelines

## Project Overview
AI-powered code comment generation with workflow orchestration.

## Build & Test
- `npm run build` — compile TypeScript
- `npm test` — run tests with Node.js test runner
- `npm run lint` — type-check without emitting

## Workflow Orchestration

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction: update tasks/lessons.md with the pattern
- Write rules that prevent the same mistake
- Review lessons at session start

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- Skip this for simple, obvious fixes

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it
- Point at logs, errors, failing tests — then resolve them

## Task Management
1. Write plan to tasks/todo.md with checkable items
2. Check in before starting implementation
3. Track progress: mark items complete as you go
4. Document results: add review section to tasks/todo.md
5. Capture lessons: update tasks/lessons.md after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible
- **No Laziness**: Find root causes. No temporary fixes
- **Minimal Impact**: Changes should only touch what's necessary
