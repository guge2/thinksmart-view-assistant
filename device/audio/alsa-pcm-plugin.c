/* PipeWire 1.0.5 ALSA PCM nodes with alsa-pcm-watchdog.patch.
 * SPDX-License-Identifier: MIT
 * Based on spa/plugins/alsa/alsa.c, Copyright 2018 Wim Taymans.
 */
#include <errno.h>
#include <pthread.h>
#include <spa/support/plugin.h>
#include <spa/support/log.h>

extern const struct spa_handle_factory spa_alsa_source_factory;
extern const struct spa_handle_factory spa_alsa_sink_factory;

struct spa_log_topic alsa_log_topic = SPA_LOG_TOPIC(0, "spa.alsa");

static struct spa_handle_factory thinksmart_factories[2];
static pthread_once_t thinksmart_factories_once = PTHREAD_ONCE_INIT;

static void init_thinksmart_factories(void)
{
    thinksmart_factories[0] = spa_alsa_source_factory;
    thinksmart_factories[0].name = "thinksmart.alsa.pcm.source";
    thinksmart_factories[1] = spa_alsa_sink_factory;
    thinksmart_factories[1].name = "thinksmart.alsa.pcm.sink";
}

SPA_EXPORT
int spa_handle_factory_enum(const struct spa_handle_factory **factory, uint32_t *index)
{
    spa_return_val_if_fail(factory != NULL, -EINVAL);
    spa_return_val_if_fail(index != NULL, -EINVAL);
    pthread_once(&thinksmart_factories_once, init_thinksmart_factories);
    if (*index >= 2)
        return 0;
    *factory = &thinksmart_factories[*index];
    (*index)++;
    return 1;
}
