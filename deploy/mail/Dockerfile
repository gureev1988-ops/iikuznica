FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends dovecot-imapd && rm -rf /var/lib/apt/lists/* \
    && install -d -m 700 -o 10001 -g 10001 /mail
COPY dovecot.conf /etc/dovecot/dovecot.conf
COPY start.sh /usr/local/bin/start-mail
ENTRYPOINT ["/bin/sh", "/usr/local/bin/start-mail"]
