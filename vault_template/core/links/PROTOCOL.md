# Links Module

This module governs the capture, analysis, and storage of URLs in `links/links.md`.

## Routing

### Fetch & Analyze
-   Fetch the URL content to retrieve it. Analyze for title, summary, and relevance.

### Social Media Fallback
If fetching the URL returns empty, blocked, or login-wall content (common with Threads, X/Twitter, Instagram, Reddit):
1. Retry with `curl -sL -A 'Mozilla/5.0' <URL>` to fetch raw HTML with a browser user-agent.
2. Parse the raw HTML for Open Graph meta tags (`og:title`, `og:description`) to extract the title and summary.
3. If still empty, save the link with `*[Content unavailable]*` as the summary, but **reply to the user** stating you couldn't retrieve the content and ask them to describe what it is. This overrides the `SYNAPSE_OK` response for pure vault mutations — the user needs to know.
4. **Never** hallucinate or guess a summary. If content cannot be verified, say so.

### Destination Priority
-   **Project First:** Check active projects (`projects/**/*.md`) for relevance. If a match is found, append to that project's `## Links` section.
-   **General Fallback:** Append to `links/links.md` under the current `## Month Year` and appropriate `### Category` (Coding, Finance, Home, Family, Personal).

## Formatting

-   `- [ ] [Title](URL) - *Short summary based on analysis*`

## Archival

-   When `links/links.md` exceeds ~100 lines, or at year-end, move all `[x]` items to `archive/YYYY-links.md`.
-   Preserve the original section headers in the archive file for context.
