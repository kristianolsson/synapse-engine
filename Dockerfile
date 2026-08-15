FROM node:20-slim

# System deps
RUN apt-get update && apt-get install -y \
    tini \
    git \
    openssh-client \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code and Gemini CLIs globally
RUN npm install -g @anthropic-ai/claude-code @google/gemini-cli

# Create non-root user (required for --dangerously-skip-permissions)
RUN useradd -m -u 1002 synapse

# Set up SSH config for GitHub
RUN mkdir -p /home/synapse/.ssh && \
    printf "Host github.com\n  IdentityFile /home/synapse/.ssh/id_ed25519\n  StrictHostKeyChecking no\n" \
    > /home/synapse/.ssh/config && \
    chown -R synapse:synapse /home/synapse/.ssh && \
    chmod 700 /home/synapse/.ssh && \
    chmod 600 /home/synapse/.ssh/config

# Install Python deps into a venv
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Install Playwright Firefox for E*TRADE browser-based authentication (wetrade)
# Matches options-bot pattern: fixed browser path, owned by synapse for runtime access
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && \
    /app/venv/bin/playwright install --with-deps firefox && \
    chown -R synapse:synapse /ms-playwright

WORKDIR /app/synapse-engine
USER synapse

# Install Antigravity CLI (agy)
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

# Configure git signing, identity, and safe directories
ARG GIT_USER_NAME="Synapse Bot"
ARG GIT_USER_EMAIL="synapse@localhost"
RUN git config --global gpg.format ssh && \
    git config --global user.signingkey /home/synapse/.ssh/id_ed25519.pub && \
    git config --global commit.gpgsign true && \
    git config --global user.name "${GIT_USER_NAME}" && \
    git config --global user.email "${GIT_USER_EMAIL}" && \
    git config --global --add safe.directory /app/synapse-engine && \
    git config --global --add safe.directory /app/notes

ENV PATH="/app/venv/bin:/home/synapse/.local/bin:$PATH"

ENTRYPOINT ["tini", "--"]
CMD ["python3", "-m", "services.ingestion.main"]
