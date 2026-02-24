# Product Guide

## Initial Concept
The "dumb pipe" infrastructure for the **Synapse** system. The project's requirements and progress are actively tracked in `/Users/kristianolsson/Documents/code/notes/projects/coding/todo-ingestion.md`.

## Vision
To build a reliable, always-on ingestion service that seamlessly bridges external communication channels (Email, Telegram) with the local intelligence of the Synapse system. It aims to empower users to capture thoughts, tasks, and data from anywhere without friction, ensuring standardized processing by the core engine.

## Core Value Proposition
-   **Frictionless Capture:** Send tasks via email or chat without opening specific apps.
-   **Unified Pipeline:** All inputs are standardized into a common format before processing.
-   **Secure & Private:** operates locally with strict whitelisting for senders.
-   **Extensible:** Modular design allows easy addition of new ingestion channels.

## Target Audience
-   **Primary User:** The developer/creator looking to automate personal workflows and knowledge management.
-   **Usage Context:** Mobile (on the go), Desktop (email clients), always-on background service.

## Key Features
-   **Multi-Channel Ingestion:**
    -   **Email:** Real-time monitoring via IMAP IDLE. Supports attachments and body text.
    -   **Telegram:** Interactive bot with long-polling. Supports text, files, and photos.
-   **Standardization Engine:**
    -   Wraps raw content in YAML metadata (Type, Sender, Timestamp, Context).
    -   Sanitizes input for consistent downstream processing.
-   **Execution Pipe:**
    -   Pluggable AI Provider abstraction (e.g., Gemini CLI, Echo).
    -   Subprocess management for secure command execution (when using CLI providers).
-   **Operational Reliability:**
    -   Rate limiting to prevent abuse.
    -   Automatic error handling and recovery.
    -   User whitelisting for security.
    -   System-level integration (macOS launchd).

## User Journey
1.  **Capture:** User forwards an email or sends a Telegram message with a task or note.
2.  **Ingest:** The service detects the message immediately.
3.  **Process:** The system validates the sender, formats the content, and invokes the Gemini CLI.
4.  **Confirm:**
    -   *Email:* Silent success (to reduce noise), replies on error.
    -   *Telegram:* Immediate confirmation of receipt or error detail.
