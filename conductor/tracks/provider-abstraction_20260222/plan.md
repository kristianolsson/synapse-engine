# Implementation Plan - Track: Abstract AI Provider

## Phase 1: Core Abstraction & Gemini Migration
Establish the provider interface and migrate the existing Gemini logic without breaking functionality.

- [x] Task: Initialize `services/ingestion/providers/` package
    - [ ] Create `services/ingestion/providers/__init__.py` with factory logic.
    - [ ] Create `services/ingestion/providers/base.py` defining the `AIProvider` abstract base class.
- [x] Task: Implement `GeminiProvider`
    - [ ] Create `services/ingestion/providers/gemini.py`.
    - [ ] Move CLI execution, error parsing, and retry logic from `pipe.py` to `GeminiProvider`.
    - [ ] Create unit tests for `GeminiProvider` (migrating relevant tests from `test_pipe.py`).
- [x] Task: Update Configuration
    - [ ] Add `AI_PROVIDER` to `services/ingestion/config.py` (default: 'gemini').
    - [ ] Update `.env.example`.
- [x] Task: Integrate Provider into Pipe
    - [ ] Refactor `services/ingestion/pipe.py` to instantiate `AIProvider` via factory.
    - [ ] Update `pipe_to_gemini` (rename to `pipe_to_provider`?) to delegate to the provider.
    - [ ] Fix broken tests in `test_pipe.py` by mocking the provider instead of `subprocess`.
- [~] Task: Conductor - User Manual Verification 'Core Abstraction & Gemini Migration' (Protocol in workflow.md)

## Phase 2: Validation & Extension Proof
Verify the abstraction works by adding a dummy provider and ensuring the system is stable.

- [ ] Task: Implement `EchoProvider` (Test/Dev Provider)
    - [ ] Create `services/ingestion/providers/echo.py` (returns static text).
    - [ ] Add 'echo' to the provider factory.
- [ ] Task: Integration Testing
    - [ ] Create a new integration test that switches `AI_PROVIDER=echo` and verifies the full pipeline.
- [ ] Task: Documentation & Cleanup
    - [ ] Update `README.md` with new configuration options.
    - [ ] Ensure all new files have proper docstrings and type hints.
- [ ] Task: Conductor - User Manual Verification 'Validation & Extension Proof' (Protocol in workflow.md)
