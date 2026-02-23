# Technical Debt

## Testing Gaps (2026-02-22)

### `services/ingestion/main.py` (0% Coverage)
- **Description:** The entry point script `main.py` is currently untested. It handles thread spawning and signal handling.
- **Impact:** Low risk as logic is simple, but integration testing could be improved.
- **Plan:** Consider an integration test that runs `main.py` in a subprocess for a short duration.

### Edge Case Handling in Listeners
- **`services/ingestion/email_listener.py` (~80% Coverage)**
    - Missing coverage for specific IMAP error conditions and flag handling edge cases.
- **`services/ingestion/telegram_listener.py` (~85% Coverage)**
    - Missing coverage for some specific logging branches and signal handling within `stop()`.

### `config.py`
- **Description:** Some configuration loading logic (lines 50, 56-66) is not fully exercised.
- **Impact:** Low.
