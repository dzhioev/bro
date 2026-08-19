# syntax=docker/dockerfile:1
FROM bro-base

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates gosu procps unzip gnupg bc \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /usr/share/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs gh docker-ce-cli docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
 && unzip -q /tmp/awscli.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/awscli.zip /tmp/aws

ARG CLAUDE_CODE_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

RUN useradd -m -u 1000 -s /bin/bash ride \
 && mkdir -p /home/ride/.claude \
 && chown ride:ride /home/ride/.claude

# print the session-info banner once per interactive shell — surfaces workspace
# paths, the `ride exec` command, and (for bro sessions) the ASCII Bro logo when
# users shell in via `ride exec <name>`. guarded so it runs only when `ride` is on
# PATH (i.e. after venv activation) and only once per shell process.
RUN cat >> /home/ride/.bashrc <<'EOF'

if [ -z "${RIDE_BANNER_SHOWN:-}" ] && command -v ride >/dev/null 2>&1; then
  export RIDE_BANNER_SHOWN=1
  ride banner || true
fi
EOF

# ride needs to talk to the host docker socket (bind-mounted at runtime) so
# deploy scripts can build + push without a nested runtime. group `docker`
# inside the container owns /var/run/docker.sock; ride is added so it can
# access without sudo. socket gid is reconciled with the host's at runtime
# by the entrypoint.
RUN groupadd -g 999 docker \
 && usermod -aG docker ride

# pre-install the pyright-lsp Claude Code plugin at build time. settings.json
# only *enables* the plugin (ride/ride/claude/claude_config.py); without a matching *install* record Claude
# prompts the "LSP Plugin Recommendation" on .py files. CLAUDE_CONFIG_DIR is the
# runtime ~/.claude path so the absolute paths baked into installed_plugins.json
# / known_marketplaces.json match where the entrypoint copies the seed. That dir
# is bind-mounted from a fresh per-session dir at runtime, so the built plugin
# tree is staged at /opt and copied in on first run. Done as ride (file ownership)
# and offline-after-build (no per-session network race). swift-lsp is left out —
# no Swift toolchain in this image.
RUN mkdir -p /opt/claude-plugins-seed && chown ride:ride /opt/claude-plugins-seed
RUN gosu ride bash -ec '\
      export CLAUDE_CONFIG_DIR=/home/ride/.claude; \
      printf "%s" "{\"hasCompletedOnboarding\":true,\"installMethod\":\"global\"}" > "$CLAUDE_CONFIG_DIR/.claude.json"; \
      claude plugin marketplace add anthropics/claude-plugins-official; \
      claude plugin install pyright-lsp@claude-plugins-official; \
      cp -r "$CLAUDE_CONFIG_DIR/plugins/." /opt/claude-plugins-seed/; \
      rm -rf "$CLAUDE_CONFIG_DIR/plugins" "$CLAUDE_CONFIG_DIR/.claude.json" "$CLAUDE_CONFIG_DIR/backups"'

# bake the project venv at /opt/ride-venv at image-build time so per-session
# containers reuse it instead of running `uv sync` on every launch. the bake is
# split into two layers so the expensive dependency install survives source
# churn: this layer mounts only the manifest set the context stages — every file
# uv reads to resolve the environment and nothing else, so source edits leave its
# cache key alone — and defers all editable members, which need their source
# trees, to the next layer.
# UV_LINK_MODE=copy because /opt and bind mounts may be on different filesystems.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
RUN --mount=type=bind,source=.bro-container/manifests,target=/manifests \
    mkdir -p /workspace \
 && cp -r /manifests/. /workspace/ \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/ride-venv uv sync --frozen --all-packages --all-groups --all-extras --no-install-workspace \
 && find /workspace -mindepth 1 -delete \
 && chmod -R a+rwX /opt/uv-cache /opt/ride-venv

# editable-install the workspace, including its committed console-script bridges.
# setuptools compat mode records each member's source directory under /workspace,
# the exact path the entrypoint clones into at runtime.
# only the venv is kept from the tree: the build-time source is a bind mount
# removed in the same layer, so nothing is duplicated. the closing find opens up
# only the files this layer created — everything older is already a+rwX from the
# layer above, and a blanket chmod -R here would copy the whole venv up into this
# layer
RUN --mount=type=bind,target=/project-src \
    mkdir -p /workspace \
 && tar -C /project-src --exclude=./.bro-container -cf - . | tar -C /workspace -xf - \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/ride-venv uv sync --frozen --all-packages --all-groups --all-extras \
 && find /workspace -mindepth 1 -delete \
 && find /opt/ride-venv /opt/uv-cache \( -type f -o -type d \) ! -perm -o+w \
      -exec chmod a+rwX {} +

# stage the manifests the venv bake ran from — the entrypoint's reuse gate
# compares the clone's copies against these, at the same relative paths. a
# separate layer so a gate-only change never invalidates the expensive venv layer
# above
RUN --mount=type=bind,source=.bro-container/manifests,target=/manifests \
    mkdir -p /opt/ride-venv-manifest \
 && cp -r /manifests/. /opt/ride-venv-manifest/

WORKDIR /workspace
ENV RIDE_IN_CONTAINER=1

# the shell-script prelude (logging + fail-fast guards), baked next to the
# entrypoint: the clone the entrypoint prepares does not exist yet when its early
# phases run
COPY .bro-container/prelude.sh .bro-container/log.sh .bro-container/strict.sh /usr/local/lib/bro-shell/
COPY .bro-container/git.sh /usr/local/lib/bro-shell/container-git.sh
COPY .bro-container/entrypoint.sh /usr/local/bin/ride-entrypoint
RUN chmod +x /usr/local/bin/ride-entrypoint
ENTRYPOINT ["/usr/local/bin/ride-entrypoint"]
