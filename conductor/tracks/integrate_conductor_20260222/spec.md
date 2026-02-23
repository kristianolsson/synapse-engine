# Track Specification: Integrate Conductor and Verify Core Flows

## Goal
Establish a baseline of confidence by verifying that the core ingestion flows (Email, Telegram) and the execution pipe are covered by automated tests, aligning the project with the new Conductor workflow.

## Scope
-   **Module:** `services/ingestion`
-   **Flows:**
    -   Email Ingestion (IMAP IDLE -> Format -> Pipe)
    -   Telegram Ingestion (Long-polling -> Format -> Pipe)
    -   Gemini Pipe Execution

## Success Criteria
1.  `pytest` configuration is formalized.
2.  `requirements.txt` includes necessary testing tools (e.g., `pytest-cov`).
3.  Automated tests exist and pass for the core happy paths of all three flows.
4.  A coverage report confirms that these critical paths are exercised.

## Risks
-   External dependencies (IMAP, Telegram) must be mocked effectively to avoid flaky tests.
-   Legacy code might require minor refactoring to be testable.
