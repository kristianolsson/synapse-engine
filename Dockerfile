FROM node:20-slim

# System deps
RUN apt-get update && apt-get install -y \
    git \
    openssh-client \
    python3 \
    python3-pip \
    python3-venv \
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

WORKDIR /app/synapse-engine
USER synapse

# Configure git signing and identity
RUN git config --global gpg.format ssh && \
    git config --global user.signingkey /home/synapse/.ssh/id_ed25519.pub && \
    git config --global commit.gpgsign true && \
    git config --global user.name "Kristian Olsson" && \
    git config --global user.email "developer@kasa.nu"

ENV PATH="/app/venv/bin:$PATH"

CMD ["python3", "-m", "services.ingestion.main"]
