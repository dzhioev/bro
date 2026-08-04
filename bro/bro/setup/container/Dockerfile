# syntax=docker/dockerfile:1
FROM ppp-base

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

RUN useradd -m -u 1000 -s /bin/bash cw \
 && mkdir -p /home/cw/.claude \
 && chown cw:cw /home/cw/.claude

# print the session-info banner once per interactive shell — surfaces workspace
# paths, the `cw exec` command, and (for bro sessions) the ASCII Bro logo when
# users shell in via `cw exec <name>`. guarded so it runs only when `cw` is on
# PATH (i.e. after venv activation) and only once per shell process.
RUN cat >> /home/cw/.bashrc <<'EOF'

if [ -z "${CW_BANNER_SHOWN:-}" ] && command -v cw >/dev/null 2>&1; then
  export CW_BANNER_SHOWN=1
  cw banner || true
fi
EOF

# cw needs to talk to the host docker socket (bind-mounted at runtime) so
# deploy scripts can build + push without a nested runtime. group `docker`
# inside the container owns /var/run/docker.sock; cw is added so it can
# access without sudo. socket gid is reconciled with the host's at runtime
# by the entrypoint.
RUN groupadd -g 999 docker \
 && usermod -aG docker cw

# pre-install the pyright-lsp Claude Code plugin at build time. settings.json
# only *enables* the plugin (cw/docker.py); without a matching *install* record Claude
# prompts the "LSP Plugin Recommendation" on .py files. CLAUDE_CONFIG_DIR is the
# runtime ~/.claude path so the absolute paths baked into installed_plugins.json
# / known_marketplaces.json match where the entrypoint copies the seed. That dir
# is bind-mounted from a fresh per-session dir at runtime, so the built plugin
# tree is staged at /opt and copied in on first run. Done as cw (file ownership)
# and offline-after-build (no per-session network race). swift-lsp is left out —
# no Swift toolchain in this image.
RUN mkdir -p /opt/claude-plugins-seed && chown cw:cw /opt/claude-plugins-seed
RUN gosu cw bash -ec '\
      export CLAUDE_CONFIG_DIR=/home/cw/.claude; \
      printf "%s" "{\"hasCompletedOnboarding\":true,\"installMethod\":\"global\"}" > "$CLAUDE_CONFIG_DIR/.claude.json"; \
      claude plugin marketplace add anthropics/claude-plugins-official; \
      claude plugin install pyright-lsp@claude-plugins-official; \
      cp -r "$CLAUDE_CONFIG_DIR/plugins/." /opt/claude-plugins-seed/; \
      rm -rf "$CLAUDE_CONFIG_DIR/plugins" "$CLAUDE_CONFIG_DIR/.claude.json" "$CLAUDE_CONFIG_DIR/backups"'

# bake the project venv at /opt/cw-venv at image-build time so per-session
# containers reuse it instead of running `uv sync` on every launch. the bake is
# split into two layers so the expensive dependency install survives source
# churn: this layer mounts every workspace manifest but defers all editable
# members, which need their source trees; the next layer installs them from the
# full project context. UV_LINK_MODE=copy because /opt and bind mounts may be on
# different filesystems.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
RUN --mount=type=bind,from=project,source=pyproject.toml,target=/workspace/pyproject.toml \
    --mount=type=bind,from=project,source=uv.lock,target=/workspace/uv.lock \
    --mount=type=bind,from=project,source=bro/pyproject.toml,target=/workspace/bro/pyproject.toml \
    --mount=type=bind,from=project,source=bro-dev/pyproject.toml,target=/workspace/bro-dev/pyproject.toml \
    cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/cw-venv uv sync --frozen --all-packages --all-groups --all-extras --no-install-workspace \
 && chmod -R a+rwX /opt/uv-cache /opt/cw-venv

# editable-install the workspace, including its committed console-script bridges.
# setuptools compat mode records each member's source directory under /workspace,
# the exact path the entrypoint clones into at runtime.
# only the venv is kept from the tree: the build-time source is a bind mount
# removed in the same layer, so nothing is duplicated. the tar excludes are a
# fallback for builders that don't apply the repo-root .dockerignore to named
# contexts. the closing find opens up only the files this layer created —
# everything older is already a+rwX from the layer above, and a blanket chmod -R
# here would copy the whole venv up into this layer
RUN --mount=type=bind,from=project,target=/project-src \
    mkdir -p /workspace \
 && tar -C /project-src --exclude=./.git --exclude=./.venv --exclude=./var \
      --exclude=./node_modules --exclude=./.configs -cf - . | tar -C /workspace -xf - \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/cw-venv uv sync --frozen --all-packages --all-groups --all-extras \
 && find /workspace -mindepth 1 -delete \
 && find /opt/cw-venv /opt/uv-cache \( -type f -o -type d \) ! -perm -o+w \
      -exec chmod a+rwX {} +

# stage the manifests the venv bake ran from — the entrypoint's reuse gate
# compares the clone's copies against these. a separate layer so a gate-only
# change never invalidates the expensive venv layer above. ppp/pyproject.toml
# exists when the project vendors ppp as a submodule: the baked console-script
# bridge is a function of its [project.scripts], so a submodule bump that
# changes it must fail the gate
RUN --mount=type=bind,from=project,target=/project-src \
    mkdir -p /opt/cw-venv-manifest \
 && cp /project-src/pyproject.toml /project-src/uv.lock /opt/cw-venv-manifest/ \
 && if [ -f /project-src/ppp/pyproject.toml ]; then \
      cp /project-src/ppp/pyproject.toml /opt/cw-venv-manifest/ppp-pyproject.toml; fi

WORKDIR /workspace
ENV CW_IN_CONTAINER=1

# the shell-script prelude (logging + fail-fast guards), baked next to the
# entrypoint: the clone the entrypoint prepares does not exist yet when its
# early phases run. a project vendoring ppp as a submodule carries the files at
# ppp/bro/bro/setup/
RUN --mount=type=bind,from=project,target=/project-src \
    src=/project-src/bro/bro/setup \
 && if [ ! -f "$src/prelude.sh" ]; then src=/project-src/ppp/bro/bro/setup; fi \
 && mkdir -p /usr/local/lib/ppp-shell \
 && cp "$src/prelude.sh" "$src/log.sh" "$src/strict.sh" /usr/local/lib/ppp-shell/
COPY git.sh /usr/local/lib/ppp-shell/container-git.sh
COPY entrypoint.sh /usr/local/bin/cw-entrypoint
RUN chmod +x /usr/local/bin/cw-entrypoint
ENTRYPOINT ["/usr/local/bin/cw-entrypoint"]
