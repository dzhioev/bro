# syntax=docker/dockerfile:1
FROM bro-server-base

COPY build/bro-wheel/*.whl /tmp/bro-wheel/
RUN set -- /tmp/bro-wheel/*.whl \
 && [ "$#" -eq 1 ] \
 && uv pip install --system --no-cache "$1[trails-server]" \
 && rm -rf /tmp/bro-wheel

ARG BRO_TRAILS_REGION=us-east-1
ENV BRO_CONFIGS_DIR=/app/.configs
COPY oops/trails/server/runtime_credentials.json /tmp/runtime_credentials.json
RUN mkdir -p "${BRO_CONFIGS_DIR}" \
 && sed "s/__BRO_TRAILS_REGION__/${BRO_TRAILS_REGION}/g" \
  /tmp/runtime_credentials.json >"${BRO_CONFIGS_DIR}/credentials.json" \
 && rm /tmp/runtime_credentials.json

ENV PORT=8004
EXPOSE 8004

RUN install-launcher trails-server bro.trails.server.server

USER server

CMD ["trails-server", "--allow-env"]
