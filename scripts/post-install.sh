#!/bin/sh
# One-time setup after the first start of the stack. Safe to run again.
# Run it from anywhere; it works on the stack next to this script.
set -eu
cd "$(dirname "$0")/.."

occ() { docker compose exec -T -u www-data app php occ "$@"; }

# Phone numbers without a country code are read as German ones.
occ config:system:set default_phone_region --value=DE
# Nextcloud's heavy nightly jobs start at 01:00 UTC.
occ config:system:set maintenance_window_start --type=integer --value=1
# New users start with an empty home folder, not the demo files.
occ config:system:set skeletondirectory --value=""
# The cron container runs cron.php; do not wait for page loads to do it.
occ background:cron

# Calendar, Contacts, Talk (spreed, without the high-performance backend),
# Notes, Deck. Office and Collabora stay off.
for app in calendar contacts spreed notes deck; do
  occ app:install "$app" || occ app:enable "$app"
done

occ db:add-missing-indices
occ maintenance:repair --include-expensive

# Show what is switched on among the five.
occ app:list | sed -n '/^Enabled:/,/^Disabled:/p' | grep -E '^  - (calendar|contacts|spreed|notes|deck):'
