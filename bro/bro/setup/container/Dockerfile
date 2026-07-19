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
# containers reuse it instead of running `uv sync` (~3.4s) on every launch (the
# entrypoint symlinks /workspace/.venv to it and stamps provision_repo.sh's skip
# marker). the bake is split into two layers so the expensive part survives source
# churn: the dependency install below mounts only the two manifests, keying its
# cache on exactly their content, while the project install after it keys on the
# full `project` context (filtered by the repo-root .dockerignore) and re-runs
# cheaply on any source change. the baked env matches a clone only when the
# clone's dependency manifests equal the ones this build ran from — CW_BASE_REF
# can base the clone on any ref — so the manifests are staged below and the
# entrypoint compares before reusing; a mismatch falls through to a normal sync
# from the clone's own manifests. UV_LINK_MODE=copy: /opt is on the image overlay,
# a different fs from any bind mount, so hardlinks can't be probed.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
# --no-install-package ppp: a project that vendors ppp as a path dependency
# (a submodule) can't satisfy it here — only the two manifests are mounted — so
# the editable ppp is deferred to the full-context layer below. for the ppp
# project itself the flag is a no-op (--no-install-project already excludes it).
RUN --mount=type=bind,from=project,source=pyproject.toml,target=/workspace/pyproject.toml \
    --mount=type=bind,from=project,source=uv.lock,target=/workspace/uv.lock \
    cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/cw-venv uv sync --frozen --all-groups --no-install-project \
      --no-install-package ppp \
 && chmod -R a+rwX /opt/uv-cache /opt/cw-venv

# editable-install the project and bake the `_entrypoints.py` console-script
# bridge into the venv (the bridge is a pure function of the `[project.scripts]`
# table, letting the entrypoint skip the ~1s `sync-scripts --entrypoints` regen).
# the sync runs with the project tree at /workspace, so the editable install's
# module finder records each top-level module under /workspace — the exact path
# the entrypoint clones into at runtime — and resolves against the live clone.
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
 && UV_PROJECT_ENVIRONMENT=/opt/cw-venv uv sync --frozen --all-groups \
 && /opt/cw-venv/bin/python -m dev.sync_scripts --entrypoints \
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
# ppp/setup/
RUN --mount=type=bind,from=project,target=/project-src \
    src=/project-src/setup \
 && if [ ! -f "$src/prelude.sh" ]; then src=/project-src/ppp/setup; fi \
 && mkdir -p /usr/local/lib/ppp-shell \
 && cp "$src/prelude.sh" "$src/log.sh" "$src/strict.sh" /usr/local/lib/ppp-shell/
COPY entrypoint.sh /usr/local/bin/cw-entrypoint
ENTRYPOINT ["/usr/local/bin/cw-entrypoint"]
