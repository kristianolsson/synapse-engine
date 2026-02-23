# Implementation Plan - Integrate Conductor and Verify Core Flows

## Phase 1: Analysis & Configuration [checkpoint: 21831e6]
- [x] Task: specific - Analyze existing tests in `services/ingestion/tests/` to identify gaps in core flow coverage.
    - [x] Subtask: Run existing tests and capture output.
    - [x] Subtask: Review test files against `services/ingestion` source code.
- [x] Task: specific - Update `requirements.txt` to include `pytest-cov` and install dependencies.
- [x] Task: specific - Create or update `pytest.ini` to configure test discovery and coverage reporting.
- [x] Task: Conductor - User Manual Verification 'Analysis & Configuration' (Protocol in workflow.md)

## Phase 2: Core Flow Verification [checkpoint: 578e70b]
- [x] Task: specific - Verify and/or Implement tests for Email Ingestion (`email_listener.py`).
    - [x] Subtask: Write unit test for `EmailListener` mocking IMAP client.
    - [x] Subtask: Verify message parsing and standardizer invocation.
- [x] Task: specific - Verify and/or Implement tests for Telegram Ingestion (`telegram_listener.py`).
    - [x] Subtask: Write unit test for `TelegramBot` mocking Telegram API.
    - [x] Subtask: Verify message handling and standardizer invocation.
- [x] Task: specific - Verify and/or Implement tests for Execution Pipe (`pipe.py`).
    - [x] Subtask: Write unit test for `GeminiPipe` mocking subprocess execution.
    - [x] Subtask: Verify correct command construction and error handling.
- [x] Task: Conductor - User Manual Verification 'Core Flow Verification' (Protocol in workflow.md)

## Phase 3: Integration & Baseline [checkpoint: 3172d92]
- [x] Task: specific - Run full test suite with coverage report.
- [x] Task: specific - specific - Document any known issues or technical debt discovered during verification in `conductor/technical_debt.md` (create if needed).
- [x] Task: Conductor - User Manual Verification 'Integration & Baseline' (Protocol in workflow.md)
