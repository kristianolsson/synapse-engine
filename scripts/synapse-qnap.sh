#!/bin/bash
# synapse-qnap.sh — QNAP/docker subcommands for the ./synapse dispatcher.
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

cmd_setup_qnap() {
    if [ -z "$SYNAPSE_HOST_DIR" ]; then
        echo "❌ SYNAPSE_HOST_DIR not set. Run: cp .env.compose.example .env, then edit it."
        exit 1
    fi

    if [ ! -d "$SYNAPSE_HOST_DIR/vault" ]; then
        echo ""
        echo "No vault found at $SYNAPSE_HOST_DIR/vault."
        # `|| true`: read returns non-zero at EOF (non-TTY stdin — the usual
        # `ssh qnap 'cd ... && ./synapse setup'` case), which under the
        # entrypoint's `set -e` would abort setup instead of taking the
        # prompt's default.
        read -rp "Clone the synapse-vault template there now? [Y/n]: " scaffold_answer || true
        if [[ ! "$scaffold_answer" =~ ^[Nn] ]]; then
            # Fail-soft, mirroring cmd_setup_mac's guarded call: a vault
            # problem must never abort the container build/start below.
            _setup_vault "$SYNAPSE_HOST_DIR/vault" _qnap_vault_clone _qnap_vault_git _qnap_vault_push \
                || echo "⚠️  Vault setup did not complete — continuing with container setup."
        else
            echo "Skipping — services will have nowhere to write until a vault exists there."
        fi
    fi

    echo "Building and starting containers..."
    (cd "$PROJECT_DIR" && docker compose build && docker compose up -d)
    echo "✅ Started. Logs: ./synapse logs"
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
