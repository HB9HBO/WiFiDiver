/*
 * wifidiver-monitor – kleiner Hilfsprozess für den WLAN-Monitormodus von WifiDiver.
 *
 * Läuft mit den Datei-Capabilities CAP_NET_ADMIN und CAP_NET_RAW (siehe Makefile, "make install"),
 * damit die Oberfläche selbst nicht als root laufen muss.
 *
 * Aufgaben (bewusst nur diese):
 *   - Monitor-Interface einrichten (vorhandenes Monitor-Interface nutzen, sonst ein zusätzliches
 *     anlegen, sonst den Typ des Interfaces umschalten) und beim Beenden alles zurückstellen
 *   - Kanäle wechseln (Liste und Verweilzeit kommen über stdin)
 *   - Frames mitschneiden und gekürzt an stdout weitergeben
 *
 * Die Frames werden hier NICHT ausgewertet: Funkdaten kann jeder senden, die Auswertung passiert
 * im unprivilegierten Python-Prozess. Hier wird nur die Radiotap-Länge gelesen, um zu kürzen.
 * Nach dem Öffnen des Mitschnitts wird CAP_NET_RAW abgegeben; CAP_NET_ADMIN bleibt für den
 * Kanalwechsel und das Aufräumen.
 *
 * Protokoll stdin (Textzeilen):  F <MHz> <MHz> ...   Kanalliste (1 Eintrag = fester Kanal)
 *                                D <ms>               Verweilzeit pro Kanal (50..10000)
 *                                Q                    beenden (EOF ebenso)
 * Protokoll stdout (binär, little endian): Datensätze mit 24-Byte-Kopf, siehe struct rec.
 *
 * Lizenz: GPL-3.0-or-later (wie WifiDiver)
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/capability.h>
#include <linux/genetlink.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <linux/netlink.h>
#include <linux/nl80211.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define SNAP_MGMT 1024      /* Management-Frames (Beacons) fast vollständig */
#define SNAP_DATA 40        /* Daten: nur der MAC-Kopf */
#define SNAP_CTRL 24
#define MAX_FREQS 160

enum { R_FRAME = 1, R_CHANNEL = 2, R_MSG = 3, R_READY = 4, R_STATS = 5 };

struct rec {
    uint8_t type;
    uint8_t flags;
    uint16_t caplen;     /* Länge der folgenden Nutzdaten */
    uint32_t freq;       /* eingestellte Frequenz (MHz) */
    uint32_t origlen;    /* Originallänge des Frames */
    uint32_t aux;        /* R_CHANNEL: Verweilzeit ms; R_STATS: verworfene Pakete */
    uint64_t ts_ns;      /* CLOCK_MONOTONIC */
} __attribute__((packed));

enum mode { M_EXISTING, M_CREATED, M_SWITCHED };

static int nl_fd = -1, nl_family = -1;
static uint32_t nl_seq = 1;
static volatile sig_atomic_t stop_flag = 0;

static enum mode mode;
static int orig_ifindex, mon_ifindex, orig_iftype, orig_was_up, mon_was_up, orig_downed;
static char orig_name[IFNAMSIZ], mon_name[IFNAMSIZ];
static uint32_t wiphy;

static uint32_t freqs[MAX_FREQS];
static int n_freqs = 0, cur_idx = 0, dwell_ms = 250;
static uint32_t cur_freq = 0;

/* ------------------------------------------------------------------ Ausgabe */
static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static int out_rec(uint8_t type, uint32_t freq, uint32_t origlen, uint32_t aux, const void *data, uint16_t len)
{
    struct rec r = { type, 0, len, freq, origlen, aux, now_ns() };
    if (fwrite(&r, sizeof r, 1, stdout) != 1)
        return -1;
    if (len && fwrite(data, len, 1, stdout) != 1)
        return -1;
    return 0;
}

static void msg(const char *fmt, ...)
{
    char buf[400];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if (n >= (int)sizeof buf)
        n = sizeof buf - 1;
    out_rec(R_MSG, cur_freq, 0, 0, buf, (uint16_t)n);
    fflush(stdout);
}

/* ------------------------------------------------------------------ Netlink (ohne libnl) */
struct nlbuf {
    char data[1024];
    size_t len;
};

static void nl_start(struct nlbuf *b, uint16_t type, uint8_t cmd, uint16_t flags)
{
    memset(b, 0, sizeof *b);
    struct nlmsghdr *h = (struct nlmsghdr *)b->data;
    h->nlmsg_type = type;
    h->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | flags;
    h->nlmsg_seq = nl_seq++;
    struct genlmsghdr *g = (struct genlmsghdr *)(b->data + NLMSG_HDRLEN);
    g->cmd = cmd;
    g->version = 1;
    b->len = NLMSG_HDRLEN + GENL_HDRLEN;
}

static struct nlattr *nl_put(struct nlbuf *b, uint16_t type, const void *data, size_t len)
{
    size_t need = NLA_ALIGN(NLA_HDRLEN + len);
    if (b->len + need > sizeof b->data)
        return NULL;
    struct nlattr *a = (struct nlattr *)(b->data + b->len);
    a->nla_type = type;
    a->nla_len = NLA_HDRLEN + len;
    if (len)
        memcpy((char *)a + NLA_HDRLEN, data, len);
    b->len += need;
    return a;
}

static void nl_put_u32(struct nlbuf *b, uint16_t type, uint32_t v) { nl_put(b, type, &v, 4); }

typedef void (*nl_cb)(struct nlattr *attrs[], int max, void *arg);

/* Sendet, liest Antworten bis ACK/Fehler; ruft cb für jede Datenantwort auf. Rückgabe: 0 oder -errno */
static int nl_talk(struct nlbuf *b, nl_cb cb, int maxattr, void *arg)
{
    struct nlmsghdr *h = (struct nlmsghdr *)b->data;
    h->nlmsg_len = b->len;
    uint32_t seq = h->nlmsg_seq;
    if (send(nl_fd, b->data, b->len, 0) < 0)
        return -errno;
    char rbuf[16384];
    for (;;) {
        ssize_t n = recv(nl_fd, rbuf, sizeof rbuf, 0);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -errno;
        }
        for (struct nlmsghdr *m = (struct nlmsghdr *)rbuf; NLMSG_OK(m, (size_t)n); m = NLMSG_NEXT(m, n)) {
            if (m->nlmsg_seq != seq)
                continue;
            if (m->nlmsg_type == NLMSG_ERROR) {
                struct nlmsgerr *e = (struct nlmsgerr *)NLMSG_DATA(m);
                return e->error;
            }
            if (m->nlmsg_type == NLMSG_DONE)
                return 0;
            if (cb) {
                struct nlattr *attrs[NL80211_ATTR_MAX + 1];
                int max = maxattr < NL80211_ATTR_MAX ? maxattr : NL80211_ATTR_MAX;
                memset(attrs, 0, sizeof attrs);
                char *p = (char *)NLMSG_DATA(m) + GENL_HDRLEN;
                int rem = (int)m->nlmsg_len - NLMSG_HDRLEN - GENL_HDRLEN;
                while (rem >= NLA_HDRLEN) {
                    struct nlattr *a = (struct nlattr *)p;
                    if (a->nla_len < NLA_HDRLEN || a->nla_len > rem)
                        break;
                    int t = a->nla_type & NLA_TYPE_MASK;
                    if (t <= max)
                        attrs[t] = a;
                    rem -= NLA_ALIGN(a->nla_len);
                    p += NLA_ALIGN(a->nla_len);
                }
                cb(attrs, max, arg);
            }
        }
    }
}

static void family_cb(struct nlattr *attrs[], int max, void *arg)
{
    (void)max;
    if (attrs[CTRL_ATTR_FAMILY_ID])
        *(int *)arg = *(uint16_t *)((char *)attrs[CTRL_ATTR_FAMILY_ID] + NLA_HDRLEN);
}

static int nl_init(void)
{
    nl_fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_GENERIC);
    if (nl_fd < 0)
        return -errno;
    struct sockaddr_nl sa = { .nl_family = AF_NETLINK };
    if (bind(nl_fd, (struct sockaddr *)&sa, sizeof sa) < 0)
        return -errno;
    struct nlbuf b;
    nl_start(&b, GENL_ID_CTRL, CTRL_CMD_GETFAMILY, 0);
    nl_put(&b, CTRL_ATTR_FAMILY_NAME, "nl80211", 8);
    int id = -1;
    int err = nl_talk(&b, family_cb, CTRL_ATTR_MAX, &id);
    if (err)
        return err;
    nl_family = id;
    return id < 0 ? -ENOENT : 0;
}

struct ifinfo { int found; uint32_t iftype, wiphy; };

static void iface_cb(struct nlattr *attrs[], int max, void *arg)
{
    (void)max;
    struct ifinfo *i = arg;
    if (attrs[NL80211_ATTR_IFTYPE] && attrs[NL80211_ATTR_WIPHY]) {
        i->iftype = *(uint32_t *)((char *)attrs[NL80211_ATTR_IFTYPE] + NLA_HDRLEN);
        i->wiphy = *(uint32_t *)((char *)attrs[NL80211_ATTR_WIPHY] + NLA_HDRLEN);
        i->found = 1;
    }
}

static int get_iface(int ifindex, struct ifinfo *info)
{
    struct nlbuf b;
    memset(info, 0, sizeof *info);
    nl_start(&b, nl_family, NL80211_CMD_GET_INTERFACE, 0);
    nl_put_u32(&b, NL80211_ATTR_IFINDEX, ifindex);
    int err = nl_talk(&b, iface_cb, NL80211_ATTR_MAX, info);
    return err ? err : (info->found ? 0 : -ENODEV);
}

static void put_monitor_flags(struct nlbuf *b)
{
    /* Kontroll-Frames und Frames fremder BSS mitschneiden */
    struct nlattr *nest = nl_put(b, NL80211_ATTR_MNTR_FLAGS, NULL, 0);
    if (!nest)
        return;
    size_t start = (char *)nest - b->data;
    nl_put(b, NL80211_MNTR_FLAG_CONTROL, NULL, 0);
    nl_put(b, NL80211_MNTR_FLAG_OTHER_BSS, NULL, 0);
    ((struct nlattr *)(b->data + start))->nla_len = b->len - start;
}

static int new_monitor(uint32_t phy, const char *name)
{
    struct nlbuf b;
    nl_start(&b, nl_family, NL80211_CMD_NEW_INTERFACE, 0);
    nl_put_u32(&b, NL80211_ATTR_WIPHY, phy);
    nl_put(&b, NL80211_ATTR_IFNAME, name, strlen(name) + 1);
    nl_put_u32(&b, NL80211_ATTR_IFTYPE, NL80211_IFTYPE_MONITOR);
    put_monitor_flags(&b);
    return nl_talk(&b, NULL, 0, NULL);
}

static int del_iface(int ifindex)
{
    struct nlbuf b;
    nl_start(&b, nl_family, NL80211_CMD_DEL_INTERFACE, 0);
    nl_put_u32(&b, NL80211_ATTR_IFINDEX, ifindex);
    return nl_talk(&b, NULL, 0, NULL);
}

static int set_iftype(int ifindex, uint32_t type)
{
    struct nlbuf b;
    nl_start(&b, nl_family, NL80211_CMD_SET_INTERFACE, 0);
    nl_put_u32(&b, NL80211_ATTR_IFINDEX, ifindex);
    nl_put_u32(&b, NL80211_ATTR_IFTYPE, type);
    if (type == NL80211_IFTYPE_MONITOR)
        put_monitor_flags(&b);
    return nl_talk(&b, NULL, 0, NULL);
}

static int set_freq(int ifindex, uint32_t freq)
{
    struct nlbuf b;
    nl_start(&b, nl_family, NL80211_CMD_SET_WIPHY, 0);
    nl_put_u32(&b, NL80211_ATTR_IFINDEX, ifindex);
    nl_put_u32(&b, NL80211_ATTR_WIPHY_FREQ, freq);
    /* Kanal 14 (2484 MHz) ist nur ohne OFDM erlaubt, HT20 wird dort abgelehnt */
    nl_put_u32(&b, NL80211_ATTR_CHANNEL_WIDTH,
               freq == 2484 ? NL80211_CHAN_WIDTH_20_NOHT : NL80211_CHAN_WIDTH_20);
    nl_put_u32(&b, NL80211_ATTR_CENTER_FREQ1, freq);
    return nl_talk(&b, NULL, 0, NULL);
}

/* ------------------------------------------------------------------ Link up/down */
static int link_flags(const char *name, int *flags)
{
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0)
        return -errno;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    memcpy(ifr.ifr_name, name, strnlen(name, IFNAMSIZ - 1));
    int r = ioctl(s, SIOCGIFFLAGS, &ifr) < 0 ? -errno : 0;
    close(s);
    *flags = ifr.ifr_flags;
    return r;
}

static int link_set(const char *name, int up)
{
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (s < 0)
        return -errno;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    memcpy(ifr.ifr_name, name, strnlen(name, IFNAMSIZ - 1));
    int r = 0;
    if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0)
        r = -errno;
    else {
        if (up)
            ifr.ifr_flags |= IFF_UP;
        else
            ifr.ifr_flags &= ~IFF_UP;
        if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0)
            r = -errno;
    }
    close(s);
    return r;
}

static int is_up(const char *name)
{
    int f = 0;
    return link_flags(name, &f) == 0 && (f & IFF_UP);
}

/* ------------------------------------------------------------------ Capabilities */
static void drop_caps_except_admin(void)
{
    struct __user_cap_header_struct hdr = { _LINUX_CAPABILITY_VERSION_3, 0 };
    struct __user_cap_data_struct data[2];
    memset(data, 0, sizeof data);
    data[0].effective = data[0].permitted = 1u << CAP_NET_ADMIN;
    syscall(SYS_capset, &hdr, data);
    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
}

/* ------------------------------------------------------------------ Aufräumen */
static void cleanup(void)
{
    switch (mode) {
    case M_CREATED:
        if (mon_ifindex)
            del_iface(mon_ifindex);
        if (orig_downed)
            link_set(orig_name, 1);
        break;
    case M_SWITCHED:
        link_set(orig_name, 0);
        set_iftype(orig_ifindex, orig_iftype);
        if (orig_was_up)
            link_set(orig_name, 1);
        break;
    case M_EXISTING:
        if (!mon_was_up)
            link_set(mon_name, 0);
        break;
    }
}

static void on_signal(int sig)
{
    (void)sig;
    stop_flag = 1;
}

/* ------------------------------------------------------------------ Kanalwechsel */
static void tune(int idx)
{
    if (n_freqs <= 0)
        return;
    cur_idx = idx % n_freqs;
    uint32_t f = freqs[cur_idx];
    int err = set_freq(mon_ifindex, f);
    if (err == -EBUSY && mode == M_CREATED && !orig_downed) {
        /* Ein aktives, aber unverbundenes Interface auf demselben Gerät blockiert beim mt7921 den
         * Kanalwechsel. Für die Dauer des Mitschnitts ausschalten, beim Beenden wieder einschalten.
         * Ein verbundenes Interface (IFF_RUNNING) wird nicht angetastet. */
        int fl = 0;
        if (link_flags(orig_name, &fl) == 0 && (fl & IFF_UP) && !(fl & IFF_RUNNING)
            && link_set(orig_name, 0) == 0) {
            orig_downed = 1;
            msg("%s war aktiv (nicht verbunden) und blockierte den Kanalwechsel – für die Dauer "
                "des Monitormodus ausgeschaltet", orig_name);
            err = set_freq(mon_ifindex, f);
        }
    }
    if (err) {
        msg("Kanalwechsel auf %u MHz fehlgeschlagen: %s", f, strerror(-err));
        return;
    }
    cur_freq = f;
    out_rec(R_CHANNEL, f, 0, (uint32_t)dwell_ms, NULL, 0);
}

static void handle_line(char *line)
{
    while (*line == ' ')
        line++;
    if (line[0] == 'Q') {
        stop_flag = 1;
    } else if (line[0] == 'D') {
        long v = strtol(line + 1, NULL, 10);
        if (v >= 50 && v <= 10000)
            dwell_ms = (int)v;
    } else if (line[0] == 'F') {
        int n = 0;
        char *p = line + 1, *end;
        while (n < MAX_FREQS) {
            long v = strtol(p, &end, 10);
            if (end == p)
                break;
            if (v >= 2300 && v <= 7300)
                freqs[n++] = (uint32_t)v;
            p = end;
        }
        n_freqs = n;
        if (n)
            tune(0);
    }
}

/* ------------------------------------------------------------------ main */
static int valid_name(const char *s)
{
    size_t n = strlen(s);
    if (n == 0 || n >= IFNAMSIZ)
        return 0;
    for (; *s; s++)
        if (!isalnum((unsigned char)*s) && *s != '_' && *s != '-' && *s != '.')
            return 0;
    return 1;
}

static int fail(const char *fmt, ...)
{
    char buf[300];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    msg("%s", buf);
    fprintf(stderr, "wifidiver-monitor: %s\n", buf);
    return 1;
}

int main(int argc, char **argv)
{
    static char obuf[1 << 16];
    setvbuf(stdout, obuf, _IOFBF, sizeof obuf);
    if (argc != 2 || !valid_name(argv[1])) {
        fprintf(stderr, "Aufruf: wifidiver-monitor <wlan-interface>\n");
        return 2;
    }
    strcpy(orig_name, argv[1]);
    orig_ifindex = if_nametoindex(orig_name);
    if (!orig_ifindex)
        return fail("Interface %s nicht gefunden", orig_name);
    int err = nl_init();
    if (err)
        return fail("nl80211 nicht verfügbar: %s", strerror(-err));
    struct ifinfo info;
    err = get_iface(orig_ifindex, &info);
    if (err)
        return fail("%s ist kein WLAN-Interface: %s", orig_name, strerror(-err));
    wiphy = info.wiphy;
    orig_iftype = info.iftype;
    orig_was_up = is_up(orig_name);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    if (orig_iftype == NL80211_IFTYPE_MONITOR) {
        mode = M_EXISTING;
        strcpy(mon_name, orig_name);
        mon_ifindex = orig_ifindex;
        mon_was_up = orig_was_up;
    } else {
        snprintf(mon_name, sizeof mon_name, "wdmon%u", wiphy);
        err = new_monitor(wiphy, mon_name);
        if (err == 0) {
            mode = M_CREATED;
            mon_ifindex = if_nametoindex(mon_name);
            if (orig_was_up)
                msg("Hinweis: %s ist aktiv – falls es verbunden ist oder scannt, kann der "
                    "Kanalwechsel scheitern", orig_name);
        } else {
            /* Treiber erlaubt kein zusätzliches Interface: Typ umschalten */
            mode = M_SWITCHED;
            strcpy(mon_name, orig_name);
            mon_ifindex = orig_ifindex;
            link_set(orig_name, 0);
            err = set_iftype(orig_ifindex, NL80211_IFTYPE_MONITOR);
            if (err) {
                if (orig_was_up)
                    link_set(orig_name, 1);
                mode = M_EXISTING;
                mon_was_up = 1;
                return fail("Monitormodus für %s nicht möglich: %s", orig_name, strerror(-err));
            }
        }
        mon_was_up = 0;
    }
    err = is_up(mon_name) ? 0 : link_set(mon_name, 1);
    if (err) {
        cleanup();
        return fail("%s lässt sich nicht aktivieren: %s", mon_name, strerror(-err));
    }

    int sock = socket(AF_PACKET, SOCK_RAW | SOCK_CLOEXEC, htons(ETH_P_ALL));
    if (sock < 0) {
        cleanup();
        return fail("Mitschnitt nicht möglich: %s (Capabilities gesetzt? siehe README)", strerror(errno));
    }
    int rcv = 8 << 20;
    if (setsockopt(sock, SOL_SOCKET, SO_RCVBUFFORCE, &rcv, sizeof rcv) < 0)
        setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof rcv);
    struct sockaddr_ll sll = { .sll_family = AF_PACKET, .sll_protocol = htons(ETH_P_ALL),
                               .sll_ifindex = mon_ifindex };
    if (bind(sock, (struct sockaddr *)&sll, sizeof sll) < 0) {
        int e = errno;
        cleanup();
        return fail("Mitschnitt auf %s nicht möglich: %s", mon_name, strerror(e));
    }
    drop_caps_except_admin();
    fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);

    out_rec(R_READY, 0, (uint32_t)mode, wiphy, mon_name, (uint16_t)strlen(mon_name));
    fflush(stdout);

    static unsigned char pkt[4096];
    char line[2048];
    size_t line_len = 0;
    uint64_t next_hop = 0;
    while (!stop_flag) {
        uint64_t now = now_ns();
        if (n_freqs > 1 && now >= next_hop) {
            struct tpacket_stats st;
            socklen_t sl = sizeof st;
            if (getsockopt(sock, SOL_PACKET, PACKET_STATISTICS, &st, &sl) == 0 && st.tp_drops)
                out_rec(R_STATS, cur_freq, 0, st.tp_drops, NULL, 0);
            tune(cur_idx + 1);
            next_hop = now_ns() + (uint64_t)dwell_ms * 1000000ull;
        }
        int timeout = 200;
        if (n_freqs > 1) {
            int64_t left = (int64_t)(next_hop - now_ns()) / 1000000;
            timeout = left < 0 ? 0 : (int)left;
        }
        struct pollfd pf[2] = { { sock, POLLIN, 0 }, { STDIN_FILENO, POLLIN, 0 } };
        if (fflush(stdout) != 0)
            break;
        int pr = poll(pf, 2, timeout);
        if (pr < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        if (pf[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            ssize_t n = read(STDIN_FILENO, line + line_len, sizeof line - 1 - line_len);
            if (n == 0)
                break;              /* Oberfläche beendet */
            if (n > 0) {
                line_len += (size_t)n;
                line[line_len] = 0;
                char *nl;
                while ((nl = strchr(line, '\n'))) {
                    *nl = 0;
                    handle_line(line);
                    size_t used = (size_t)(nl - line) + 1;
                    memmove(line, nl + 1, line_len - used + 1);
                    line_len -= used;
                    if (n_freqs > 1)
                        next_hop = now_ns() + (uint64_t)dwell_ms * 1000000ull;
                }
                if (line_len >= sizeof line - 1)
                    line_len = 0;  /* überlange Zeile verwerfen */
            }
        }
        if (pf[0].revents & POLLIN) {
            for (int i = 0; i < 256; i++) {
                ssize_t n = recv(sock, pkt, sizeof pkt, MSG_DONTWAIT | MSG_TRUNC);
                if (n < 0)
                    break;
                size_t got = (size_t)n < sizeof pkt ? (size_t)n : sizeof pkt;
                if (got < 4)
                    continue;
                size_t rtlen = pkt[2] | (pkt[3] << 8);
                if (rtlen < 8 || rtlen > 512 || rtlen + 2 > got)
                    continue;
                unsigned ftype = (pkt[rtlen] >> 2) & 3;
                size_t snap = ftype == 0 ? SNAP_MGMT : ftype == 2 ? SNAP_DATA : SNAP_CTRL;
                size_t cap = rtlen + snap < got ? rtlen + snap : got;
                if (out_rec(R_FRAME, cur_freq, (uint32_t)n, 0, pkt, (uint16_t)cap) < 0) {
                    stop_flag = 1;
                    break;
                }
            }
        }
    }
    cleanup();
    return 0;
}
