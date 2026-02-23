# Product Guidelines

## Documentation
-   **Style:** Concise & Technical. Prioritize clarity and brevity over narrative.
-   **Format:** Markdown with clear headers and code blocks.
-   **Maintenance:** Keep `README.md` and inline comments up-to-date with code changes.

## Engineering Principles
-   **Architecture:** Modular Services. Each ingestion channel operates independently. Maintain clear separation of concerns.
-   **Error Handling:** Silent & Recover. The system should be robust and self-healing. Avoid notifying the user for transient errors; retry silently. Only alert on critical failures.
-   **Logging:** Operational. Log only significant events (start/stop, successful ingestion, errors). Avoid debug noise in production.

## Security
-   **Access Control:** Strict whitelisting of senders (Email) and user IDs (Telegram).
-   **Data Privacy:** Process data locally. Minimize external API calls.

## Testing
-   **Strategy:** Automated testing using `pytest`.
-   **Coverage:** Focus on core logic and integration points.
