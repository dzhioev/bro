# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
ARG PYTHON_VERSION
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates gosu procps unzip gnupg bc jq \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs gh \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/awscli.zip /tmp/aws

ARG CLAUDE_CODE_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

RUN useradd -m -u 1000 -s /bin/bash ride \
 && mkdir -p /home/ride/.claude \
 && chown ride:ride /home/ride/.claude

# Interactive `ride exec` shells show the session banner once the runtime volume
# puts `ride` on PATH.
RUN cat >> /home/ride/.bashrc <<'EOF'

if [ -z "${RIDE_BANNER_SHOWN:-}" ] && command -v ride >/dev/null 2>&1; then
  export RIDE_BANNER_SHOWN=1
  ride banner || true
fi
EOF

# Claude's settings enable pyright-lsp, but each fresh mounted config also needs
# the matching installation records to avoid an interactive recommendation.
RUN mkdir -p /opt/claude-plugins-seed && chown ride:ride /opt/claude-plugins-seed
RUN gosu ride bash -ec '\
      export CLAUDE_CONFIG_DIR=/home/ride/.claude; \
      printf "%s" "{\"hasCompletedOnboarding\":true,\"installMethod\":\"global\"}" > "$CLAUDE_CONFIG_DIR/.claude.json"; \
      claude plugin marketplace add anthropics/claude-plugins-official; \
      claude plugin install pyright-lsp@claude-plugins-official; \
      cp -r "$CLAUDE_CONFIG_DIR/plugins/." /opt/claude-plugins-seed/; \
      rm -rf "$CLAUDE_CONFIG_DIR/plugins" "$CLAUDE_CONFIG_DIR/.claude.json" "$CLAUDE_CONFIG_DIR/backups"'

WORKDIR /workspace
ENV RIDE_IN_CONTAINER=1
ENV PATH=/var/ride/runtime/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# The entrypoint needs these before a workspace exists or the runtime volume is mounted.
COPY .bro-container/prelude.sh .bro-container/log.sh .bro-container/strict.sh /usr/local/lib/bro-shell/
COPY .bro-container/git.sh /usr/local/lib/bro-shell/container-git.sh
COPY .bro-container/entrypoint.sh /usr/local/bin/ride-entrypoint
RUN chmod +x /usr/local/bin/ride-entrypoint
ENTRYPOINT ["/usr/local/bin/ride-entrypoint"]
