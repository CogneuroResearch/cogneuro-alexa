#include "wake_word.h"
#include "esp_log.h"
#include "sdkconfig.h"

static const char *TAG = "wakeword";

#if CONFIG_CAPTURE_TRIGGER_WAKE_WORD

/*
 * SCAFFOLDING — not yet built or run.
 *
 * To finish this path:
 *
 * 1. Add the model dependency in main/idf_component.yml:
 *        espressif/esp-sr: "^1.9"
 *    and select a wake word under `idf.py menuconfig` →
 *    ESP Speech Recognition → Wake word model.
 *
 * 2. The shape below is the standard AFE arrangement: one task feeds mic audio
 *    into the front end, another polls it for a detection. Both run continuously,
 *    which means the codec stays open — unlike the button path, which opens it
 *    per capture. Reconcile that with record() in capture_main.c before enabling:
 *    two owners of one codec handle is the first bug you'd hit.
 *
 *      esp_afe_sr_iface_t *afe = &ESP_AFE_SR_HANDLE;
 *      afe_config_t *cfg = afe_config_init("MM", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
 *      esp_afe_sr_data_t *data = afe->create_from_config(cfg);
 *
 *      feed task:   esp_codec_dev_read(mic, chunk, afe->get_feed_chunksize(data) * channels * 2);
 *                   afe->feed(data, chunk);
 *
 *      fetch task:  afe_fetch_result_t *res = afe->fetch(data);
 *                   if (res->wakeup_state == WAKENET_DETECTED) { *trigger = "wake"; xSemaphoreGive(req); }
 *
 * 3. Decide what the 5 seconds means once a wake word starts it — the window
 *    should probably begin at detection, not after it, or you lose the first
 *    syllable of the command.
 */

void wake_word_start(SemaphoreHandle_t capture_request, const char **trigger_label)
{
    (void)capture_request;
    (void)trigger_label;
    ESP_LOGW(TAG, "wake word is enabled in config but not implemented yet; use the buttons");
}

#else

void wake_word_start(SemaphoreHandle_t capture_request, const char **trigger_label)
{
    (void)capture_request;
    (void)trigger_label;
}

#endif
