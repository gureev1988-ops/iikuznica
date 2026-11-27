#!/bin/sh
set -eu
umask 077
if [ -f .env ]; then
    printf '%s\n' '.env already exists'
    exit 0
fi
{
    printf 'II_JUDGE_LOGIN=judge\n'
    for name in II_DB_PASSWORD II_JUDGE_PASSWORD II_BOT_PASSWORD II_QUALITY_PASSWORD II_IMAP_OPERATOR_PASSWORD; do
        printf '%s=%s\n' "$name" "$(openssl rand -hex 24)"
    done
} >.env
