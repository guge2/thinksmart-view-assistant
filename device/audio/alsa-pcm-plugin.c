/* PipeWire 1.0.5 ALSA PCM nodes with alsa-pcm-watchdog.patch.
 * SPDX-License-Identifier: MIT
 * Based on spa/plugins/alsa/alsa.c, Copyright 2018 Wim Taymans.
 */
#include <errno.h>
#include <spa/support/plugin.h>
#include <spa/support/log.h>

extern const struct spa_handle_factory spa_alsa_source_factory;
extern const struct spa_handle_factory spa_alsa_sink_factory;

struct spa_log_topic alsa_log_topic = SPA_LOG_TOPIC(0, "spa.alsa");

SPA_EXPORT
int spa_handle_factory_enum(const struct spa_handle_factory **factory, uint32_t *index)
{
    static struct spa_handle_factory factories[2];
    spa_return_val_if_fail(factory != NULL, -EINVAL);
    spa_return_val_if_fail(index != NULL, -EINVAL);
    switch (*index) {
    case 0:
        factories[0] = spa_alsa_source_factory;
        factories[0].name = "thinksmart.alsa.pcm.source";
        break;
    case 1:
        factories[1] = spa_alsa_sink_factory;
        factories[1].name = "thinksmart.alsa.pcm.sink";
        break;
    default:
        return 0;
    }
    *factory = &factories[*index];
    (*index)++;
    return 1;
}
