# Hilfsprozess für den WLAN-Monitormodus.
#
#   make                 übersetzen
#   sudo make install    nach /usr/local/lib/wifidiver installieren und Capabilities setzen
#   sudo make uninstall  wieder entfernen
#
# Ausführen dürfen ihn nur root und die Gruppe GROUP (Debian: "netdev").

CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -D_FORTIFY_SOURCE=2 -fstack-protector-strong
PREFIX  ?= /usr/local
DEST    := $(PREFIX)/lib/wifidiver
GROUP   ?= netdev

wifidiver-monitor: wifidiver-monitor.c
	$(CC) $(CFLAGS) -o $@ $<

install: wifidiver-monitor
	install -d -m 0755 $(DEST)
	install -o root -g $(GROUP) -m 0750 wifidiver-monitor $(DEST)/wifidiver-monitor
	setcap cap_net_admin,cap_net_raw+ep $(DEST)/wifidiver-monitor
	@echo "installiert: $(DEST)/wifidiver-monitor (Gruppe $(GROUP))"

uninstall:
	rm -f $(DEST)/wifidiver-monitor
	-rmdir $(DEST)

clean:
	rm -f wifidiver-monitor

.PHONY: install uninstall clean
