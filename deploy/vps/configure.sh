#!/usr/bin/env bash
# Run once on the VPS with the OpenRouter token on standard input.
set -euo pipefail
umask 077

root=$1
hostname=$2
if [[ ! $hostname =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
    printf '%s\n' 'Expected a DNS hostname such as demo.example.org' >&2
    exit 65
fi

IFS= read -r api_key || [[ -n $api_key ]]
if [[ ! $api_key =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    printf '%s\n' 'Expected an OpenRouter token on standard input' >&2
    exit 65
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$root/shared"
cd -- "$root/shared"
if [[ -e production.env ]]; then
    printf '%s\n' 'Production configuration already exists. Update the specific value to preserve database and mail credentials.' >&2
    exit 73
fi

temp=$(mktemp -d ./config.XXXXXXXX)
trap 'rm -rf -- "$temp"' EXIT
(
    cd -- "$temp"
    bash "$script_dir/../init-env.sh"
)
cat -- "$temp/.env" >"$temp/production.env"
{
    printf 'II_CADDY_ADDRESS=%s\n' "$hostname"
    printf 'II_PUBLIC_URL=https://%s\n' "$hostname"
    printf 'II_LISTEN_IP=0.0.0.0\nII_HTTPS_PORT=443\n'
    printf 'OPENROUTER_API_KEY=%s\n' "$api_key"
} >>"$temp/production.env"
unset api_key
printf '%s\n' "$hostname" >"$temp/hostname"
mv -- "$temp/hostname" hostname
mv -- "$temp/production.env" production.env
printf '%s\n' 'Production environment configured'
