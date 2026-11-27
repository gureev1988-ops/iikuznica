#!/usr/bin/env bash
set -euo pipefail
umask 077

root=$1
revision=$2
release=$root/releases/$revision
config=$root/shared/production.env
current_file=$root/shared/current
public_host=$(<"$root/shared/hostname")
if [[ ! $public_host =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
    printf '%s\n' 'Expected a DNS hostname in shared/hostname' >&2
    exit 65
fi

previous=
if [[ -s $current_file ]]; then
    previous=$(<"$current_file")
    [[ $previous =~ ^[0-9a-f]{40}$ && -d $root/releases/$previous ]]
fi

compose() {
    local sha=$1
    local source=$root/releases/$sha
    shift
    II_APP_IMAGE="iikuznica-assembly:$sha" II_IMAP_IMAGE="iikuznica-imap:$sha" II_RELEASE_SHA="$sha" \
        docker compose --project-name iikuznica-prod --project-directory "$source" --env-file "$config" \
        -f "$source/compose.yaml" -f "$source/deploy/vps/compose.production.yaml" "$@"
}

verify_https() {
    local sha=$1
    local headers
    headers=$(curl -fsS --connect-timeout 5 --max-time 10 --retry 15 --retry-delay 3 --retry-all-errors \
        --resolve "$public_host:443:127.0.0.1" -D - -o /dev/null "https://$public_host/healthz") || return
    printf '%s\n' "$headers" | awk -v sha="$sha" '
        tolower($1) == "x-iikuznica-revision:" {
            sub(/\r$/, "", $2)
            if ($2 == sha) found=1
        }
        END { exit !found }
    '
}

compose "$revision" config --quiet
compose "$revision" build api imap
# Check real credentials and network paths before changing running services.
compose "$revision" run --no-deps --rm -T --entrypoint python interactive - <"$release/deploy/qa/models.py"

if [[ -n $previous && $previous != "$revision" ]]; then
    mkdir -p "$root/backups"
    backup=$(mktemp "$root/backups/$previous.XXXXXXXX")
    trap 'rm -f -- "$backup"' EXIT
    compose "$previous" exec -T db pg_dump -U iik -d iikuznica -Fc >"$backup"
    mv -- "$backup" "$backup.dump"
    trap - EXIT
fi

rollback() {
    local status=$?
    trap - EXIT
    if (( status != 0 )) && [[ -n $previous && $previous != "$revision" ]]; then
        printf 'Restoring services from %s after deployment failure\n' "$previous" >&2
        # Keep the database and all named volumes attached. Leave schema restoration to a separate operation.
        compose "$previous" up -d --no-build --no-deps imap smtp api interactive background https || return "$status"
        verify_https "$previous" || printf '%s\n' 'Previous release needs recovery' >&2
    fi
    return "$status"
}

trap rollback EXIT
compose "$revision" up -d --no-build --wait --wait-timeout 240
compose "$revision" exec -T api python - <"$release/deploy/vps/check-mail.py"
verify_https "$revision"
printf '%s\n' "$revision" >"$current_file.new"
mv -- "$current_file.new" "$current_file"
trap - EXIT
printf 'deployed %s\n' "$revision"
