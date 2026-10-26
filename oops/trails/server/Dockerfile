# syntax=docker/dockerfile:1
FROM bro-server-base

COPY build/bro-wheel/*.whl /tmp/bro-wheel/
RUN set -- /tmp/bro-wheel/*.whl \
 && uv pip install --system --no-cache "bro[trails-server]" "$@" \
 && rm -rf /tmp/bro-wheel

ARG AWS_REGION=us-east-1
ENV AWS_REGION=${AWS_REGION}
ENV BRO_STORE=/app/.configs
COPY oops/trails/server/creds.json ${BRO_STORE}/creds.json

COPY oops/trails/server/entries.json /app/entries.json

ENV PORT=8004
EXPOSE 8004

RUN install-launcher trails-server bro.trails.server.server

USER server

CMD ["trails-server", "--allow-env", "--entries", "/app/entries.json"]
