// beatlight ALSA plugin: a pass-through playback filter that also sends a copy of
// each chunk to the beatlight server's unix datagram socket the moment the player writes
// it (so the copy arrives ahead of the DAC). Sends never block and errors are ignored,
// so playback never waits on, or fails because of, the server.
//
// Datagram: 1 byte source id, uint64 little-endian index (frames written since the PCM
// was opened) of the first frame, then interleaved S32_LE stereo frames.
//
//   pcm_type.beatlight { lib "/opt/beatlight/libasound_module_pcm_beatlight.so" }
//   pcm.x { type beatlight; slave.pcm "hw:0"; source "s" }
//
// Build on the same distro as the player (its libc and alsa-lib), e.g. on Alpine:
//   gcc -O2 -Wall -shared -fPIC -DPIC -o libasound_module_pcm_beatlight.so pcm_beatlight.c -lasound

#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define SOCK_PATH "/run/beatlight/tap.sock"
#define CHANNELS 2
#define CHUNK_FRAMES 512   // ~11.6 ms per datagram
#define HDR 9

struct beatlight {
    snd_pcm_extplug_t ext;
    int fd;
    struct sockaddr_un addr;
    uint64_t frame;
    unsigned char buf[HDR + CHUNK_FRAMES * CHANNELS * 4];
};

static void send_chunk(struct beatlight *s, size_t frames) {
    for (int i = 0; i < 8; i++) s->buf[1 + i] = (unsigned char)(s->frame >> (8 * i));
    // No listener (ENOENT/ECONNREFUSED) or a full queue (EAGAIN): drop the chunk.
    if (s->fd >= 0)
        sendto(s->fd, s->buf, HDR + frames * CHANNELS * 4, MSG_DONTWAIT | MSG_NOSIGNAL,
               (struct sockaddr *)&s->addr, sizeof(s->addr));
    s->frame += frames;
}

static snd_pcm_sframes_t beatlight_transfer(snd_pcm_extplug_t *ext,
                                         const snd_pcm_channel_area_t *dst,
                                         snd_pcm_uframes_t dst_offset,
                                         const snd_pcm_channel_area_t *src,
                                         snd_pcm_uframes_t src_offset,
                                         snd_pcm_uframes_t size) {
    struct beatlight *s = ext->private_data;
    snd_pcm_areas_copy(dst, dst_offset, src, src_offset, CHANNELS, size,
                       SND_PCM_FORMAT_S32_LE);

    int32_t *out = (int32_t *)(s->buf + HDR);
    size_t n = 0;
    for (snd_pcm_uframes_t f = 0; f < size; f++) {
        for (int c = 0; c < CHANNELS; c++) {
            const snd_pcm_channel_area_t *a = &src[c];
            const char *p = (const char *)a->addr +
                            (a->first + a->step * (src_offset + f)) / 8;
            memcpy(&out[n * CHANNELS + c], p, 4);
        }
        if (++n == CHUNK_FRAMES) {
            send_chunk(s, n);
            n = 0;
        }
    }
    if (n) send_chunk(s, n);
    return size;
}

static int beatlight_close(snd_pcm_extplug_t *ext) {
    struct beatlight *s = ext->private_data;
    if (s->fd >= 0) close(s->fd);
    free(s);
    return 0;
}

static const snd_pcm_extplug_callback_t beatlight_callback = {
    .transfer = beatlight_transfer,
    .close = beatlight_close,
};

SND_PCM_PLUGIN_DEFINE_FUNC(beatlight) {
    snd_config_iterator_t i, next;
    snd_config_t *slave = NULL;
    const char *source = "?";

    snd_config_for_each(i, next, conf) {
        snd_config_t *n = snd_config_iterator_entry(i);
        const char *id;
        if (snd_config_get_id(n, &id) < 0) continue;
        if (!strcmp(id, "comment") || !strcmp(id, "type") || !strcmp(id, "hint"))
            continue;
        if (!strcmp(id, "slave")) {
            slave = n;
            continue;
        }
        if (!strcmp(id, "source")) {
            if (snd_config_get_string(n, &source) < 0) {
                SNDERR("beatlight: source must be a string");
                return -EINVAL;
            }
            continue;
        }
        SNDERR("beatlight: unknown field %s", id);
        return -EINVAL;
    }
    if (!slave) {
        SNDERR("beatlight: no slave defined");
        return -EINVAL;
    }
    if (stream != SND_PCM_STREAM_PLAYBACK) {
        SNDERR("beatlight: playback only");
        return -EINVAL;
    }

    struct beatlight *s = calloc(1, sizeof(*s));
    if (!s) return -ENOMEM;
    s->buf[0] = (unsigned char)source[0];
    s->addr.sun_family = AF_UNIX;
    strncpy(s->addr.sun_path, SOCK_PATH, sizeof(s->addr.sun_path) - 1);
    s->fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);

    s->ext.version = SND_PCM_EXTPLUG_VERSION;
    s->ext.name = "beatlight";
    s->ext.callback = &beatlight_callback;
    s->ext.private_data = s;

    int err = snd_pcm_extplug_create(&s->ext, name, root, slave, stream, mode);
    if (err < 0) {
        if (s->fd >= 0) close(s->fd);
        free(s);
        return err;
    }
    snd_pcm_extplug_set_param(&s->ext, SND_PCM_EXTPLUG_HW_CHANNELS, CHANNELS);
    snd_pcm_extplug_set_slave_param(&s->ext, SND_PCM_EXTPLUG_HW_CHANNELS, CHANNELS);
    snd_pcm_extplug_set_param(&s->ext, SND_PCM_EXTPLUG_HW_FORMAT, SND_PCM_FORMAT_S32_LE);
    snd_pcm_extplug_set_slave_param(&s->ext, SND_PCM_EXTPLUG_HW_FORMAT,
                                    SND_PCM_FORMAT_S32_LE);
    *pcmp = s->ext.pcm;
    return 0;
}

SND_PCM_PLUGIN_SYMBOL(beatlight);
