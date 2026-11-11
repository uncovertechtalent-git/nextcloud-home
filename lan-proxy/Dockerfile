# Caddy with the Route 53 DNS module, built from the official images.
# Versions are pinned; to update, change the three version numbers together.
FROM caddy:2.11.9-builder AS builder
RUN xcaddy build v2.11.9 --with github.com/caddy-dns/route53@v1.6.2

FROM caddy:2.11.9
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
