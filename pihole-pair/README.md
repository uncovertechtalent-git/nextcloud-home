# pihole-pair

Two Pi-hole v6 machines as one resolver for a home network: if one is down, the network keeps its DNS. One is the **primary** (you edit it, only it), the other the **secondary** (a read-only copy). Three layers, each small:

| Layer | What | Where |
|---|---|---|
| Failover | a virtual address that moves to the healthy machine (keepalived, VRRP) | `keepalived.example.conf` |
| Same answers | a one-way sync of lists and DNS settings, primary to secondary, every 30 minutes | `pihole-sync.py` + service and timer |
| Clients | the DHCP server hands out the virtual address (and the secondary's own address) as DNS servers | your router; not in this repository |

```
keepalived.example.conf   the VRRP pair, same file on both machines apart from three lines
pihole-sync.py            the sync (Python standard library only)
pihole-sync.service       systemd unit, unprivileged, writes nothing to disk
pihole-sync.timer         every 30 minutes
pihole-sync.env.example   the two API addresses (and optional password files)
```

## Failover

The primary has the higher VRRP priority and holds the virtual address; the secondary is backup. A check script asks `systemctl is-active pihole-FTL`, so the address also moves when only the DNS service dies, not just the machine. Both machines answer DNS on their own addresses as well, so a client that has both can use either.

Measured on the pair this was built for (the secondary's priority was raised for a minute so that it took the address, then set back; nothing was stopped on the primary):

- the address moved to the secondary about 3 seconds after the change, and back to the primary about 3.6 seconds after the priority was restored (the primary takes it back by itself, because its priority is higher)
- 346 queries to the virtual address, one every 0.2 seconds over 75 seconds across both moves: none failed
- the probe ran on the secondary itself, so it shows the address never went unreachable but not how fast other machines learn the new hardware address; keepalived sends gratuitous ARP on every move

Expect roughly 3 to 4 seconds when a machine disappears, and 6 to 10 seconds when only the DNS service on the primary stops (two failed checks, then the move).

## The sync

`pihole-sync.py` runs on the **secondary**. It reads the primary's web API and writes to the secondary's own.

Copied from the primary:

- adlists, allow and deny entries, groups, clients: through Teleporter, tables only, followed by a gravity update on the secondary when adlists or groups changed
- the DNS settings that define the answers: upstreams, local records, CNAME records, conditional forwarding, domain, DNSSEC and a few flags

Never copied: web server ports and certificates (so each machine keeps its own), passwords, interface and listening settings, DHCP, whether blocking is switched on. A bad or empty primary is not copied either: the script refuses a source that shows no adlist.

The primary is only read. Every request that is not a `GET` is sent to the secondary, and the script stops itself if code ever tried to send one to the primary. `--dry-run` prints the differences and changes nothing.

Two things worth knowing from testing it with deliberate drift on the secondary (an extra allow entry, an extra local record, an extra adlist; each was repaired on the next run):

- a Teleporter import makes FTL on the secondary restart a moment after it returns; the script waits for the API, and counts a gravity update as done only when its output ends with `Done.`
- each machine rebuilds gravity on its own weekly schedule, so the number of blocked domains can differ slightly between the two when the upstream lists changed in between. The lists are the same; the compiled counts are not.

Prerequisites: Python 3 on the secondary, and the same Pi-hole version on both machines, or a newer one on the secondary. A Teleporter archive is not guaranteed to import across a version gap in the other direction. Tested with FTL 6.5 on the primary and 6.6.2 on the secondary.

Install on the secondary:

```sh
sudo install -m 755 pihole-sync.py /usr/local/sbin/pihole-sync
sudo install -d /etc/pihole-sync
sudo install -m 644 pihole-sync.env.example /etc/pihole-sync/sync.env      # then edit the two URLs
sudo install -m 644 pihole-sync.service pihole-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start pihole-sync.service && journalctl -u pihole-sync.service -n 5 -o cat   # "in sync"
sudo systemctl enable --now pihole-sync.timer
```

From then on, **edit only the primary**. Whatever you change by hand on the secondary in the synced areas is undone at the next run.

If a password is set on a Pi-hole, the service needs it, and the service does not run as root (`DynamicUser=yes`), so a root-only file would not be readable. Use systemd credentials: keep the password in a root-only file and let systemd hand it to the service. `sudo systemctl edit pihole-sync.service`:

```ini
[Service]
LoadCredential=primary-password:/etc/pihole-sync/primary-password
LoadCredential=secondary-password:/etc/pihole-sync/secondary-password
Environment=PRIMARY_PASSWORD_FILE=%d/primary-password
Environment=SECONDARY_PASSWORD_FILE=%d/secondary-password
```

(`%d` is the directory systemd puts the credentials in. Leave out the lines for a Pi-hole without a password.) The files can be `root:root`, mode 600; an app password works as well. Tested with a password on the secondary: login, sync and logout work. If the file cannot be read, the script says so and exits 2.

Tests: `python3 -m unittest pihole-pair/test_pihole_sync.py` runs the script against two fake Pi-hole APIs (in sync, drift repaired with passwords on both, empty source refused, wrong password, unreadable password file, unreachable primary, same address).

## Clients

The DHCP server decides. Hand out the **virtual address first** and the secondary's own address second. The first gives failover without clients doing anything; the second covers a failure of keepalived itself. Both machines answer the same, so it does not matter which one a client picks. Changing this setting is the one step that makes the pair real; until then the secondary and the virtual address are ready but unused.

## What is not covered

- Nothing alerts when the sync or keepalived stops. `systemctl list-timers pihole-sync.timer`, `journalctl -u pihole-sync` and `ip addr` on both machines are the places to look; an alert on the age of the last successful sync is the missing piece.
- If the primary is down for a long time, the secondary keeps what it had at the last sync. Edit nothing on the secondary during that time unless you promote it (give it the higher priority, and run the sync the other way round).
- Both machines should be on different hardware. A secondary that runs on the same host as the primary protects against a service failure only.
