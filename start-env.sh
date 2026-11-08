#!/bin/zsh
# Run: source start-env.sh
if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example to .env and set DATABASE_URL and SMTP first."
  return 1
fi
set -a
source .env
set +a
echo "Configuration loaded (passwords not displayed)."
