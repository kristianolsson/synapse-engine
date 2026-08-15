# Tasks Module

This module governs the management of tasks and todos in `tasks/master_todos.md`.

## Routing

-   Add action items to `tasks/master_todos.md` under the appropriate header (Home, Family, Coding, Finance, Personal, Travel, Sports, Other).
-   **CRITICAL:** Before adding to `master_todos.md`, you MUST check if the task belongs to an existing active project in the `projects/` directory. If it matches a project, you must add it to that specific project file instead (Load `projects/PROTOCOL.md` for instructions on structuring and managing project tasks).
-   Search `tasks/master_todos.md` and active project files (`projects/**/*.md`) to avoid duplicates. Be intentional — re-review files to ensure you're not creating duplicates or moving data to the wrong place.

## Formatting

-   Always use Obsidian standard: `- [ ] task description`.
-   Use `[[wikilinks]]` to connect notes (e.g., link a project task back to the `daily/` note it originated from).
-   Maintain the section headers: Personal, Travel, Sports, Home, Family, Coding, Finance, Other.
-   If Other grows large, determine if some items can be grouped logically and propose adding a new header.

## Vault Maintenance

### Master Todos
-   Ensure `tasks/master_todos.md` acts as a high-level dashboard for active projects by linking to them.
-   Periodically review `tasks/master_todos.md` against active project files (`projects/**/*.md`).
-   Ensure every major ongoing project has a corresponding "Epic" entry in `tasks/master_todos.md`.
-   Update these entries when project milestones are hit or statuses change to maintain a global overview.

### Archival
-   Completed tasks (`[x]`) remain in-place for the current year.
-   When `tasks/master_todos.md` exceeds ~100 lines, or at year-end, move all `[x]` items to `archive/YYYY-todos.md`.
-   Preserve the original section headers in the archive file for context.
