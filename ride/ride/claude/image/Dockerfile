ARG CLAUDE_CODE_VERSION
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && mkdir -p /opt/claude-code \
 && ln -s "$(command -v claude)" /opt/claude-code/claude

# Claude's settings enable pyright-lsp, but each fresh mounted config also needs
# the matching installation records to avoid an interactive recommendation.
RUN mkdir -p /home/ride/.claude && chown ride:ride /home/ride/.claude
RUN mkdir -p /opt/claude-plugins-seed && chown ride:ride /opt/claude-plugins-seed
RUN gosu ride bash -ec '\
      export CLAUDE_CONFIG_DIR=/home/ride/.claude; \
      printf "%s" "{\"hasCompletedOnboarding\":true,\"installMethod\":\"global\"}" > "$CLAUDE_CONFIG_DIR/.claude.json"; \
      claude plugin marketplace add anthropics/claude-plugins-official; \
      claude plugin install pyright-lsp@claude-plugins-official; \
      cp -r "$CLAUDE_CONFIG_DIR/plugins/." /opt/claude-plugins-seed/; \
      rm -rf "$CLAUDE_CONFIG_DIR/plugins" "$CLAUDE_CONFIG_DIR/.claude.json" "$CLAUDE_CONFIG_DIR/backups"'

