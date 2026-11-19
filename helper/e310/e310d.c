/*
 * e310d – Spektrum-Server für AD9361-Geräte mit Pluto-Firmware (HamGeek E310, ANTSDR, PlutoSDR).
 *
 * Läuft auf dem Gerät, stimmt den AD9361 schrittweise ab, berechnet die gemittelten Spektren
 * (pffft, NEON) und schickt pro Sweep nur die Leistungswerte über TCP (int16, 0.01 dB).
 * Gesteuert wird direkt über sysfs/Character-Device des IIO-Treibers, ohne libiio/iiod.
 *
 * Protokoll (Port 7355):
 *   Client -> Server: Textzeilen
 *       set key=value [key=value ...]   Einstellungen (siehe apply_setting)
 *       start <lo_mhz> <hi_mhz>         Sweep starten / Bereich ändern
 *       stop                            Sweep anhalten
 *       info                            Geräteinfo anfordern
 *   Server -> Client: Rahmen  "E31D" u8 typ, u8 version, u16 0, u32 länge, nutzdaten
 *       typ 1 INFO   Text "key=value\n..."
 *       typ 2 SWEEP  u32 seq, u32 nbins, f64 f0_hz, f64 df_hz, f32 dauer_s, f32 gain_db,
 *                    int16 leistung[nbins] in 0.01 dBFS (-32768 = keine Daten)
 *       typ 3 ERROR  Text
 *       typ 4 STATE  Text (leer = alles in Ordnung)
 *   Alle Zahlen little endian.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "pffft.h"

#if defined(__ARM_NEON)
#include <arm_neon.h>
#endif

#define VERSION        1
#define DEFAULT_PORT   7355
#define NSLOTS         3
#define MAX_FFT        4096
#define MAX_AVG        512
#define MAX_LINE       1024
#define FULL_SCALE     2048.0f          /* 12-Bit-ADC, vorzeichenerweitert auf 16 Bit */
#define NO_DATA        (-32768)

enum { T_INFO = 1, T_SWEEP = 2, T_ERROR = 3, T_STATE = 4 };

static int verbose;
static void logf_(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}
#define LOG(...)  logf_(__VA_ARGS__)
#define DBG(...)  do { if (verbose) logf_(__VA_ARGS__); } while (0)

static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

/* ------------------------------------------------------------------ sysfs */
static char phy_dir[300], rx_dir[300], rx_dev[300];

static int sysfs_write(const char *dir, const char *attr, const char *val) {
    char p[400];
    snprintf(p, sizeof p, "%s/%s", dir, attr);
    int fd = open(p, O_WRONLY | O_CLOEXEC);
    if (fd < 0)
        return -errno;
    ssize_t r = write(fd, val, strlen(val));
    int e = errno;
    close(fd);
    return r < 0 ? -e : 0;
}

static int sysfs_read(const char *dir, const char *attr, char *buf, size_t n) {
    char p[400];
    snprintf(p, sizeof p, "%s/%s", dir, attr);
    int fd = open(p, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -errno;
    ssize_t r = read(fd, buf, n - 1);
    int e = errno;
    close(fd);
    if (r < 0)
        return -e;
    buf[r] = 0;
    while (r > 0 && (buf[r - 1] == '\n' || buf[r - 1] == ' '))
        buf[--r] = 0;
    return 0;
}

static int sysfs_write_ll(const char *dir, const char *attr, long long v) {
    char b[32];
    snprintf(b, sizeof b, "%lld", v);
    return sysfs_write(dir, attr, b);
}

static int find_devices(void) {
    const char *base = "/sys/bus/iio/devices";
    DIR *d = opendir(base);
    if (!d)
        return -1;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (strncmp(e->d_name, "iio:device", 10))
            continue;
        char dir[300], name[64];
        snprintf(dir, sizeof dir, "%s/%s", base, e->d_name);
        if (sysfs_read(dir, "name", name, sizeof name))
            continue;
        if (!strcmp(name, "ad9361-phy"))
            snprintf(phy_dir, sizeof phy_dir, "%s", dir);
        else if (!strcmp(name, "cf-ad9361-lpc")) {
            snprintf(rx_dir, sizeof rx_dir, "%s", dir);
            snprintf(rx_dev, sizeof rx_dev, "/dev/%s", e->d_name);
        }
    }
    closedir(d);
    return phy_dir[0] && rx_dir[0] ? 0 : -1;
}

/* ------------------------------------------------------------------ Konfiguration */
typedef struct {
    double lo_mhz, hi_mhz;
    double rate_msps;     /* Abtastrate */
    double bw_mhz;        /* analoge Bandbreite, 0 = Abtastrate */
    int fft;              /* FFT-Größe */
    int avg;              /* gemittelte FFT-Blöcke pro Schritt */
    double usable;        /* genutzter Anteil der Abtastrate pro Schritt */
    double settle_us;     /* nach dem Umstimmen verworfen */
    char gain_mode[16];   /* manual, slow_attack, fast_attack, hybrid */
    double gain_db;
    char port[16];        /* RF-Port, leer = nicht ändern */
    double max_rate;      /* Sweeps/s, 0 = frei */
    int max_bins;         /* höchstens so viele Werte pro Sweep (Max-Pooling), 0 = alle */
} config_t;

static const config_t default_cfg = {
    .lo_mhz = 2400, .hi_mhz = 2500, .rate_msps = 30.72, .bw_mhz = 0, .fft = 1024, .avg = 16,
    .usable = 0.75, .settle_us = 100, .gain_mode = "manual", .gain_db = 50, .port = "",
    .max_rate = 25, .max_bins = 4096,
};

/* Gemeinsamer Zustand, geschützt durch mtx */
static pthread_mutex_t mtx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static config_t cfg;
static unsigned cfg_gen = 1;     /* erhöht bei Änderungen, die einen neuen Plan erfordern */
static unsigned live_gen = 1;    /* erhöht bei Gain-Änderungen */
static int running;              /* Sweep aktiv */
static volatile sig_atomic_t quitting;
static char state_msg[256];      /* an den Client zu meldender Zustand */
static int state_dirty;
static char error_msg[256];
static int error_dirty;

/* Rahmen, der vom FFT-Thread an den Netzwerk-Thread geht (immer nur der neueste) */
static uint8_t *pending_frame;
static size_t pending_len;
static int wake_pipe[2];

static void wake_main(void) {
    char c = 1;
    if (write(wake_pipe[1], &c, 1) < 0) { /* Pipe voll: Haupt-Thread wird ohnehin geweckt */ }
}

static void set_state(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    pthread_mutex_lock(&mtx);
    char tmp[sizeof state_msg];
    vsnprintf(tmp, sizeof tmp, fmt, ap);
    if (strcmp(tmp, state_msg)) {
        strcpy(state_msg, tmp);
        state_dirty = 1;
    }
    pthread_mutex_unlock(&mtx);
    va_end(ap);
    wake_main();
}

static void set_error(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    pthread_mutex_lock(&mtx);
    vsnprintf(error_msg, sizeof error_msg, fmt, ap);
    error_dirty = 1;
    pthread_mutex_unlock(&mtx);
    va_end(ap);
    LOG("Fehler: %s", error_msg);
    wake_main();
}

/* ------------------------------------------------------------------ Sweep-Plan */
typedef struct {
    unsigned gen;
    double rate_hz, bin_hz;
    int fft, avg, keep;          /* keep: genutzte Bins pro Schritt (gerade) */
    int nsteps;
    double f0_hz;                /* Mitte des ersten Bins des Sweeps */
    int nbins;                   /* Bins im Bereich lo..hi */
    int discard;                 /* Samples nach dem Umstimmen verwerfen */
    int total;                   /* Samples pro Aufnahme (discard + fft*avg, ausgerichtet) */
    double gain_db;
    int agc;
    double max_rate;
    int max_bins;
} plan_t;

static double lo_min_hz = 70e6, lo_max_hz = 6000e6;   /* aus out_altvoltage0_RX_LO_frequency_available */

/* Versatz in Bins, um den die Abstimmfrequenz eines Schritts verschoben werden muss, damit sie im
   Bereich des Geräts liegt (z.B. letzter Schritt bei einem Sweep bis 6000 MHz). */
static int step_shift(const plan_t *p, int step) {
    double ideal = p->f0_hz + (step * (double)p->keep + p->keep / 2) * p->bin_hz;
    if (ideal > lo_max_hz)
        return (int)ceil((ideal - lo_max_hz) / p->bin_hz);
    if (ideal < lo_min_hz)
        return -(int)ceil((lo_min_hz - ideal) / p->bin_hz);
    return 0;
}

static long long step_lo_hz(const plan_t *p, int step) {
    /* Mittenfrequenz: genau auf dem Bin-Raster, damit die Schritte lückenlos aneinanderpassen */
    return llround(p->f0_hz + (step * (double)p->keep + p->keep / 2 - step_shift(p, step)) * p->bin_hz);
}

static void make_plan(const config_t *c, plan_t *p, unsigned gen) {
    memset(p, 0, sizeof *p);
    p->gen = gen;
    p->rate_hz = c->rate_msps * 1e6;
    p->fft = c->fft;
    p->avg = c->avg;
    p->bin_hz = p->rate_hz / p->fft;
    int keep = (int)(c->usable * p->fft) & ~1;
    if (keep < 16)
        keep = 16;
    if (keep > p->fft - 2)
        keep = p->fft - 2;
    p->keep = keep;
    double lo = c->lo_mhz * 1e6, hi = c->hi_mhz * 1e6;
    p->nbins = (int)ceil((hi - lo) / p->bin_hz);
    if (p->nbins < 1)
        p->nbins = 1;
    p->f0_hz = lo + p->bin_hz / 2;
    p->nsteps = (p->nbins + keep - 1) / keep;
    p->discard = (int)(c->settle_us * 1e-6 * p->rate_hz);
    int total = p->discard + p->fft * p->avg;
    p->total = (total + 7) & ~7;
    p->discard = p->total - p->fft * p->avg;
    p->gain_db = c->gain_db;
    p->agc = strcmp(c->gain_mode, "manual") != 0;
    p->max_rate = c->max_rate;
    p->max_bins = c->max_bins;
}

/* ------------------------------------------------------------------ Aufnahme -> FFT */
typedef struct {
    int16_t *iq;                 /* total * 2 */
    int step;
    double gain_db;              /* tatsächliche Verstärkung bei der Aufnahme */
    double t_start;              /* Beginn des Sweeps */
    plan_t plan;
    int full;                    /* 1 = wartet auf FFT */
} slot_t;

static slot_t slots[NSLOTS];
static int slot_cap;             /* Samples pro Slot (Kapazität) */

static int ensure_slot_capacity(int total) {
    if (total <= slot_cap)
        return 0;
    for (int i = 0; i < NSLOTS; i++) {
        void *n = realloc(slots[i].iq, (size_t)total * 4);
        if (!n)
            return -1;
        slots[i].iq = n;
    }
    slot_cap = total;
    return 0;
}

/* ------------------------------------------------------------------ Gerät */
static int rx_fd = -1;
static int buf_len_set;
static char saved_txlo_pd[16];

static int apply_hw(const plan_t *p, const config_t *c) {
    int r;
    if ((r = sysfs_write_ll(phy_dir, "in_voltage_sampling_frequency", llround(p->rate_hz)))) {
        set_error("Abtastrate %.3f MS/s nicht möglich (%s)", p->rate_hz / 1e6, strerror(-r));
        return -1;
    }
    double bw = c->bw_mhz > 0 ? c->bw_mhz * 1e6 : p->rate_hz;
    if (bw > 56e6)
        bw = 56e6;
    sysfs_write_ll(phy_dir, "in_voltage_rf_bandwidth", llround(bw));
    if (c->port[0] && (r = sysfs_write(phy_dir, "in_voltage0_rf_port_select", c->port)))
        set_error("RF-Port %s nicht möglich (%s)", c->port, strerror(-r));
    if ((r = sysfs_write(phy_dir, "in_voltage0_gain_control_mode", c->gain_mode)))
        set_error("Gain-Modus %s nicht möglich (%s)", c->gain_mode, strerror(-r));
    if (!p->agc) {
        char b[32];
        snprintf(b, sizeof b, "%.0f", c->gain_db);
        sysfs_write(phy_dir, "in_voltage0_hardwaregain", b);
    }
    /* Kanal 0 (I/Q von RX1) aktivieren */
    sysfs_write(rx_dir, "buffer/enable", "0");
    sysfs_write(rx_dir, "scan_elements/in_voltage0_en", "1");
    sysfs_write(rx_dir, "scan_elements/in_voltage1_en", "1");
    sysfs_write(rx_dir, "scan_elements/in_voltage2_en", "0");
    sysfs_write(rx_dir, "scan_elements/in_voltage3_en", "0");
    if ((r = sysfs_write_ll(rx_dir, "buffer/length", p->total))) {
        set_error("Pufferlänge %d nicht möglich (%s)", p->total, strerror(-r));
        return -1;
    }
    buf_len_set = p->total;
    return 0;
}

static void apply_live(const config_t *c) {
    sysfs_write(phy_dir, "in_voltage0_gain_control_mode", c->gain_mode);
    if (!strcmp(c->gain_mode, "manual")) {
        char b[32];
        snprintf(b, sizeof b, "%.0f", c->gain_db);
        sysfs_write(phy_dir, "in_voltage0_hardwaregain", b);
    }
}

static void radio_acquire(void) {
    /* TX-LO abschalten: steht bei Pluto-Firmware auf 2.45 GHz und wäre im Spektrum zu sehen */
    if (!saved_txlo_pd[0] && !sysfs_read(phy_dir, "out_altvoltage1_TX_LO_powerdown", saved_txlo_pd,
                                         sizeof saved_txlo_pd))
        sysfs_write(phy_dir, "out_altvoltage1_TX_LO_powerdown", "1");
}

static void radio_release(void) {
    sysfs_write(rx_dir, "buffer/enable", "0");
    if (saved_txlo_pd[0]) {
        sysfs_write(phy_dir, "out_altvoltage1_TX_LO_powerdown", saved_txlo_pd);
        saved_txlo_pd[0] = 0;
    }
}

/* Liest genau n Samples (I/Q int16) nach dem Einschalten des Puffers. */
static int capture(int16_t *dst, int n) {
    int r = sysfs_write(rx_dir, "buffer/enable", "1");
    if (r) {
        set_error("RX-Puffer lässt sich nicht einschalten (%s) – benutzt ein anderes Programm "
                  "(iiod/Client) das Gerät?", strerror(-r));
        return -1;
    }
    size_t want = (size_t)n * 4, got = 0;
    while (got < want) {
        struct pollfd pfd = { rx_fd, POLLIN, 0 };
        int pr = poll(&pfd, 1, 1000);
        if (pr == 0) {
            sysfs_write(rx_dir, "buffer/enable", "0");
            set_state("Zeitüberschreitung beim Lesen der Samples");
            return -2;
        }
        ssize_t rd = read(rx_fd, (char *)dst + got, want - got);
        if (rd < 0) {
            if (errno == EINTR || errno == EAGAIN)
                continue;
            int e = errno;
            sysfs_write(rx_dir, "buffer/enable", "0");
            set_error("Lesen der Samples: %s", strerror(e));
            return -1;
        }
        got += rd;
    }
    sysfs_write(rx_dir, "buffer/enable", "0");
    return 0;
}

static double read_gain(void) {
    char b[32];
    if (sysfs_read(phy_dir, "in_voltage0_hardwaregain", b, sizeof b))
        return NAN;
    return atof(b);
}

/* ------------------------------------------------------------------ Aufnahme-Thread */
static int slot_put_idx, slot_get_idx;

static void *capture_thread(void *arg) {
    (void)arg;
    plan_t plan;
    config_t c;
    unsigned have_gen = 0, have_live = 0;
    long long tuned = -1;
    int step = 0;
    double t_sweep = 0, last_sweep_start = 0;
    int fails = 0;

    for (;;) {
        pthread_mutex_lock(&mtx);
        while (!quitting && !running) {
            if (have_gen) {        /* gerade angehalten */
                pthread_mutex_unlock(&mtx);
                radio_release();
                pthread_mutex_lock(&mtx);
                have_gen = 0;
                tuned = -1;
            }
            pthread_cond_wait(&cond, &mtx);
        }
        if (quitting) {
            pthread_mutex_unlock(&mtx);
            break;
        }
        int replan = have_gen != cfg_gen;
        int relive = have_live != live_gen;
        c = cfg;
        unsigned gen = cfg_gen;
        have_live = live_gen;
        pthread_mutex_unlock(&mtx);

        if (replan) {
            make_plan(&c, &plan, gen);
            if (ensure_slot_capacity(plan.total)) {
                set_error("zu wenig Speicher");
                pthread_mutex_lock(&mtx);
                running = 0;
                pthread_mutex_unlock(&mtx);
                continue;
            }
            radio_acquire();
            if (apply_hw(&plan, &c)) {
                pthread_mutex_lock(&mtx);
                running = 0;
                pthread_mutex_unlock(&mtx);
                continue;
            }
            have_gen = gen;
            tuned = -1;
            step = 0;
            DBG("Plan: %d Schritte × %d Bins, %d Samples/Schritt, Bin %.1f kHz",
                plan.nsteps, plan.keep, plan.total, plan.bin_hz / 1e3);
        } else if (relive) {
            apply_live(&c);
            plan.gain_db = c.gain_db;
            plan.agc = strcmp(c.gain_mode, "manual") != 0;
            plan.max_rate = c.max_rate;
        }

        if (step == 0) {
            if (plan.max_rate > 0 && last_sweep_start > 0) {
                double rest = last_sweep_start + 1.0 / plan.max_rate - now_s();
                if (rest > 0) {
                    struct timespec ts = { (time_t)rest, (long)((rest - (time_t)rest) * 1e9) };
                    nanosleep(&ts, NULL);
                }
            }
            t_sweep = last_sweep_start = now_s();
        }

        long long fc = step_lo_hz(&plan, step);
        if (fc != tuned) {
            int r = sysfs_write_ll(phy_dir, "out_altvoltage0_RX_LO_frequency", fc);
            if (r) {
                set_error("Abstimmen auf %.3f MHz fehlgeschlagen (%s)", fc / 1e6, strerror(-r));
                pthread_mutex_lock(&mtx);
                running = 0;
                pthread_mutex_unlock(&mtx);
                continue;
            }
            tuned = fc;
        }

        /* freien Slot holen */
        pthread_mutex_lock(&mtx);
        while (slots[slot_put_idx].full && running && !quitting)
            pthread_cond_wait(&cond, &mtx);
        if (!running || quitting) {
            pthread_mutex_unlock(&mtx);
            continue;
        }
        slot_t *s = &slots[slot_put_idx];
        pthread_mutex_unlock(&mtx);

        int r = capture(s->iq, plan.total);
        if (r == -1) {
            pthread_mutex_lock(&mtx);
            running = 0;
            pthread_mutex_unlock(&mtx);
            continue;
        }
        if (r == -2) {
            if (++fails > 5) {
                set_error("Keine Samples vom AD9361");
                pthread_mutex_lock(&mtx);
                running = 0;
                pthread_mutex_unlock(&mtx);
                fails = 0;
            }
            continue;
        }
        if (fails) {
            fails = 0;
            set_state("");
        }
        s->step = step;
        s->plan = plan;
        s->t_start = t_sweep;
        s->gain_db = plan.agc ? read_gain() : plan.gain_db;

        pthread_mutex_lock(&mtx);
        s->full = 1;
        slot_put_idx = (slot_put_idx + 1) % NSLOTS;
        pthread_cond_broadcast(&cond);
        pthread_mutex_unlock(&mtx);

        step = (step + 1) % plan.nsteps;
    }
    radio_release();
    return NULL;
}

/* ------------------------------------------------------------------ FFT-Thread */
static void *fft_thread(void *arg) {
    (void)arg;
    PFFFT_Setup *setup = NULL;
    int setup_n = 0;
    float *in = pffft_aligned_malloc(MAX_FFT * 2 * sizeof(float));
    float *out = pffft_aligned_malloc(MAX_FFT * 2 * sizeof(float));
    float *work = pffft_aligned_malloc(MAX_FFT * 2 * sizeof(float));
    float *acc = pffft_aligned_malloc(MAX_FFT * sizeof(float));
    float *win2 = pffft_aligned_malloc(MAX_FFT * 2 * sizeof(float));   /* Fenster, je I und Q */
    float *sweep = NULL;          /* dB pro Bin des ganzen Sweeps */
    int sweep_cap = 0;
    unsigned cur_gen = 0;
    int win_n = 0;
    float norm_db = 0;
    int next_step = 0;
    uint32_t seq = 0;
    double gain_ref = 0;

    for (;;) {
        pthread_mutex_lock(&mtx);
        while (!quitting && !slots[slot_get_idx].full)
            pthread_cond_wait(&cond, &mtx);
        if (quitting) {
            pthread_mutex_unlock(&mtx);
            break;
        }
        slot_t *s = &slots[slot_get_idx];
        int still_running = running;
        pthread_mutex_unlock(&mtx);

        const plan_t *p = &s->plan;
        if (!still_running)
            goto release;

        if (p->gen != cur_gen) {
            cur_gen = p->gen;
            if (setup_n != p->fft) {
                if (setup)
                    pffft_destroy_setup(setup);
                setup = pffft_new_setup(p->fft, PFFFT_COMPLEX);
                setup_n = p->fft;
            }
            if (win_n != p->fft) {
                /* Blackman, wie das UHD-Backend */
                double sum = 0;
                for (int i = 0; i < p->fft; i++) {
                    double x = 2 * M_PI * i / (p->fft - 1);
                    double w = 0.42 - 0.5 * cos(x) + 0.08 * cos(2 * x);
                    win2[2 * i] = win2[2 * i + 1] = (float)(w / FULL_SCALE);
                    sum += w;
                }
                norm_db = (float)(-10.0 * log10(sum * sum));
                win_n = p->fft;
            }
            int need = p->nsteps * p->keep;
            if (need > sweep_cap) {
                free(sweep);
                sweep = malloc((size_t)need * sizeof(float));
                sweep_cap = sweep ? need : 0;
            }
            next_step = 0;
        }
        if (!sweep || !setup)
            goto release;
        if (s->step != next_step) {      /* nach Neuplanung: auf Sweep-Anfang warten */
            if (s->step != 0)
                goto release;
            next_step = 0;
        }
        if (s->step == 0)
            gain_ref = p->gain_db;

        /* Welch-Mittelung */
        memset(acc, 0, p->fft * sizeof(float));
        const int16_t *iq = s->iq + 2 * p->discard;
        for (int b = 0; b < p->avg; b++, iq += 2 * p->fft) {
            int n2 = 2 * p->fft;
#if defined(__ARM_NEON)
            for (int i = 0; i < n2; i += 8) {
                int16x8_t v = vld1q_s16(iq + i);
                float32x4_t a = vcvtq_f32_s32(vmovl_s16(vget_low_s16(v)));
                float32x4_t c2 = vcvtq_f32_s32(vmovl_s16(vget_high_s16(v)));
                vst1q_f32(in + i, vmulq_f32(a, vld1q_f32(win2 + i)));
                vst1q_f32(in + i + 4, vmulq_f32(c2, vld1q_f32(win2 + i + 4)));
            }
#else
            for (int i = 0; i < n2; i++)
                in[i] = iq[i] * win2[i];
#endif
            pffft_transform_ordered(setup, in, out, work, PFFFT_FORWARD);
#if defined(__ARM_NEON)
            for (int i = 0; i < p->fft; i += 4) {
                float32x4x2_t v = vld2q_f32(out + 2 * i);
                float32x4_t a = vld1q_f32(acc + i);
                a = vmlaq_f32(a, v.val[0], v.val[0]);
                a = vmlaq_f32(a, v.val[1], v.val[1]);
                vst1q_f32(acc + i, a);
            }
#else
            for (int i = 0; i < p->fft; i++)
                acc[i] += out[2 * i] * out[2 * i] + out[2 * i + 1] * out[2 * i + 1];
#endif
        }

        {
            /* Bins um die Abstimmfrequenz (fftshift: Index fft/2 ab Mitte) */
            float off = norm_db - 10.0f * log10f((float)p->avg);
            if (p->agc && isfinite(s->gain_db))
                off += (float)(gain_ref - s->gain_db);    /* auf eingestellte Verstärkung beziehen */
            float *dst = sweep + (size_t)s->step * p->keep;
            int shift = step_shift(p, s->step);
            for (int k = 0; k < p->keep; k++) {
                int m = k - p->keep / 2 + shift;           /* Bin-Offset zur LO-Frequenz */
                if (m <= -p->fft / 2 || m >= p->fft / 2) {
                    dst[k] = -INFINITY;                    /* außerhalb des Geräts */
                    continue;
                }
                int idx = m < 0 ? p->fft + m : m;          /* unshifted Index */
                dst[k] = 10.0f * log10f(acc[idx] + 1e-20f) + off;
            }
            /* DC-Spitze durch lineare Interpolation ersetzen */
            int dc = p->fft / 512;
            if (dc < 1)
                dc = 1;
            int c0 = p->keep / 2 - shift - dc, c1 = p->keep / 2 - shift + dc;
            if (c0 > 0 && c1 < p->keep - 1) {
                float a = dst[c0 - 1], b = dst[c1 + 1];
                for (int k = c0; k <= c1; k++)
                    dst[k] = a + (b - a) * (k - c0 + 1) / (float)(c1 - c0 + 2);
            }
        }
        next_step = s->step + 1;

        if (s->step == p->nsteps - 1) {
            /* Sweep fertig: auf max_bins reduzieren und als Rahmen ablegen */
            int n = p->nbins;
            int k = 1;
            if (p->max_bins > 0 && n > p->max_bins)
                k = (n + p->max_bins - 1) / p->max_bins;
            int nout = (n + k - 1) / k;
            size_t plen = 32 + (size_t)nout * 2;
            uint8_t *fr = malloc(12 + plen);
            if (fr) {
                memcpy(fr, "E31D", 4);
                fr[4] = T_SWEEP;
                fr[5] = VERSION;
                fr[6] = fr[7] = 0;
                uint32_t len = (uint32_t)plen;
                memcpy(fr + 8, &len, 4);
                uint8_t *pl = fr + 12;
                uint32_t nb = nout;
                double f0 = p->f0_hz + (k - 1) / 2.0 * p->bin_hz, df = p->bin_hz * k;
                float dur = (float)(now_s() - s->t_start), g = (float)gain_ref;
                seq++;
                memcpy(pl, &seq, 4);
                memcpy(pl + 4, &nb, 4);
                memcpy(pl + 8, &f0, 8);
                memcpy(pl + 16, &df, 8);
                memcpy(pl + 24, &dur, 4);
                memcpy(pl + 28, &g, 4);
                int16_t *v = (int16_t *)(pl + 32);
                for (int o = 0; o < nout; o++) {
                    float m = -INFINITY;
                    int end = (o + 1) * k < n ? (o + 1) * k : n;
                    for (int i = o * k; i < end; i++)
                        if (sweep[i] > m)
                            m = sweep[i];
                    float x = roundf(m * 100.0f);
                    v[o] = !isfinite(m) ? NO_DATA : x < -32767 ? -32767 : x > 32767 ? 32767 : (int16_t)x;
                }
                pthread_mutex_lock(&mtx);
                if (running && p->gen == cfg_gen) {
                    free(pending_frame);           /* Netz kommt nicht nach: alten verwerfen */
                    pending_frame = fr;
                    pending_len = 12 + plen;
                } else {
                    free(fr);                      /* Bereich/Plan inzwischen geändert */
                    fr = NULL;
                }
                pthread_mutex_unlock(&mtx);
                if (fr)
                    wake_main();
            }
            next_step = 0;
        }

release:
        pthread_mutex_lock(&mtx);
        s->full = 0;
        slot_get_idx = (slot_get_idx + 1) % NSLOTS;
        pthread_cond_broadcast(&cond);
        pthread_mutex_unlock(&mtx);
    }
    if (setup)
        pffft_destroy_setup(setup);
    return NULL;
}

/* ------------------------------------------------------------------ Netzwerk */
static int client = -1;
static uint8_t *outbuf;
static size_t out_len, out_off;
static char inbuf[MAX_LINE];
static size_t in_len;

static int queue_frame(int type, const void *data, size_t n) {
    if (client < 0)
        return -1;
    uint8_t *nb = realloc(outbuf, out_len + 12 + n);
    if (!nb)
        return -1;
    outbuf = nb;
    uint8_t *f = outbuf + out_len;
    memcpy(f, "E31D", 4);
    f[4] = (uint8_t)type;
    f[5] = VERSION;
    f[6] = f[7] = 0;
    uint32_t len = (uint32_t)n;
    memcpy(f + 8, &len, 4);
    memcpy(f + 12, data, n);
    out_len += 12 + n;
    return 0;
}

static void queue_text(int type, const char *s) {
    queue_frame(type, s, strlen(s));
}

static void send_info(void) {
    char b[1024], model[128] = "", rng[64] = "", rates[64] = "", gains[64] = "", ports[256] = "";
    sysfs_read("/sys/firmware/devicetree/base", "model", model, sizeof model);
    sysfs_read(phy_dir, "out_altvoltage0_RX_LO_frequency_available", rng, sizeof rng);
    sysfs_read(phy_dir, "in_voltage_sampling_frequency_available", rates, sizeof rates);
    sysfs_read(phy_dir, "in_voltage0_hardwaregain_available", gains, sizeof gains);
    sysfs_read(phy_dir, "in_voltage_rf_port_select_available", ports, sizeof ports);
    long long fmin = 0, fmax = 0, step = 0;
    sscanf(rng, "[%lld %lld %lld]", &fmin, &step, &fmax);
    long long rmin = 0, rmax = 0;
    sscanf(rates, "[%lld %lld %lld]", &rmin, &step, &rmax);
    snprintf(b, sizeof b,
             "version=%d\nmodel=%s\nfmin_mhz=%.3f\nfmax_mhz=%.3f\nrate_min_msps=%.3f\n"
             "rate_max_msps=%.3f\ngain_range=%s\nports=%s\nmax_fft=%d\n",
             VERSION, model, fmin / 1e6, fmax / 1e6, rmin / 1e6, rmax / 1e6, gains, ports, MAX_FFT);
    queue_text(T_INFO, b);
}

static int is_pow2(int n) { return n >= 64 && n <= MAX_FFT && !(n & (n - 1)); }

/* Einstellung übernehmen; Rückgabe: 0 = ok, 1 = live, -1 = Fehler */
static int apply_setting(config_t *c, const char *key, const char *val) {
    char *end;
    double d = strtod(val, &end);
    int num = end != val && *end == 0 && isfinite(d);
    if (!strcmp(key, "rate") && num && d >= 2.1 && d <= 61.44)      c->rate_msps = d;
    else if (!strcmp(key, "bw") && num && d >= 0 && d <= 56)        c->bw_mhz = d;
    else if (!strcmp(key, "fft") && num && is_pow2((int)d))         c->fft = (int)d;
    else if (!strcmp(key, "avg") && num && d >= 1 && d <= MAX_AVG)  c->avg = (int)d;
    else if (!strcmp(key, "usable") && num && d >= 0.1 && d <= 0.98) c->usable = d;
    else if (!strcmp(key, "settle") && num && d >= 0 && d <= 100000) c->settle_us = d;
    else if (!strcmp(key, "max_bins") && num && d >= 0 && d <= 1000000) c->max_bins = (int)d;
    else if (!strcmp(key, "port") && strlen(val) < sizeof c->port && !strchr(val, '/'))
        snprintf(c->port, sizeof c->port, "%s", val);
    else if (!strcmp(key, "gain") && num && d >= -10 && d <= 80) { c->gain_db = d; return 1; }
    else if (!strcmp(key, "max_rate") && num && d >= 0 && d <= 1000) { c->max_rate = d; return 1; }
    else if (!strcmp(key, "gain_mode") && (!strcmp(val, "manual") || !strcmp(val, "slow_attack") ||
                                           !strcmp(val, "fast_attack") || !strcmp(val, "hybrid"))) {
        snprintf(c->gain_mode, sizeof c->gain_mode, "%s", val);
        return 1;
    } else
        return -1;
    return 0;
}

static void handle_line(char *line) {
    char *save = NULL, *cmd = strtok_r(line, " \t\r", &save);
    if (!cmd)
        return;
    DBG("< %s", cmd);
    if (!strcmp(cmd, "set")) {
        char *kv;
        pthread_mutex_lock(&mtx);
        config_t c = cfg;
        int re = 0, live = 0;
        char bad[128] = "";
        while ((kv = strtok_r(NULL, " \t\r", &save))) {
            char *eq = strchr(kv, '=');
            if (!eq) {
                snprintf(bad, sizeof bad, "%s", kv);
                continue;
            }
            *eq = 0;
            int r = apply_setting(&c, kv, eq + 1);
            if (r < 0)
                snprintf(bad, sizeof bad, "%s=%s", kv, eq + 1);
            else if (r == 1)
                live = 1;
            else
                re = 1;
        }
        cfg = c;
        if (re)
            cfg_gen++;
        if (live)
            live_gen++;
        pthread_cond_broadcast(&cond);
        pthread_mutex_unlock(&mtx);
        if (bad[0]) {
            char m[192];
            snprintf(m, sizeof m, "ungültige Einstellung: %s", bad);
            queue_text(T_ERROR, m);
        }
    } else if (!strcmp(cmd, "start")) {
        char *a = strtok_r(NULL, " \t\r", &save), *b = strtok_r(NULL, " \t\r", &save);
        double lo = a ? atof(a) : 0, hi = b ? atof(b) : 0;
        if (!(hi > lo && lo > 0 && hi - lo <= 6000)) {
            queue_text(T_ERROR, "start: ungültiger Bereich");
            return;
        }
        pthread_mutex_lock(&mtx);
        cfg.lo_mhz = lo;
        cfg.hi_mhz = hi;
        cfg_gen++;
        running = 1;
        pthread_cond_broadcast(&cond);
        pthread_mutex_unlock(&mtx);
    } else if (!strcmp(cmd, "stop")) {
        pthread_mutex_lock(&mtx);
        running = 0;
        pthread_cond_broadcast(&cond);
        pthread_mutex_unlock(&mtx);
    } else if (!strcmp(cmd, "info")) {
        send_info();
    } else {
        queue_text(T_ERROR, "unbekannter Befehl");
    }
}

static void drop_client(void) {
    if (client < 0)
        return;
    LOG("Client getrennt");
    close(client);
    client = -1;
    out_len = out_off = in_len = 0;
    pthread_mutex_lock(&mtx);
    running = 0;
    cfg = default_cfg;           /* nächster Client beginnt mit Standardwerten */
    cfg_gen++;
    live_gen++;
    free(pending_frame);
    pending_frame = NULL;
    pthread_cond_broadcast(&cond);
    pthread_mutex_unlock(&mtx);
}

static void on_signal(int sig) {
    (void)sig;
    quitting = 1;
    wake_main();
}

static void usage(const char *prog) {
    fprintf(stderr, "Aufruf: %s [-p port] [-b adresse] [-v]\n", prog);
}

int main(int argc, char **argv) {
    int port = DEFAULT_PORT, opt;
    const char *bind_addr = NULL;
    while ((opt = getopt(argc, argv, "p:b:vh")) != -1) {
        switch (opt) {
        case 'p': port = atoi(optarg); break;
        case 'b': bind_addr = optarg; break;
        case 'v': verbose = 1; break;
        default: usage(argv[0]); return opt == 'h' ? 0 : 2;
        }
    }
    if (find_devices()) {
        LOG("AD9361 (ad9361-phy / cf-ad9361-lpc) nicht gefunden");
        return 1;
    }
    {
        char rng[64];
        long long a, st, b;
        if (!sysfs_read(phy_dir, "out_altvoltage0_RX_LO_frequency_available", rng, sizeof rng) &&
            sscanf(rng, "[%lld %lld %lld]", &a, &st, &b) == 3 && b > a) {
            lo_min_hz = a;
            lo_max_hz = b;
        }
    }
    rx_fd = open(rx_dev, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (rx_fd < 0) {
        LOG("%s: %s", rx_dev, strerror(errno));
        return 1;
    }
    cfg = default_cfg;
    if (pipe2(wake_pipe, O_CLOEXEC | O_NONBLOCK)) {
        LOG("pipe: %s", strerror(errno));
        return 1;
    }
    struct sigaction sa = { 0 };
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    int ls = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in sin = { .sin_family = AF_INET, .sin_port = htons(port) };
    sin.sin_addr.s_addr = bind_addr ? inet_addr(bind_addr) : htonl(INADDR_ANY);
    if (bind(ls, (struct sockaddr *)&sin, sizeof sin) || listen(ls, 2)) {
        LOG("Port %d: %s", port, strerror(errno));
        return 1;
    }
    LOG("e310d %d: %s, %s, Port %d", VERSION, phy_dir, rx_dev, port);

    pthread_t th_cap, th_fft;
    pthread_create(&th_cap, NULL, capture_thread, NULL);
    pthread_create(&th_fft, NULL, fft_thread, NULL);

    while (!quitting) {
        struct pollfd pfd[3] = {
            { ls, POLLIN, 0 },
            { wake_pipe[0], POLLIN, 0 },
            { client, (short)(POLLIN | (out_off < out_len ? POLLOUT : 0)), 0 },
        };
        int r = poll(pfd, client >= 0 ? 3 : 2, 1000);
        if (r < 0 && errno != EINTR)
            break;
        if (pfd[1].revents & POLLIN) {
            char tmp[64];
            while (read(wake_pipe[0], tmp, sizeof tmp) > 0) { }
        }
        if (pfd[0].revents & POLLIN) {
            struct sockaddr_in peer;
            socklen_t pl = sizeof peer;
            int c = accept4(ls, (struct sockaddr *)&peer, &pl, SOCK_CLOEXEC | SOCK_NONBLOCK);
            if (c >= 0) {
                if (client >= 0) {
                    LOG("neuer Client ersetzt den bisherigen");
                    drop_client();
                }
                client = c;
                setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                LOG("Client %s:%d verbunden", inet_ntoa(peer.sin_addr), ntohs(peer.sin_port));
                send_info();
            }
        }
        if (client >= 0 && (pfd[2].revents & (POLLERR | POLLHUP)) && !(pfd[2].revents & POLLIN)) {
            drop_client();
        } else if (client >= 0 && (pfd[2].revents & POLLIN)) {
            ssize_t n = recv(client, inbuf + in_len, sizeof inbuf - 1 - in_len, 0);
            if (n <= 0 && !(n < 0 && (errno == EAGAIN || errno == EINTR))) {
                drop_client();
            } else if (n > 0) {
                in_len += n;
                char *nl;
                while (client >= 0 && (nl = memchr(inbuf, '\n', in_len))) {
                    *nl = 0;
                    handle_line(inbuf);
                    size_t used = nl + 1 - inbuf;
                    memmove(inbuf, nl + 1, in_len - used);
                    in_len -= used;
                }
                if (in_len >= sizeof inbuf - 1) {
                    queue_text(T_ERROR, "Zeile zu lang");
                    in_len = 0;
                }
            }
        }
        /* Meldungen und fertige Sweeps einreihen */
        if (client >= 0) {
            pthread_mutex_lock(&mtx);
            char msg[256];
            int have_err = error_dirty, have_state = state_dirty;
            if (have_err) {
                strcpy(msg, error_msg);
                error_dirty = 0;
            }
            char st[256];
            if (have_state) {
                strcpy(st, state_msg);
                state_dirty = 0;
            }
            uint8_t *fr = NULL;
            size_t fl = 0;
            if (pending_frame && out_off >= out_len) {   /* nur wenn alles Bisherige raus ist */
                fr = pending_frame;
                fl = pending_len;
                pending_frame = NULL;
            }
            pthread_mutex_unlock(&mtx);
            if (have_err)
                queue_text(T_ERROR, msg);
            if (have_state)
                queue_text(T_STATE, st);
            if (fr) {
                if (out_off >= out_len)
                    out_off = out_len = 0;
                uint8_t *nb = realloc(outbuf, out_len + fl);
                if (nb) {
                    outbuf = nb;
                    memcpy(outbuf + out_len, fr, fl);
                    out_len += fl;
                }
                free(fr);
            }
        }
        if (client >= 0 && out_off < out_len) {
            ssize_t n = send(client, outbuf + out_off, out_len - out_off, MSG_NOSIGNAL);
            if (n > 0) {
                out_off += n;
                if (out_off >= out_len)
                    out_off = out_len = 0;
            } else if (n < 0 && errno != EAGAIN && errno != EINTR) {
                drop_client();
            }
        }
    }

    LOG("beende");
    drop_client();
    pthread_mutex_lock(&mtx);
    quitting = 1;
    running = 0;
    pthread_cond_broadcast(&cond);
    pthread_mutex_unlock(&mtx);
    pthread_join(th_cap, NULL);
    pthread_join(th_fft, NULL);
    return 0;
}
