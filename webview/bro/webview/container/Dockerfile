ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends novnc websockify x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npm install -g @playwright/mcp@0.0.82 \
    && /usr/local/lib/node_modules/@playwright/mcp/node_modules/.bin/playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright /usr/local/lib/node_modules/@playwright
