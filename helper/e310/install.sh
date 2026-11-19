#!/bin/sh
# e310d auf ein Gerät mit Pluto-/ANTSDR-Firmware installieren (dauerhaft in /mnt/jffs2).
#
#   ./install.sh <adresse>            installieren bzw. aktualisieren und starten
#   ./install.sh --remove <adresse>   Server und Autostart entfernen
#
# Voraussetzung: SSH-Zugang als root (ssh-copy-id root@<adresse>, Passwort "analog").
# Die SSH-Schlüssel werden mit device_persistent_keys dauerhaft gespeichert, damit der
# Zugang ohne Passwort und der Host-Schlüssel einen Neustart überstehen.
set -eu

REMOVE=0
if [ "${1:-}" = "--remove" ]; then
    REMOVE=1
    shift
fi
HOST=${1:?Aufruf: $0 [--remove] <adresse>}
DIR=$(cd "$(dirname "$0")" && pwd)
SSH="ssh -o ConnectTimeout=5 root@$HOST"

BEGIN="# >>> wifidiver e310d"
END="# <<< wifidiver e310d"

# Autostart-Block aus autorun.sh entfernen (andere Einträge bleiben erhalten)
REMOVE_BLOCK="
if [ -f /mnt/jffs2/autorun.sh ]; then
    sed -i '/^$BEGIN\$/,/^$END\$/d' /mnt/jffs2/autorun.sh
    [ -s /mnt/jffs2/autorun.sh ] || rm -f /mnt/jffs2/autorun.sh
fi"

STOP="
kill \$(cat /var/run/e310d-loop.pid 2>/dev/null) 2>/dev/null || true
rm -f /var/run/e310d-loop.pid
killall e310d 2>/dev/null || true"

if [ "$REMOVE" = 1 ]; then
    $SSH "$STOP
$REMOVE_BLOCK
rm -f /mnt/jffs2/e310d
sync"
    echo "e310d von $HOST entfernt"
    exit 0
fi

[ -x "$DIR/e310d" ] || make -C "$DIR" e310d

# Firmware ohne sftp-server: altes scp-Protokoll verwenden
$SSH "$STOP; rm -f /mnt/jffs2/e310d"
scp -O -q "$DIR/e310d" "root@$HOST:/mnt/jffs2/e310d"

$SSH "set -e
chmod 755 /mnt/jffs2/e310d
$REMOVE_BLOCK
cat >> /mnt/jffs2/autorun.sh <<'EOF'
$BEGIN
# Spektrum-Server für WifiDiver; wird bei einem Absturz neu gestartet
( while [ -x /mnt/jffs2/e310d ]; do
      /mnt/jffs2/e310d >> /tmp/e310d.log 2>&1
      sleep 2
  done ) </dev/null >/dev/null 2>&1 &
echo \$! > /var/run/e310d-loop.pid
$END
EOF
device_persistent_keys >/dev/null
sync
# jetzt starten wie beim Booten (nur den eigenen Block)
sed -n '/^$BEGIN\$/,/^$END\$/p' /mnt/jffs2/autorun.sh | sh
sleep 1
pidof e310d >/dev/null || { echo 'e310d startet nicht:'; tail -5 /tmp/e310d.log; exit 1; }
echo \"frei auf /mnt/jffs2: \$(df -h /mnt/jffs2 | awk 'NR==2 {print \$4}')\""
echo "e310d auf $HOST installiert und gestartet (Port 7355)"
