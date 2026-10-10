ARG CLAUDE_CODE_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && mkdir -p /opt/claude-code \
 && ln -s "$(command -v claude)" /opt/claude-code/claude
