#!/bin/bash
# Chronicle Writer Wrapper — Ensures env vars are available

export NEXUS_SECRET="62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"
export TELEGRAM_BOT_TOKEN="7973500599:AAGZYc2UtQ0sa9k_CrIUuXuvisikwt1Eq4c"
export CHRONICLE_URL="http://192.168.1.42:8020"
export NEXUS_WEBHOOK_SECRET="62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd /Users/ahmedsadek/nexus
exec /usr/bin/python3 nexus-integrity/chronicle_writer.py
