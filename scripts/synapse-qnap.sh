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

# Pure check: does this diff touch a file that requires an image rebuild?
# Runs against whatever repo is CWD. Kept as a standalone function so it's
# unit-testable with a real local git repo — the container-embedded copy in
# _qnap_git_pull below duplicates this pattern as an inline string, because
# alpine/git's shell can't source this file's bash syntax. Keep both in sync
# if the file list changes.
_diff_needs_rebuild() {
    local before="$1" after="$2"
    git diff --name-only "$before" "$after" | grep -qE '^(Dockerfile|requirements\.txt)$'
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
        read -rp "Clone the synapse-vault template there now? [Y/n]: " scaffold_answer
        if [[ ! "$scaffold_answer" =~ ^[Nn] ]]; then
            _setup_vault "$SYNAPSE_HOST_DIR/vault" _qnap_vault_clone _qnap_vault_git _qnap_vault_push
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
# caller. See the _diff_needs_rebuild comment above for why the rebuild
# check is duplicated inline here instead of shared.
_qnap_git_pull() {
    local key_path="$SYNAPSE_HOST_DIR/ssh/id_ed25519"
    if [ ! -f "$key_path" ]; then
        echo "⚠️  SSH deploy key not found at $key_path — cannot update, aborting." >&2
        return 1
    fi

    # Mount $SYNAPSE_HOST_DIR at its absolute path (in prod, $PROJECT_DIR is
    # always a subdirectory of $SYNAPSE_HOST_DIR, e.g. $SYNAPSE_HOST_DIR/synapse-engine).
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
            if [ \"\$BEFORE\" != \"\$AFTER\" ] && git diff --name-only \"\$BEFORE\" \"\$AFTER\" | grep -qE '^(Dockerfile|requirements\.txt)\$'; then \
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
    chown -R synapse "$dest_dir" 2>/dev/null || true
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
    _qnap_git_in_container "$dir" "git -c user.name='$git_name' -c user.email='$git_email' $*"
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
