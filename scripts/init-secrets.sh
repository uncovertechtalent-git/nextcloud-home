#!/bin/sh
# Create ./secrets with one random 32-character password per file (mode 600,
# directory mode 700). Existing files are kept, so running it again is safe.
# Nothing is printed except the file names that were created.
set -eu
cd "$(dirname "$0")/.."

umask 077
mkdir -p secrets
chmod 700 secrets

for name in admin_password db_password db_root_password; do
  file="secrets/$name"
  if [ -e "$file" ]; then
    echo "kept    $file"
    continue
  fi
  # Letters and digits only: no quoting problems in any config file.
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 >"$file"
  chmod 600 "$file"
  echo "created $file"
done
