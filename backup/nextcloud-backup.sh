#!/bin/bash
# Nightly Nextcloud backup to a NAS over ssh. Runs as root (the data directory
# belongs to the container's web user). Settings come from
# /etc/nextcloud-home/backup.env (see backup/backup.env.example).
#
# Transport is GNU tar over ssh, not rsync: Synology's rsync refuses to act as
# the receiving side while the DSM "rsync service" is switched off. If you
# enable that service, an rsync-based version of this script is a drop-in
# replacement (see README.md).
#
# One run does, in this order:
#   1. data pass 1 with Nextcloud running (the bulk; a full archive on the
#      first run and on Sundays, an incremental archive on other days)
#   2. config archive
#   3. maintenance mode on
#   4. data pass 2, an incremental archive of what changed during pass 1
#   5. database dump
#   6. maintenance mode off
#   7. publish: write LAST_OK, drop old backups
#
# On the NAS, under NAS_DIR:
#   data/<generation>/<stamp>-a.tar, <stamp>-b.tar   a generation starts with
#       a full archive; every later archive is incremental to the one before
#   config/config-<stamp>.tar        html/config, custom_apps, themes, secrets,
#       .env and docker-compose.yml
#   db/nextcloud-<stamp>.sql.gz
#   LAST_OK                          stamp of the last run that finished
#
# Every upload is checked: the byte count tar reports must equal the size of
# the file on the NAS. State for the incremental chain lives in STATE_DIR on
# this machine; losing it only means the next run starts a new full generation.
set -euo pipefail
umask 077

CONF="${NEXTCLOUD_BACKUP_ENV:-/etc/nextcloud-home/backup.env}"
# shellcheck disable=SC1090
. "$CONF"

: "${STACK_DIR:?}" "${NAS_TARGET:?}" "${NAS_DIR:?}" "${SSH_KEY:?}"
KEEP="${KEEP:-28}"                  # newest db dumps and config archives to keep
KEEP_GENERATIONS="${KEEP_GENERATIONS:-4}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/root/.ssh/known_hosts}"
STATE_DIR="${STATE_DIR:-/var/lib/nextcloud-backup}"

STAMP="$(date +%Y-%m-%d_%H%M)"
SSH=(ssh -i "$SSH_KEY" -o BatchMode=yes -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes)
log() { echo "$(date '+%F %T') $*"; }
nas() { "${SSH[@]}" -n "$NAS_TARGET" "$@"; }            # no stdin: safe inside loops
nas_in() { "${SSH[@]}" "$NAS_TARGET" "$@"; }             # passes stdin through
compose() { docker compose --project-directory "$STACK_DIR" "$@"; }
occ() { compose exec -T -u www-data app php occ "$@"; }

# One run at a time.
exec 9>/run/lock/nextcloud-backup.lock
flock -n 9 || { log "another backup is running, exiting"; exit 1; }

WORK=""
MAINT=off
finish() {
  rc=$?
  [ -z "$WORK" ] || rm -rf "$WORK"
  if [ "$MAINT" = on ]; then
    log "leaving maintenance mode"
    occ maintenance:mode --off >/dev/null || log "WARNING: could not turn maintenance mode off, do it by hand"
  fi
  [ "$rc" -eq 0 ] || log "FAILED (exit $rc)"
}
trap finish EXIT
WORK="$(mktemp -d)"
mkdir -p "$STATE_DIR"

# finish_upload <remote path> <expected bytes> : the data was written to
# <remote path>.partial; compare its size with the byte count we sent, then
# rename it into place.
finish_upload() {
  local rpath=$1 want=$2 got
  got="$(nas "sudo wc -c -- '$rpath.partial'" | awk '{print $1}')"
  if [ "$want" != "$got" ]; then
    log "size mismatch for $rpath: sent $want, NAS has $got"
    return 1
  fi
  nas "sudo mv '$rpath.partial' '$rpath'"
}

# tar_up <remote path> <tar args...> : tar to stdout, stream to the NAS.
# tar exit 1 means "a file changed while it was read", normal in the warm pass.
tar_up() {
  local rpath=$1 st want
  shift
  set +e
  tar --totals --numeric-owner --no-check-device -C "$STACK_DIR" -cf - "$@" 2>"$WORK/tar.err" |
    nas_in "sudo tee -- '$rpath.partial' >/dev/null"
  st=("${PIPESTATUS[@]}")
  set -e
  grep -v -E '^tar: (Removing leading|.*file changed as we read it|.*socket ignored|Total bytes written)' "$WORK/tar.err" >&2 || true
  if [ "${st[0]}" -gt 1 ] || [ "${st[1]}" -ne 0 ]; then
    log "upload of $rpath failed (tar ${st[0]}, ssh ${st[1]})"
    return 1
  fi
  want="$(sed -n 's/^Total bytes written: \([0-9]*\).*/\1/p' "$WORK/tar.err")"
  finish_upload "$rpath" "$want"
}

# data_pass <suffix> : one incremental (or full) archive of ./data.
# The snapshot file is a copy; it only replaces the real one after the upload
# is verified, so a failed upload cannot make the next run skip files.
data_pass() {
  local suffix=$1 snar="$STATE_DIR/current.snar"
  cp -p "$snar" "$WORK/work.snar" 2>/dev/null || : >"$WORK/work.snar"
  tar_up "$NAS_DIR/data/$GEN/$STAMP-$suffix.tar" \
    --listed-incremental="$WORK/work.snar" \
    --exclude='data/appdata_*/preview' data
  mv "$WORK/work.snar" "$snar"
}

# Generation: a new full one when there is none, when it is Sunday and the
# current one is older than today, or when asked.
GEN="$(cat "$STATE_DIR/current.gen" 2>/dev/null || true)"
if [ -z "$GEN" ] || [ ! -s "$STATE_DIR/current.snar" ] || [ "${FORCE_FULL:-0}" = 1 ] ||
  { [ "$(date +%u)" = 7 ] && [ "${GEN%%_*}" != "${STAMP%%_*}" ]; }; then
  GEN="$STAMP"
  rm -f "$STATE_DIR/current.snar"
  log "new generation $GEN (full archive)"
fi

log "start $STAMP"
nas "sudo install -d -m 700 '$NAS_DIR/data/$GEN' '$NAS_DIR/config' '$NAS_DIR/db'"

log "data pass 1, Nextcloud running"
data_pass a
echo "$GEN" >"$STATE_DIR/current.gen"

# Before maintenance mode goes on: config.php records that flag, and a restore
# must not come back stuck in maintenance.
log "config archive"
tar_up "$NAS_DIR/config/config-$STAMP.tar" --exclude='secrets/admin_password' \
  html/config html/custom_apps html/themes secrets .env docker-compose.yml

log "maintenance mode on"
occ maintenance:mode --on >/dev/null
MAINT=on

log "data pass 2, what changed meanwhile"
data_pass b

log "database dump"
# The password is read inside the container, so it never reaches this shell.
# shellcheck disable=SC2016
compose exec -T db sh -c \
  'MYSQL_PWD="$(cat /run/secrets/db_root_password)" exec mariadb-dump --single-transaction --routines --events -u root nextcloud' |
  gzip -9 >"$WORK/db.sql.gz"
# A dump of a few bytes means the dump command failed quietly.
[ "$(wc -c <"$WORK/db.sql.gz")" -gt 1000 ] || { log "dump too small, aborting"; exit 1; }
gzip -t "$WORK/db.sql.gz"
nas_in "sudo tee -- '$NAS_DIR/db/nextcloud-$STAMP.sql.gz.partial' >/dev/null" <"$WORK/db.sql.gz"
finish_upload "$NAS_DIR/db/nextcloud-$STAMP.sql.gz" "$(wc -c <"$WORK/db.sql.gz")"

log "maintenance mode off"
occ maintenance:mode --off >/dev/null
MAINT=off

log "publish"
echo "$STAMP" | nas_in "sudo tee -- '$NAS_DIR/LAST_OK' >/dev/null"

# Prune on this side: list the names, keep the newest, delete the rest by exact
# name (names must match the stamp pattern, nothing else is touched).
prune() { # <dir> <name regex> <keep> <rm flags>
  nas "sudo ls -1 '$NAS_DIR/$1'" | { grep -E "$2" || true; } | sort | head -n "-$3" |
    while read -r name; do nas "sudo rm $4 -- '$NAS_DIR/$1/$name'"; log "removed $1/$name"; done
}
prune db '^nextcloud-[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}\.sql\.gz$' "$KEEP" -f
prune config '^config-[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}\.tar$' "$KEEP" -f
prune data '^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}$' "$KEEP_GENERATIONS" -rf

# Leftovers of runs that failed half way.
nas "sudo find '$NAS_DIR' -name '*.partial' -mmin +1440 -exec rm -f {} +"

log "done $STAMP"
