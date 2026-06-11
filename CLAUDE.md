# AIComment - Development Guidelines

## Project Overview
じっせん君コメントシステム — 歯科セミナー実践事例 PDF への AI コメント生成パイプライン。
Google Drive（入力 PDF）→ Claude API（コメント生成）→ コメントページ結合 PDF →
Google Drive（出力）/ Google Sheets（出力一覧）/ Gmail（下書き）。
実装は Python（`src/`）、本番実行は GitHub Actions（`generate_comments.yml`）。

## 実行環境の制約（絶対遵守）

このプロジェクトの作業は **すべてクラウド環境で完結させる**。
- 開発: GitHub Codespaces
- 本番実行: GitHub Actions
- OAuth等のセットアップ: Codespaces / Cloud Shell
- ローカルPC実行は前提にしない・提案しない・要求しない

ブラウザ認証など「ローカル前提」に見える処理であっても、Codespaces 内で完結する代替手段（手動URL貼付フロー / デバイスフロー / Cloud Shell移行 等）を必ず提示すること。

## Build & Test
- `python -m pytest tests/` — run tests
- `python -m mypy src/ --ignore-missing-imports` — type-check

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
