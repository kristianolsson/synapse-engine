# Implementation Plan - Integrate Conductor and Verify Core Flows

## Phase 1: Analysis & Configuration
- [~] Task: specific - Analyze existing tests in `services/ingestion/tests/` to identify gaps in core flow coverage.
    - [ ] Subtask: Run existing tests and capture output.
    - [ ] Subtask: Review test files against `services/ingestion` source code.
- [x] Task: specific - Update `requirements.txt` to include `pytest-cov` and install dependencies.
- [x] Task: specific - Create or update `pytest.ini` to configure test discovery and coverage reporting.
- [ ] Task: Conductor - User Manual Verification 'Analysis & Configuration' (Protocol in workflow.md)

## Phase 2: Core Flow Verification
- [ ] Task: specific - Verify and/or Implement tests for Email Ingestion (`email_listener.py`).
    - [ ] Subtask: Write unit test for `EmailListener` mocking IMAP client.
    - [ ] Subtask: Verify message parsing and standardizer invocation.
- [ ] Task: specific - Verify and/or Implement tests for Telegram Ingestion (`telegram_listener.py`).
    - [ ] Subtask: Write unit test for `TelegramBot` mocking Telegram API.
    - [ ] Subtask: Verify message handling and standardizer invocation.
- [ ] Task: specific - Verify and/or Implement tests for Execution Pipe (`pipe.py`).
    - [ ] Subtask: Write unit test for `GeminiPipe` mocking subprocess execution.
    - [ ] Subtask: Verify correct command construction and error handling.
- [ ] Task: Conductor - User Manual Verification 'Core Flow Verification' (Protocol in workflow.md)

## Phase 3: Integration & Baseline
- [ ] Task: specific - Run full test suite with coverage report.
- [ ] Task: specific - specific - Document any known issues or technical debt discovered during verification in `conductor/technical_debt.md` (create if needed).
- [ ] Task: Conductor - User Manual Verification 'Integration & Baseline' (Protocol in workflow.md)
