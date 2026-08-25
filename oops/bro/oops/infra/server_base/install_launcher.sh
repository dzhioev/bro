#!/bin/sh
set -eu

name="$1"
module="$2"
printf '#!/bin/sh\nexec python -m %s "$@"\n' "$module" > "/usr/local/bin/$name"
chmod +x "/usr/local/bin/$name"
