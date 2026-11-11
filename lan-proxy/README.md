# lan-proxy

Names instead of `host:port` for the services of a home network, with a real HTTPS certificate: `https://grafana.lan.example.org` instead of `http://<address>:3000`. One Caddy container is the front door. Nothing is opened to the internet.

```
Dockerfile              Caddy with the Route 53 DNS module, versions pinned
docker-compose.yml      the container (host networking)
Caddyfile               one wildcard site; one file per service is imported from sites/
sites.example/          examples of such files (plain HTTP backend, other host, HTTPS backend, Nextcloud)
iam-policy.example.json the policy of the AWS user that answers the certificate challenge
scripts/create-iam-user.sh  creates that user and writes its key to a mode-600 file
.env.example            the domain, the zone id
```

`lan.example.org` stands for a domain you own, and `ZXXXXXXXXXXXXX` for the id of its Route 53 hosted zone. Replace both.

## How it fits together

| Layer | Choice | Why |
|---|---|---|
| Names | `<service>.lan.example.org`, defined only in your own DNS servers; no public A records | A real domain lets Let's Encrypt issue a certificate without exposing anything; `home.arpa` cannot get a public certificate and every browser warns |
| Certificate | one wildcard `*.lan.example.org`, obtained and renewed by Caddy with the DNS-01 challenge through Route 53 | A wildcard keeps the individual service names out of the public certificate logs; only a short-lived TXT record touches public DNS |
| Proxy | Caddy on ports 80 and 443 of one machine, host networking; every name goes to a `host:port`, on this machine or another | One place for names and TLS. Port 80 only redirects to HTTPS |
| LAN DNS | one A record per name on the LAN's DNS servers, pointing at the proxy machine | No wildcard record, so a new name is a deliberate act |
| Tailnet DNS | the same names as headscale `extra_records_path`, pointing at the proxy machine's tailnet address | Phones and laptops on the tailnet resolve them without reaching the LAN DNS |
| AWS | an IAM user that may only change the one challenge record of the one zone | Least privilege, see below |

### The IAM policy

`iam-policy.example.json` allows `route53:ChangeResourceRecordSets` on one hosted zone and only when the change is a `CREATE`, `UPSERT` or `DELETE` of a `TXT` record named `_acme-challenge.lan.example.org`. It also allows `ListResourceRecordSets` on that zone and `GetChange`. A stolen key can therefore not touch any other record of the zone or any other zone. This was tested with `caddy-dns/route53` v1.6.2 and Caddy 2.11.9: the wildcard certificate was issued under exactly this policy, first from the Let's Encrypt staging CA, then from the real one.

## Set up

1. **AWS user.** With credentials that may create IAM users:

   ```sh
   LAN_DOMAIN=lan.example.org ROUTE53_ZONE_ID=ZXXXXXXXXXXXXX sh scripts/create-iam-user.sh secrets/aws.env
   ```

   The script creates the user, attaches the policy and writes the access key straight into `secrets/aws.env` (mode 600). The key is never printed. To remove it later: delete the access key, the user policy and the user.

2. **Configuration.** Copy `.env.example` to `.env` and set `LAN_DOMAIN` and `ROUTE53_ZONE_ID`. For the first run add the Let's Encrypt staging CA, which has no rate limits:

   ```sh
   ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
   ```

3. **Free ports 80 and 443** on the machine (move any web interface that sits there), then add one file per service under `sites/`, modelled on `sites.example/`. Each file matches one host name and names the backend:

   ```
   @grafana host grafana.{$LAN_DOMAIN}
   handle @grafana {
   	reverse_proxy 127.0.0.1:3000
   }
   ```

4. **Start.**

   ```sh
   mkdir -p data config sites
   docker compose up -d --build
   docker compose logs caddy | grep -E "obtained|error"
   ```

   `certificate obtained successfully` with the staging issuer means the challenge works. Remove the `ACME_CA` line from `.env` and run `docker compose up -d` again for the real certificate. Caddy renews it by itself well before it expires; watch `docker compose logs caddy` around the 60th day.

5. **DNS.** The names must resolve to the proxy machine.
   - On a Pi-hole v6 you can restart: `pihole-FTL --config dns.hosts '["<proxy-address> grafana.lan.example.org", ...]'` and restart FTL.
   - On a Pi-hole in daily use, avoid editing files and restarts: one API call adds the records, and the resolver may re-initialise for a second or two.

     ```sh
     curl -X PATCH -H "Content-Type: application/json" \
       -d '{"config":{"dns":{"hosts":["<proxy-address> grafana.lan.example.org"]}}}' \
       http://<pihole-address>/api/config
     ```

     This sets the whole list, so read the current one first (`GET /api/config/dns/hosts`) and send it back with your additions. Add `-H "sid: <session id>"` if the Pi-hole has a password.
   - On the tailnet, with headscale: set `dns.extra_records_path` to a JSON file and write one entry per name. Setting the option needs one restart; after that headscale re-reads the file when it changes (tested: a record added to the file answered on the tailnet within seconds, with no restart).

     ```json
     [ {"name": "grafana.lan.example.org", "type": "A", "value": "<proxy tailnet address>"} ]
     ```

6. **Check.** `curl -v https://grafana.lan.example.org/` from a LAN machine shows the Let's Encrypt wildcard certificate and the backend's answer. A backend that is switched off gives `502`.

Adding a service later: a file in `sites/`, a DNS record, then `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`. Format the Caddyfile with `caddy fmt`.

## Things to know

- **Certificate logs.** A wildcard certificate puts `*.lan.example.org` into the public certificate-transparency logs. That reveals that the domain exists, not which services run under it.
- **Backends behind HTTPS.** Some applications need to be told they sit behind an HTTPS proxy: Nextcloud (see the root README), Django applications such as NetBox may need `CSRF_TRUSTED_ORIGINS`, Grafana generates links from its `root_url`. A login form on NetBox was checked through this proxy and passes the CSRF check without changes.
- **A backend with a self-signed certificate** (a Pi-hole web interface) needs `tls_insecure_skip_verify` in its site file, see `sites.example/https-backend.caddy`. The browser still sees the real certificate.
- **Host networking** lets Caddy bind the machine's ports and reach local services on their loopback address. The admin API listens on `127.0.0.1:2019` of the machine; do not publish it.
- **A second headscale** (a standby control plane) has its own config. Give it the same file and config line, or a failover loses the names on the tailnet.
- **Pinned versions.** The Dockerfile pins Caddy and the Route 53 module. To update, change the version numbers together and rebuild with `docker compose build --pull`.
