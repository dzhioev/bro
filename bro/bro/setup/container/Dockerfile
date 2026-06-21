# syntax=docker/dockerfile:1
FROM python:3.12-slim

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

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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
# only *enables* the plugin (cw.py); without a matching *install* record Claude
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
# containers reuse it instead of running `uv sync` (~3.4s) on every launch (the
# entrypoint symlinks /workspace/.venv to it and stamps provision_repo.sh's skip
# marker). the sync runs with the project tree at /workspace, so the editable
# install's module finder records each top-level module under /workspace — the
# exact path the entrypoint clones into at runtime — and resolves against the live
# clone. also bake the `_entrypoints.py` console-script bridge into the venv so the
# entrypoint can skip the ~1s `sync-scripts --entrypoints` regen too: the bridge is
# a pure function of the `[project.scripts]` table, which the tag pins, so the baked
# copy matches any session whose clone produced this image. only the venv is kept:
# the build-time source is a bind mount removed in the same layer, so nothing is
# duplicated. correctness holds for the life of the image because the tag pins
# pyproject.toml + uv.lock (see cw.py:_image_tag), so a session's clone always
# carries the same deps and the same launcher set the bake produced. UV_LINK_MODE=copy:
# /opt is on the image overlay, a different fs from any bind mount, so hardlinks can't
# be probed.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
RUN --mount=type=bind,from=proj,target=/proj-src \
    mkdir -p /workspace \
 && tar -C /proj-src --exclude=./.git --exclude=./.venv --exclude=./var \
      --exclude=./node_modules --exclude=./.configs -cf - . | tar -C /workspace -xf - \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/cw-venv uv sync --frozen --all-groups \
 && /opt/cw-venv/bin/python -m sync_scripts --entrypoints \
 && find /workspace -mindepth 1 -delete \
 && chmod -R a+rwX /opt/uv-cache /opt/cw-venv

WORKDIR /workspace
ENV CW_IN_CONTAINER=1

COPY entrypoint.sh /usr/local/bin/cw-entrypoint
ENTRYPOINT ["/usr/local/bin/cw-entrypoint"]
