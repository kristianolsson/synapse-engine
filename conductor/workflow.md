# Project Workflow

## Guiding Principles

1. **The Plan is the Source of Truth:** All work must be tracked in `plan.md`
2. **The Tech Stack is Deliberate:** Changes to the tech stack must be documented in `tech-stack.md` *before* implementation
3. **Test-Driven Development:** Write unit tests before implementing functionality
4. **Core Flow Coverage:** Ensure core flows are covered by tests.
5. **User Experience First:** Every decision should prioritize user experience
6. **Non-Interactive & CI-Aware:** Prefer non-interactive commands. Use `CI=true` for watch-mode tools (tests, linters) to ensure single execution.

## Task Workflow

All tasks follow a strict lifecycle:

### Standard Task Workflow

1. **Select Task:** Choose the next available task from `plan.md` in sequential order

2. **Mark In Progress:** Before beginning work, edit `plan.md` and change the task from `[ ]` to `[~]`

3. **Write Failing Tests (Red Phase):**
   - Create a new test file for the feature or bug fix.
   - Write one or more unit tests that clearly define the expected behavior and acceptance criteria for the task.
   - **CRITICAL:** Run the tests and confirm that they fail as expected. This is the "Red" phase of TDD. Do not proceed until you have failing tests.

4. **Implement to Pass Tests (Green Phase):**
   - Write the minimum amount of application code necessary to make the failing tests pass.
   - Run the test suite again and confirm that all tests now pass. This is the "Green" phase.

5. **Refactor (Optional but Recommended):**
   - With the safety of passing tests, refactor the implementation code and the test code to improve clarity, remove duplication, and enhance performance without changing the external behavior.
   - Rerun tests to ensure they still pass after refactoring.

6. **Verify Coverage:** Run coverage reports using the project's chosen tools.
   Target: Ensure core flows are covered.

7. **Document Deviations:** If implementation differs from tech stack:
   - **STOP** implementation
   - Update `tech-stack.md` with new design
   - Add dated note explaining the change
   - Resume implementation

8. **Stage Code Changes:**
   - Stage all code changes related to the task (`git add ...`).
   - **DO NOT COMMIT** yet. Commits are performed at the end of the phase.

9. **Draft Task Summary:**
   - Draft a detailed summary for the completed task (Task name, changes, "why").
   - Store this summary temporarily (e.g., in a scratchpad or append to a log file) to be used in the Phase Commit.

10. **Record Task Completion:**
    - Update `plan.md`: Change the task from `[~]` to `[x]`.
    - Note: Since there is no commit hash yet, leave that field blank or mark as "Staged".

11. **Commit Plan Update:**
    - **Action:** Stage the modified `plan.md` file.
    - **Action:** You may commit the plan update separately if needed to track progress, or stage it with the code. (Recommended: Stage it).

### Phase Completion Verification and Checkpointing Protocol

**Trigger:** This protocol is executed immediately after a task is completed that also concludes a phase in `plan.md`.

1.  **Announce Protocol Start:** Inform the user that the phase is complete and the verification and checkpointing protocol has begun.

2.  **Ensure Test Coverage for Phase Changes:**
    -   **Step 2.1: Determine Phase Scope:** Identify all staged changes.
    -   **Step 2.2: Verify and Create Tests:** For each changed code file, verify a corresponding test file exists covering core flows.
    -   If a test file is missing, you **must** create one.

3.  **Execute Automated Tests with Proactive Debugging:**
    -   Before execution, you **must** announce the exact shell command you will use to run the tests.
    -   **Example Announcement:** "I will now run the automated test suite to verify the phase. **Command:** `CI=true npm test`"
    -   Execute the announced command.
    -   If tests fail, you **must** inform the user and begin debugging.

4.  **Propose a Detailed, Actionable Manual Verification Plan:**
    -   **CRITICAL:** Generate a step-by-step plan for manual verification based on `product.md` and `plan.md`.
    -   Present the plan to the user.

5.  **Await Explicit User Feedback:**
    -   Ask: "**Does this meet your expectations? Please confirm with yes or provide feedback.**"
    -   **PAUSE** and await the user's response.

6.  **Create Phase Commit:**
    -   Stage all changes (code + plan).
    -   Perform the commit with a clear and concise message (e.g., `feat(phase): Complete Phase X - <Phase Description>`).

7.  **Attach Auditable Verification Report using Git Notes:**
    -   **Step 7.1: Draft Note Content:** Combine the drafted task summaries and the verification report (automated test command, manual verification steps, user confirmation).
    -   **Step 7.2: Attach Note:** Use `git notes add -m "<content>" <commit_hash>`.

8.  **Record Phase Checkpoint SHA:**
    -   **Step 8.1: Get Commit Hash:** Obtain the hash of the *just-created phase commit*.
    -   **Step 8.2: Update Plan:** Read `plan.md`, find the heading for the completed phase, and append `[checkpoint: <sha>]`.
    -   **Step 8.3: Write Plan:** Write the updated content back to `plan.md` and amend the commit or make a new commit for the plan update.

9.  **Announce Completion:** Inform the user that the phase is complete and committed.

### Quality Gates

Before marking any task complete, verify:

- [ ] All tests pass
- [ ] Core flows are covered by tests
- [ ] Code follows project's code style guidelines (as defined in `code_styleguides/`)
- [ ] All public functions/methods are documented
- [ ] Type safety is enforced
- [ ] No linting or static analysis errors
- [ ] Works correctly on mobile (if applicable)
- [ ] Documentation updated if needed
- [ ] No security vulnerabilities introduced

## Development Commands

**AI AGENT INSTRUCTION: This section should be adapted to the project's specific language, framework, and build tools.**

### Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Daily Development
```bash
# Run tests
python -m pytest services/ -v
# Run service manually
python -m services.ingestion.main
```

### Before Committing
```bash
# Run tests
python -m pytest
```

## Testing Requirements

### Unit Testing
- Every module must have corresponding tests.
- Use `pytest` fixtures.
- Mock external dependencies (IMAP, Telegram API).

### Integration Testing
- Test complete flows (Ingest -> Format -> Pipe).

## Code Review Process

### Self-Review Checklist
Before requesting review:

1. **Functionality**
   - Feature works as specified
   - Edge cases handled

2. **Code Quality**
   - Follows style guide
   - DRY principle applied

3. **Testing**
   - Unit tests comprehensive
   - Core flows covered

4. **Security**
   - No hardcoded secrets
   - Input validation present

## Commit Guidelines

### Message Format
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Formatting
- `refactor`: Code change
- `test`: Adding missing tests
- `chore`: Maintenance tasks

## Definition of Done

A task is complete when:

1. All code implemented to specification
2. Unit tests written and passing
3. Core flows covered by tests
4. Documentation complete (if applicable)
5. Changes staged for phase commit
6. Task summary drafted

## Emergency Procedures

### Critical Bug in Production
1. Create hotfix branch
2. Write failing test
3. Implement fix
4. Deploy
5. Document

## Deployment Workflow

### Pre-Deployment Checklist
- [ ] All tests passing
- [ ] Core flows covered
- [ ] Env vars configured

### Deployment Steps
1. Merge to main
2. Tag release
3. Deploy (launchd)
