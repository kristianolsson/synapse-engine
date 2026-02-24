# Specification: AI Provider Abstraction

## 1. Overview
Refactor the current `services/ingestion/pipe.py` module to abstract the direct dependency on the Gemini CLI. Introduce an `AIProvider` interface and a Strategy Pattern to allow seamless switching between AI backends (e.g., Gemini, Claude, OpenAI) via configuration. The initial implementation will move the existing Gemini logic into a `GeminiProvider` and add a global `AI_PROVIDER` environment variable.

## 2. Functional Requirements

### 2.1 AIProvider Interface
-   Create an abstract base class `AIProvider` (or protocol) with the following contract:
    -   `generate_response(prompt: str, session_id: Optional[str] = None, attachments: List[str] = []) -> ProviderResult`
-   Define `ProviderResult` dataclass:
    -   `text: str`
    -   `is_error: bool`
    -   `requires_reply: bool`
    -   `stats: Optional[dict]`
    -   `session_id: Optional[str]`

### 2.2 GeminiProvider Implementation
-   Migrate all Gemini-specific logic from `pipe.py` to `services/ingestion/providers/gemini.py`:
    -   CLI command construction (`gemini --yolo ...`).
    -   JSON output parsing.
    -   Error handling (GaxiosError, QuotaError).
    -   Retry logic (Exponential backoff, fallback models).
    -   Session management logic (resume/fresh session).

### 2.3 Configuration & Factory
-   Introduce `AI_PROVIDER` environment variable (default: `gemini`).
-   Implement a factory/selector in `services/ingestion/providers/__init__.py` to instantiate the correct provider based on config.
-   Ensure `config.py` loads the new variable.

### 2.4 Pipe Module Refactor
-   Update `services/ingestion/pipe.py` to:
    -   Initialize the configured `AIProvider` once (singleton or per-request as appropriate).
    -   Delegate the actual execution to `provider.generate_response()`.
    -   Handle the `ProviderResult` and map it to the existing `PipeResult` (if different, otherwise unify).

## 3. Non-Functional Requirements
-   **Backward Compatibility:** The system must behave *exactly* as it does now when `AI_PROVIDER=gemini` is set (or defaulted).
-   **Extensibility:** Adding a new provider should only require adding a class and registering it in the factory.
-   **Logging:** Provider-specific logs should remain clear and debuggable.

## 4. Acceptance Criteria
-   [ ] `GeminiProvider` correctly handles all existing test cases (mocks updated).
-   [ ] Setting `AI_PROVIDER=gemini` in `.env` works for end-to-end flow.
-   [ ] A dummy `EchoProvider` can be configured and returns static responses (verifying the abstraction).
-   [ ] Code coverage for `services/ingestion/providers/` is >85%.

## 5. Out of Scope
-   Implementation of actual `ClaudeProvider` or `OpenAIProvider` (this is just the abstraction).
