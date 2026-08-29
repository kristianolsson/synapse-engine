#!/bin/bash
# synapse-qnap.sh — QNAP/docker subcommands for the ./synapse.sh dispatcher.
#
# QNAP has no usable host git, so every git operation against the repo or
# the vault runs inside a throwaway `alpine/git` container. Two separate
# env files matter here: COMPOSE_ENV_FILE (this repo checkout's own .env —
# read automatically by `docker compose` for build-arg substitution, and by
# us for SYNAPSE_HOST_DIR/GIT_USER_NAME/GIT_USER_EMAIL, per
# .env.compose.example) and APP_ENV_FILE ($SYNAPSE_HOST_DIR/.env — mounted
# into the running container as its runtime environment, per
# docker-compose.yml's env_file: directive).

COMPOSE_ENV_FILE="$PROJECT_DIR/.env"
SYNAPSE_HOST_DIR="$(_env_var "$COMPOSE_ENV_FILE" SYNAPSE_HOST_DIR)"
APP_ENV_FILE="${SYNAPSE_HOST_DIR:+$SYNAPSE_HOST_DIR/.env}"

# Single source of truth for "which changed files force an image rebuild".
# Used both by _diff_needs_rebuild below (the unit-tested host-side check)
# and, interpolated, by the container-side check embedded in _qnap_git_pull
# — alpine/git's /bin/sh can't source this bash file, so the check has to be
# passed in as a string, but the pattern itself is no longer duplicated.
_REBUILD_TRIGGER_REGEX='^(Dockerfile|requirements\.txt)$'

# Pure check: does this diff touch a file that requires an image rebuild?
# Runs against whatever repo is CWD. Kept as a standalone function so it's
# unit-testable with a real local git repo.
_diff_needs_rebuild() {
    local before="$1" after="$2"
    git diff --name-only "$before" "$after" | grep -qE "$_REBUILD_TRIGGER_REGEX"
}

cmd_start_qnap() {
    (cd "$PROJECT_DIR" && docker compose up -d)
}

cmd_stop_qnap() {
    (cd "$PROJECT_DIR" && docker compose down)
}

cmd_restart_qnap() {
    (cd "$PROJECT_DIR" && docker compose restart)
}

cmd_logs_qnap() {
    (cd "$PROJECT_DIR" && docker compose logs -f)
}

# Creates the credential/data directories docker-compose.yml bind-mounts,
# and re-chowns them to synapse — cheap and idempotent, so safe to run on
# every setup even once real credentials/browser profiles live under them
# (only ownership changes, never permissions or content). Covers dirs a
# still-manual qnap-setup.md step scp's or docker-cp's into before this
# runs, in addition to creating them fresh on a brand new host.
_ensure_qnap_host_dirs() {
    mkdir -p "$SYNAPSE_HOST_DIR"/credentials/claude \
        "$SYNAPSE_HOST_DIR"/credentials/gemini \
        "$SYNAPSE_HOST_DIR"/credentials/etrade \
        "$SYNAPSE_HOST_DIR"/credentials/amazon \
        "$SYNAPSE_HOST_DIR"/data
    chown -R synapse "$SYNAPSE_HOST_DIR"/credentials "$SYNAPSE_HOST_DIR"/data \
        || echo "⚠️  Could not chown credentials/data dirs to synapse."
}

# These five keys are fixed container-internal paths (docker-compose.yml's
# bind mounts and the Dockerfile's install locations) — never derived from
# anything host-specific, so there's nothing for qnap-setup.md to ask the
# user for. Uses _set_env_var (update-in-place-or-append) rather than the
# old doc's mix of `sed` and `echo >>`, so re-running setup can't leave
# duplicate lines the way a second `echo >>` would have.
_ensure_qnap_runtime_env_defaults() {
    local env_file="$1"
    _set_env_var "$env_file" VAULT_PATH "/app/vault"
    _set_env_var "$env_file" CLAUDE_CMD "/usr/local/bin/claude"
    _set_env_var "$env_file" AGY_CMD "/home/synapse/.local/bin/agy"
    _set_env_var "$env_file" SESSION_STORAGE_PATH "/app/data/sessions.json"
    _set_env_var "$env_file" REMINDERS_JSON_PATH "/app/vault/reminders/reminders.json"
}

# TZ is optional in the compose-local .env — docker-compose.yml's
# `TZ=${TZ:-UTC}` falls back to UTC on its own. Prompted here (once — skips
# if already set) so a fresh setup doesn't silently leave logs/reminder
# scheduling on UTC without ever asking, the same way the vault-clone
# prompt above asks rather than silently skipping.
_ensure_qnap_timezone() {
    if [ -n "$(_env_var "$COMPOSE_ENV_FILE" TZ)" ]; then
        return
    fi
    # `|| true`: read returns non-zero at EOF (non-TTY stdin), same reason
    # as the vault-clone prompts below — must not abort setup under `set -e`.
    read -rp "Container timezone (IANA name, e.g. America/Los_Angeles; blank for UTC): " tz_answer || true
    if [ -n "$tz_answer" ]; then
        _set_env_var "$COMPOSE_ENV_FILE" TZ "$tz_answer"
        echo "✅ TZ=$tz_answer set in $COMPOSE_ENV_FILE"
    fi
}

cmd_setup_qnap() {
    if [ -z "$SYNAPSE_HOST_DIR" ]; then
        echo "❌ SYNAPSE_HOST_DIR not set. Run: cp .env.compose.example .env, then edit it."
        exit 1
    fi

    if [ ! -f "$APP_ENV_FILE" ]; then
        echo "❌ Runtime .env not found at $APP_ENV_FILE. Copy your app's .env there first (see qnap-setup.md), then re-run setup."
        exit 1
    fi

    _ensure_qnap_host_dirs
    _ensure_qnap_runtime_env_defaults "$APP_ENV_FILE"
    _ensure_qnap_timezone

    if [ ! -d "$SYNAPSE_HOST_DIR/vault" ]; then
        echo ""
        echo "No vault found at $SYNAPSE_HOST_DIR/vault."
        # `|| true`: read returns non-zero at EOF (non-TTY stdin — the usual
        # `ssh qnap 'cd ... && ./synapse.sh setup'` case), which under the
        # entrypoint's `set -e` would abort setup instead of taking the
        # prompt's default.
        read -rp "Clone the synapse-vault template there now? [Y/n]: " scaffold_answer || true
        if [[ ! "$scaffold_answer" =~ ^[Nn] ]]; then
            # Fail-soft, mirroring cmd_setup_mac's guarded call: a vault
            # problem must never abort the container build/start below.
            _setup_vault "$SYNAPSE_HOST_DIR/vault" _qnap_vault_clone _qnap_vault_git _qnap_vault_push \
                || echo "⚠️  Vault setup did not complete — continuing with container setup."
        else
            # QNAP has no host git, so cloning your OWN existing vault repo
            # needs the same throwaway-container dance as the template
            # clone above — offer to do that too, instead of leaving you to
            # work out the docker/alpine-git incantation yourself.
            read -rp "Clone your own existing vault repo instead? Git URL (blank to skip): " own_vault_url || true
            if [ -n "$own_vault_url" ]; then
                _qnap_vault_clone_own "$SYNAPSE_HOST_DIR/vault" "$own_vault_url" \
                    || echo "⚠️  Vault clone did not complete — continuing with container setup."
            else
                echo "Skipping — services will have nowhere to write until a vault exists there."
            fi
        fi
    fi

    echo "Building and starting containers..."
    (cd "$PROJECT_DIR" && docker compose build && docker compose up -d)
    echo "✅ Started. Logs: ./synapse.sh logs"
}

# Runs a script inside a throwaway alpine/git container against dir, mirrors
# the host having no git. Restores ownership to the synapse user afterward
# (the container runs as root, so files it touches end up root-owned).
_qnap_git_in_container() {
    local dir="$1" script="$2"
    local rc=0
    docker run --rm --entrypoint sh \
        -v "$dir:$dir" \
        alpine/git \
        -c "git config --global --add safe.directory '$dir' && cd '$dir' && $script" || rc=$?
    chown -R synapse "$dir" || echo "⚠️  Could not restore ownership of $dir to synapse."
    return $rc
}

# Same as above, with the SSH deploy key mounted, for operations that reach
# a remote. Refuses to run (rather than mounting a not-yet-provisioned
# path) when the key is absent — mounting a missing host path makes Docker
# create a directory there instead, which then poisons every future mount
# of that path.
_qnap_git_in_container_with_ssh() {
    local dir="$1" script="$2"
    local key_path="$SYNAPSE_HOST_DIR/ssh/id_ed25519"
    if [ ! -f "$key_path" ]; then
        echo "⚠️  SSH deploy key not found at $key_path — cannot reach the remote."
        return 1
    fi
    local rc=0
    docker run --rm --entrypoint sh \
        -v "$dir:$dir" \
        -v "$key_path:/root/.ssh/id_ed25519" \
        alpine/git \
        -c "chmod 600 /root/.ssh/id_ed25519 && git config --global --add safe.directory '$dir' && cd '$dir' && $script" || rc=$?
    chown -R synapse "$dir" || echo "⚠️  Could not restore ownership of $dir to synapse."
    return $rc
}

# Pulls PROJECT_DIR's own git history (the synapse-engine checkout itself,
# not the vault) and reports whether a rebuild is needed. Sets
# QNAP_PULL_BEFORE_SHA / QNAP_PULL_AFTER_SHA / QNAP_REBUILD_NEEDED for the
# caller. The rebuild check runs inside the container (it needs the pulled
# repo's git), but its pattern comes from $_REBUILD_TRIGGER_REGEX — see the
# comment at the grep line for how it is quoted into the script string.
_qnap_git_pull() {
    local key_path="$SYNAPSE_HOST_DIR/ssh/id_ed25519"
    if [ ! -f "$key_path" ]; then
        echo "⚠️  SSH deploy key not found at $key_path — cannot update, aborting." >&2
        return 1
    fi

    # Mount $SYNAPSE_HOST_DIR at its absolute path (in prod, $PROJECT_DIR is
    # always a subdirectory of $SYNAPSE_HOST_DIR, e.g. $SYNAPSE_HOST_DIR/synapse-engine).
    #
    # Quoting note for the grep below: the -c argument is one double-quoted
    # bash string, so $(_shell_quote "$_REBUILD_TRIGGER_REGEX") expands at
    # build time into the literal text '^(Dockerfile|requirements\.txt)$'
    # — surrounding single quotes included, any embedded single quote
    # escaped as '\''. The container's /bin/sh then re-parses that and hands
    # grep -qE exactly one argument: ^(Dockerfile|requirements\.txt)$
    # (single quotes keep sh from expanding the trailing $ or globbing).
    local pull_output rc=0
    pull_output="$(docker run --rm --entrypoint sh \
        -v "$SYNAPSE_HOST_DIR:$SYNAPSE_HOST_DIR" \
        -v "$key_path:/root/.ssh/id_ed25519" \
        alpine/git \
        -c "chmod 600 /root/.ssh/id_ed25519 && \
            git config --global --add safe.directory $PROJECT_DIR && \
            cd $PROJECT_DIR && \
            BEFORE=\$(git rev-parse HEAD) && \
            GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git pull && \
            AFTER=\$(git rev-parse HEAD) && \
            echo \"SYNAPSE_BEFORE_SHA=\$BEFORE\" && \
            echo \"SYNAPSE_AFTER_SHA=\$AFTER\" && \
            if [ \"\$BEFORE\" != \"\$AFTER\" ] && git diff --name-only \"\$BEFORE\" \"\$AFTER\" | grep -qE $(_shell_quote "$_REBUILD_TRIGGER_REGEX"); then \
                echo SYNAPSE_REBUILD_NEEDED=1; \
            else \
                echo SYNAPSE_REBUILD_NEEDED=0; \
            fi")" || rc=$?

    chown -R synapse "$PROJECT_DIR" || echo "⚠️  Could not restore ownership of $PROJECT_DIR to synapse."

    if [ "$rc" -ne 0 ]; then
        echo "⚠️  git pull failed inside the container (exit $rc)." >&2
        return "$rc"
    fi

    QNAP_PULL_BEFORE_SHA="$(printf '%s\n' "$pull_output" | sed -n 's/^SYNAPSE_BEFORE_SHA=//p')"
    QNAP_PULL_AFTER_SHA="$(printf '%s\n' "$pull_output" | sed -n 's/^SYNAPSE_AFTER_SHA=//p')"
    QNAP_REBUILD_NEEDED="$(printf '%s\n' "$pull_output" | sed -n 's/^SYNAPSE_REBUILD_NEEDED=//p')"
}

# POSIX-safe single-quoting for building a command string that a shell
# (possibly not bash — the alpine/git container's /bin/sh) will re-parse.
# Wraps in '...', escaping any embedded single quote as '\''.
_shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Clone target doesn't exist yet, so it can't be bind-mounted directly —
# mount its parent instead and clone into a subdirectory by name.
_qnap_vault_clone() {
    local dest_dir="$1"
    local parent_dir name rc=0
    parent_dir="$(dirname "$dest_dir")"
    name="$(basename "$dest_dir")"
    docker run --rm --entrypoint sh \
        -v "$parent_dir:$parent_dir" \
        alpine/git \
        -c "cd '$parent_dir' && git clone '$SYNAPSE_VAULT_TEMPLATE_URL' '$name'" || rc=$?
    chown -R synapse "$dest_dir" || echo "⚠️  Could not restore ownership of $dest_dir to synapse."
    return $rc
}

# Clones the user's OWN existing vault repo — unlike _qnap_vault_clone
# (always $SYNAPSE_VAULT_TEMPLATE_URL over public HTTPS, no auth needed),
# an existing personal vault is likely private, so this mounts the same SSH
# deploy key used for synapse-engine itself. Cloned as-is: history and the
# origin remote both stay intact (no detach/recommit — it's already theirs).
_qnap_vault_clone_own() {
    local dest_dir="$1" url="$2"
    if [ -e "$dest_dir" ]; then
        echo "❌ $dest_dir already exists — refusing to overwrite. Remove it or choose a different path."
        return 1
    fi
    local key_path="$SYNAPSE_HOST_DIR/ssh/id_ed25519"
    if [ ! -f "$key_path" ]; then
        echo "⚠️  SSH deploy key not found at $key_path — cannot clone a private repo. If your vault is public, use an https:// URL; otherwise set up the key first (qnap-setup.md step 3)."
        return 1
    fi
    local parent_dir name rc=0
    parent_dir="$(dirname "$dest_dir")"
    name="$(basename "$dest_dir")"
    docker run --rm --entrypoint sh \
        -v "$parent_dir:$parent_dir" \
        -v "$key_path:/root/.ssh/id_ed25519" \
        alpine/git \
        -c "chmod 600 /root/.ssh/id_ed25519 && cd '$parent_dir' && GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git clone '$url' '$name'" || rc=$?
    chown -R synapse "$dest_dir" || echo "⚠️  Could not restore ownership of $dest_dir to synapse."
    if [ "$rc" -eq 0 ]; then
        echo "✅ Vault cloned from $url into $dest_dir."
    fi
    return $rc
}

_qnap_vault_git() {
    local dir="$1"
    shift
    # The throwaway container has no ~/.gitconfig, so `git commit` would
    # otherwise fail with "please tell me who you are" — inject an identity
    # on every invocation via -c (harmless for non-commit subcommands too).
    # Same default fallback docker-compose.yml itself uses for the build args.
    local git_name git_email
    git_name="$(_env_var "$COMPOSE_ENV_FILE" GIT_USER_NAME)"
    git_name="${git_name:-Synapse Bot}"
    git_email="$(_env_var "$COMPOSE_ENV_FILE" GIT_USER_EMAIL)"
    git_email="${git_email:-synapse@localhost}"
    # Build the container's command string with each arg individually
    # single-quoted (POSIX-safe, since the container's shell may not be
    # bash) — "$*" would flatten multi-word args like a commit message
    # into bare words, silently turning "-m Initial commit from..." into
    # a -m message of just "Initial" plus four bogus pathspec arguments.
    local quoted_args="" arg
    for arg in "$@"; do
        quoted_args="$quoted_args $(_shell_quote "$arg")"
    done
    _qnap_git_in_container "$dir" "git -c user.name=$(_shell_quote "$git_name") -c user.email=$(_shell_quote "$git_email")$quoted_args"
}

_qnap_vault_push() {
    local dir="$1"
    _qnap_git_in_container_with_ssh "$dir" \
        "GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git push -u origin HEAD"
}

cmd_update_qnap() {
    echo "Pulling latest code..."
    if ! _qnap_git_pull; then
        echo "❌ Update failed — could not pull latest code." >&2
        return 1
    fi

    if [[ "$QNAP_PULL_BEFORE_SHA" == "$QNAP_PULL_AFTER_SHA" ]]; then
        echo "Already up to date."
        return
    fi

    if [[ "$QNAP_REBUILD_NEEDED" == "1" ]]; then
        echo "Dockerfile or requirements.txt changed — rebuilding image..."
        (cd "$PROJECT_DIR" && docker compose build)
    else
        echo "Code-only change — no rebuild needed."
    fi

    echo "Restarting..."
    (cd "$PROJECT_DIR" && docker compose down && docker compose up -d && docker compose logs --tail=20)
}
