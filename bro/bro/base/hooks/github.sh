# wire git and gh to resolve the token per use, so a short-lived minted app
# token is always read fresh
git config --global credential.helper '!f() { echo username=x-access-token; echo "password=$(credentials get {{insert #name}})"; }; f'
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/gh" <<WRAPPER
#!/usr/bin/env bash
GH_TOKEN="\$(credentials get {{insert #name}})" exec "$(command -v gh)" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/gh"
export PATH="$HOME/.local/bin:$PATH"
