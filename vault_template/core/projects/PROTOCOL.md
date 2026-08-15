# Projects Module

This module governs the lifecycle of personal projects under `projects/`.

## Routing

-   Before creating a new project, search `projects/**/*.md` to check for existing matches.
-   Once a matching project is determined, identify the right place to merge in the new data (update/refine/add).
-   If not matching an existing project, create a new project file from templates in `projects/templates/`. See template instructions below.

## Project Structure

-   Project files live in their respective subfolders: `projects/<area>/` (e.g., `projects/coding/`, `projects/finance/`).
-   Use YAML frontmatter for status and area tracking:
    ```markdown
    ---
    status: active
    area: home
    created: 2026-02-07
    ---
    # Project Name
    ```

## Templates

-   New projects should be created using `projects/templates/project_template.md` as a starting point.
-   Fill in the `{{area}}`, `{{date}}`, and `{{project_name}}` placeholders.

## Archival

-   When a project is completed, move it to `projects/archive/<area>/`.
-   Update `tasks/master_todos.md` to remove or mark the project entry as complete.
-   Projects follow their existing archive path structure.
