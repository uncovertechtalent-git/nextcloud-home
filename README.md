# nextcloud-home

Nextcloud for a home: files, calendar, contacts, chat and video calls (Talk), notes and boards (Deck), on one machine, in Docker Compose. It is reachable on the home network and over a Tailscale network, and nowhere else. A nightly job copies the data, the config and a database dump to a NAS.

It is not the Nextcloud all-in-one image. That image wants a public domain and brings its own reverse proxy; this install has neither.

```
docker-compose.yml     the stack: MariaDB, Redis, Nextcloud (Apache), a cron container
.env.example           port, bind address, trusted hosts. No secrets.
scripts/
  init-secrets.sh      creates ./secrets, one random password per file
  post-install.sh      settings and the five apps, run once after the first start
backup/
  nextcloud-backup.sh      the nightly job (runs as root)
  nextcloud-backup.service systemd unit for the job
  nextcloud-backup.timer   01:15 every night
  backup.env.example       where the NAS is, which key, how long to keep
```

## Design

| Piece | Choice | Why |
|---|---|---|
| Nextcloud | `nextcloud:35-apache`, pinned to the 35 line | The `stable` tag trails by one major; 35 has the longest support left. Minor updates arrive with `docker compose pull`. |
| Database | `mariadb:10.11` (LTS), `READ-COMMITTED` | The supported, well-tested pairing. |
| Cache and locks | `redis:7-alpine`, no password, no published port | Only containers of this stack can reach it. A password file does not work here: Nextcloud reads it at run time as the web user, and a mode-600 secret is unreadable to that user. |
| Background jobs | a second container runs `cron.php` | The "cron" mode, as Nextcloud recommends. |
| Port | host port `8380` (`NC_PORT`), plain HTTP | Pick any free high port. TLS is a separate step, see "Not done". |
| Exposure | bound to all interfaces of the machine; no router port forward, no public DNS | The LAN interface and the Tailscale interface are the two ways in. |
| Secrets | files under `./secrets` (directory 700, files 600), passed in with compose secrets and the images' `*_FILE` variables | No password in the compose file, in `.env`, in the repo, or in a command line. |
| Data | bind mounts `./data`, `./html`, `./db` next to the compose file | Easy to find and to back up. |

Settings made by `scripts/post-install.sh`: `default_phone_region=DE` (change it for your country), the maintenance window for heavy jobs at 01:00 UTC, an empty skeleton directory (new users start with no demo files), background jobs in cron mode. Apps: Calendar, Contacts, Talk, Notes, Deck. Office and Collabora are not installed.

Talk runs without the high-performance backend. That is fine for chat and calls between two or three people on the same network; larger calls need the backend.

## Install

You need Docker with the Compose plugin, a free port, and a machine that has a LAN address and (for remote use) a Tailscale address.

```sh
git clone https://github.com/uncovertechtalent-git/nextcloud-home.git
cd nextcloud-home
cp .env.example .env            # edit: NC_PORT, NC_TRUSTED_DOMAINS
sh scripts/init-secrets.sh      # makes ./secrets
mkdir -p html data db
docker compose up -d            # the first start installs Nextcloud, about a minute
sh scripts/post-install.sh      # settings and apps
```

`NC_TRUSTED_DOMAINS` is a space-separated list: the LAN address of the machine, its Tailscale address, and its MagicDNS name if you use one. Nextcloud answers only to those names. It is read at the first install; later changes go through `occ`:

```sh
docker compose exec -u www-data app php occ config:system:set trusted_domains 4 --value=new-name
docker compose exec -u www-data app php occ config:system:get trusted_domains
```

Log in at `http://<lan-address>:8380` or `http://<tailscale-address>:8380` as `admin`. The first password is in `secrets/admin_password`. It only seeds the install: after you change it in the web interface, the file is out of date. Create a personal account and keep `admin` for administration.

`docker compose ps` should show all four services healthy. In the web interface, Administration settings, Overview lists what Nextcloud itself thinks is wrong. On this HTTP-only setup it will report the missing HTTPS and HSTS, the missing Talk backend and the unset mail server; those are known.

### Update

Take a backup first (`systemctl start nextcloud-backup.service`), then:

```sh
docker compose pull && docker compose up -d
```

Nextcloud upgrades one major version at a time. To move from 35 to 36, change the tag in `docker-compose.yml` once 36 is out and has run its first patch releases.

## Backup

### What is backed up, and where it goes

`backup/nextcloud-backup.sh` runs as root every night at 01:15 (the timer adds up to five minutes of random delay). It logs in to the NAS over ssh and writes under one folder there (`NAS_DIR`), which only root can read:

```
data/<generation>/<stamp>-a.tar, <stamp>-b.tar   user files
config/config-<stamp>.tar                        html/config, custom_apps, themes, secrets, .env, docker-compose.yml
db/nextcloud-<stamp>.sql.gz                      database dump
LAST_OK                                          stamp of the last run that finished
```

A run takes these steps:

1. Data, pass 1, while Nextcloud runs. This is the bulk of the transfer.
2. Config archive.
3. Maintenance mode on.
4. Data, pass 2: only what changed during pass 1. Short.
5. Database dump (`mariadb-dump --single-transaction`), compressed and checked with `gzip -t`.
6. Maintenance mode off, `LAST_OK` written, old backups removed.

In steps 3 to 6 Nextcloud answers with its maintenance page. With a small data directory that is a few seconds; it grows with the amount of data that changed since pass 1. If anything fails after step 3, the script's exit handler turns maintenance mode off again.

The data archives are GNU tar incremental. The first run of a week (Sundays, or whenever the state is missing) starts a new generation with a full archive; every later archive holds what changed since the one before. The incremental state is kept on the Nextcloud machine in `/var/lib/nextcloud-backup`; if it is lost, the next run simply starts a new full generation. Preview images are not backed up (Nextcloud rebuilds them). `secrets/admin_password` is not backed up either (see Install).

Every upload is checked: the byte count that tar reports must equal the size of the file on the NAS, or the run fails and the file is not renamed into place. A run takes the lock `/run/lock/nextcloud-backup.lock`, so two runs never overlap.

Retention: the newest 28 database dumps and config archives (`KEEP`), and the newest 4 generations of data archives (`KEEP_GENERATIONS`). The script deletes only names that match its own stamp pattern inside `NAS_DIR`.

The config archive contains `config.php` and the database passwords. That is what makes a restore possible, and it is why `NAS_DIR` is mode 700 and owned by root.

### Why tar over ssh and not rsync

Synology's `rsync` refuses to act as the receiving side (`rsync service is no running`, code 43) while the "rsync service" in DSM is switched off, even over ssh. Switching it on is a change to the NAS's file services that this setup does not need: tar over ssh needs only ssh and `sudo` on the NAS. With the service on, an rsync version with hard-linked snapshots (`--link-dest`) would be possible and cheaper per night once the data is large. That version was written first and could not be tested against the NAS, so it is not in the repository.

### Install the job

On the Nextcloud machine, as a user with sudo:

```sh
sudo install -d /etc/nextcloud-home
sudo install -m 600 backup/backup.env.example /etc/nextcloud-home/backup.env   # then edit it
sudo install -m 755 backup/nextcloud-backup.sh /usr/local/sbin/nextcloud-backup
sudo install -m 644 backup/nextcloud-backup.service backup/nextcloud-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

`backup.env` names the NAS user and address, the folder on the NAS, the private key and the `known_hosts` file. Log in to the NAS once by hand with that key so the host key is in `known_hosts`; the job refuses unknown hosts. The NAS user needs passwordless `sudo` there.

Run it once by hand and look at the result before you arm the timer:

```sh
sudo systemctl start nextcloud-backup.service
sudo journalctl -u nextcloud-backup.service -n 30 -o cat      # ends with "done <stamp>"
sudo systemctl enable --now nextcloud-backup.timer
systemctl list-timers nextcloud-backup.timer
```

On the NAS, check that the archives list (`sudo tar -tf <file> | head`), that the dump unpacks (`sudo gzip -t <file>`) and that `LAST_OK` holds the stamp of your run. Then do the restore below once into a scratch folder, before you rely on the backup.

### Watch it

Nothing in this repo alerts when the job stops. `LAST_OK` on the NAS and `systemctl list-timers` are the two places to look; an alert on the age of `LAST_OK` is the missing piece, and a dead backup job is easy to miss for months.

## Restore

The order matters: configuration first, then the data, then the database, then the containers. Do it as root on the machine that will run Nextcloud, which can be a new machine. A restore point is a database dump plus the data archives up to the same stamp; the commands below use the newest of each.

```sh
# Settings for reaching the NAS. On a new machine, recreate /etc/nextcloud-home/backup.env
# from backup/backup.env.example first.
. /etc/nextcloud-home/backup.env
nas() { ssh -n -i "$SSH_KEY" -o UserKnownHostsFile="$KNOWN_HOSTS" "$NAS_TARGET" "$@"; }

STACK=/path/to/stack            # the folder that will hold docker-compose.yml, data/, db/ ...
mkdir -p "$STACK/scripts" && cd "$STACK"
```

1. **Compose files.** Copy `docker-compose.yml` and `scripts/init-secrets.sh` from this repository into `$STACK` (the config archive also holds the compose file).

2. **Config.** Restores `html/config`, `html/custom_apps`, `html/themes`, `secrets/`, `.env` and `docker-compose.yml`. The backup leaves out the admin password file; `init-secrets.sh` recreates exactly the missing file and keeps the others.

   ```sh
   cfg=$(nas "sudo ls -1 $NAS_DIR/config" | sort | tail -1)
   nas "sudo cat $NAS_DIR/config/$cfg" | tar -x --numeric-owner -C "$STACK"
   sh scripts/init-secrets.sh
   ```

3. **Data.** Apply every archive of the newest generation in name order, oldest first. `--listed-incremental=/dev/null` makes tar apply the deletions recorded in the incremental archives as well.

   ```sh
   gen=$(nas "sudo ls -1 $NAS_DIR/data" | sort | tail -1)
   for a in $(nas "sudo ls -1 $NAS_DIR/data/$gen" | sort); do
     nas "sudo cat $NAS_DIR/data/$gen/$a" | tar -x --numeric-owner --listed-incremental=/dev/null -C "$STACK"
   done
   ```

   To go back to an earlier day, stop the loop after the archives of that day (names starting with that date) and use the dump of that day in step 4. To go back past the newest generation, use an older folder under `data/`.

4. **Database.** Start only the database, wait until it is healthy, load the dump.

   ```sh
   mkdir -p db html
   docker compose up -d db
   until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q db)")" = healthy ]; do sleep 3; done
   dump=$(nas "sudo ls -1 $NAS_DIR/db" | sort | tail -1)
   nas "sudo cat $NAS_DIR/db/$dump" | gunzip |
     docker compose exec -T db sh -c 'MYSQL_PWD="$(cat /run/secrets/db_root_password)" exec mariadb -u root nextcloud'
   ```

5. **Containers.** The code in `html/` comes from the image, not from the backup.

   ```sh
   docker compose up -d
   ```

   The first start prints `Command "maintenance:install" is not defined` several times and the app container restarts once, after about two minutes. That is the image's installer finding an installation that already exists; the second start is clean. Then:

   ```sh
   docker compose exec -u www-data app php occ maintenance:update:htaccess   # without it /login answers 404
   docker compose exec -u www-data app php occ maintenance:data-fingerprint  # tells sync clients the data went back in time
   ```

   Log in. If files were uploaded after the dump you used, `occ files:scan --all` brings the file index in line with the disk.

On 2026-11-11 the newest backup was restored with these commands into a second instance on the same machine (only the port in `.env` and the compose project name differed): same version, same users and apps, login with the live admin password worked, and the data matched the live copy except for the preview images.

To get a single file back without a full restore, extract just that path from the archives (`tar -x --listed-incremental=/dev/null -f <archive> data/<user>/files/<path>`), newest archive that contains it last.

## Not done

- **TLS.** The login travels in clear text inside your network. A reverse proxy with a certificate comes later; then set `NC_PROTOCOL=https` and recreate the containers.
- **Talk high-performance backend**, **Office/Collabora**, **mail**.
- **Single sign-on.** Nextcloud can be the OIDC provider for other services (the "OIDC Identity Provider" app); not set up.
- **Alerting on a stale backup**, see "Watch it".
- **Off-site copy.** The NAS is a second disk in the same building, not a disaster recovery plan.

## License

MIT, see `LICENSE`.
