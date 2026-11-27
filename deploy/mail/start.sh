#!/bin/sh
set -eu
umask 077
hash() {
    doveadm pw -s SHA512-CRYPT -p "$1"
}
printf 'bot@assembly.test:%s\nquality@assembly.test:%s\n' \
    "$(hash "${II_BOT_PASSWORD:?}")" "$(hash "${II_QUALITY_PASSWORD:?}")" >/run/ii-users
printf 'operator:%s\n' "$(hash "${II_IMAP_OPERATOR_PASSWORD:?}")" >/run/ii-master
chown dovecot:dovecot /run/ii-users /run/ii-master
exec dovecot -F
