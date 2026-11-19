# Spektrum-Server für den E310 (AD9361, Pluto-/ANTSDR-Firmware).
#
#   make                        für ARM übersetzen (statisch, braucht gcc-arm-linux-gnueabihf)
#   make install HOST=<ip>      auf das Gerät kopieren und Autostart einrichten
#   make uninstall HOST=<ip>    Server und Autostart vom Gerät entfernen

CROSS   ?= arm-linux-gnueabihf-
CC      := $(CROSS)gcc
CFLAGS  ?= -O3 -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard -Wall -Wextra
LDFLAGS ?= -static -s
HOST    ?= 192.168.10.11

# -ffast-math nur für pffft: e310d.c prüft auf Inf/NaN (keine Daten, Eingaben)
e310d: e310d.o pffft.o
	$(CC) -o $@ $^ $(LDFLAGS) -lm -lpthread

e310d.o: e310d.c pffft.h
	$(CC) $(CFLAGS) -c -o $@ $<

pffft.o: pffft.c pffft.h
	$(CC) $(CFLAGS) -ffast-math -w -c -o $@ $<

install: e310d
	./install.sh $(HOST)

uninstall:
	./install.sh --remove $(HOST)

clean:
	rm -f e310d *.o

.PHONY: install uninstall clean
