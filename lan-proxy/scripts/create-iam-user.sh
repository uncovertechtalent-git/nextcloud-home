#!/bin/sh
# Create the IAM user that Caddy uses for the DNS challenge, give it the policy
# in iam-policy.example.json, and write its access key to an env file (mode
# 600). The key is never printed. Run it with credentials that may create IAM
# users (for example an admin profile); the new user can do nothing else than
# change the one challenge TXT record of the one hosted zone.
#
#   LAN_DOMAIN=lan.example.org ROUTE53_ZONE_ID=ZXXXXXXXXXXXXX \
#     sh scripts/create-iam-user.sh secrets/aws.env
set -eu
cd "$(dirname "$0")/.."

: "${LAN_DOMAIN:?}" "${ROUTE53_ZONE_ID:?}"
OUT="${1:?usage: create-iam-user.sh <env file to write>}"
USER_NAME="${IAM_USER:-caddy-acme-dns}"

[ ! -e "$OUT" ] || { echo "$OUT exists, not overwriting it" >&2; exit 1; }

umask 077
mkdir -p "$(dirname "$OUT")"
policy="$(mktemp)"
trap 'rm -f "$policy"' EXIT
sed -e "s/__ZONE_ID__/$ROUTE53_ZONE_ID/g" -e "s/__LAN_DOMAIN__/$LAN_DOMAIN/g" iam-policy.example.json >"$policy"

aws iam create-user --user-name "$USER_NAME" --tags Key=purpose,Value=caddy-acme-dns-challenge >/dev/null
aws iam put-user-policy --user-name "$USER_NAME" --policy-name acme-dns-challenge --policy-document "file://$policy"
aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text |
  awk 'NF == 2 { printf "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_REGION=us-east-1\n", $1, $2 }' >"$OUT"
[ -s "$OUT" ] || { echo "no key was written to $OUT" >&2; exit 1; }
chmod 600 "$OUT"
echo "user $USER_NAME created, key in $OUT (mode 600)"
