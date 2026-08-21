# syntax=docker/dockerfile:1
# check=skip=InvalidDefaultArgInFrom
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}

# Resolve dependencies from manifests alone so source edits retain the expensive
# layer, then install editable workspace metadata against the fixed /workspace path.
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_LINK_MODE=copy
RUN --mount=type=bind,source=.bro-container/manifests,target=/manifests \
    mkdir -p /workspace \
 && cp -r /manifests/. /workspace/ \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/project-venv uv sync --frozen --all-packages --all-groups --all-extras --no-install-workspace \
 && find /workspace -mindepth 1 -delete \
 && chmod -R a+rwX /opt/uv-cache /opt/project-venv

RUN --mount=type=bind,target=/project-src \
    mkdir -p /workspace \
 && tar -C /project-src --exclude=./.bro-container -cf - . | tar -C /workspace -xf - \
 && cd /workspace \
 && UV_PROJECT_ENVIRONMENT=/opt/project-venv uv sync --frozen --all-packages --all-groups --all-extras \
 && find /workspace -mindepth 1 -delete \
 && find /opt/project-venv /opt/uv-cache \( -type f -o -type d \) ! -perm -o+w \
      -exec chmod a+rwX {} +

RUN --mount=type=bind,source=.bro-container/manifests,target=/manifests \
    mkdir -p /opt/project-venv-manifest \
 && cp -r /manifests/. /opt/project-venv-manifest/
