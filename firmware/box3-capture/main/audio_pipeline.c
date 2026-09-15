#include "audio_pipeline.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"

#include "esp_afe_sr_models.h"
#include "esp_afe_config.h"
#include "model_path.h"

static const char *TAG = "audio";

#define SAMPLE_RATE      CONFIG_CAPTURE_SAMPLE_RATE
#define MIC_CHANNELS     CONFIG_CAPTURE_MIC_CHANNELS
#define MAX_UTTERANCE_S  CONFIG_CAPTURE_MAX_SECONDS
#define MIN_SPEECH_MS    CONFIG_CAPTURE_MIN_SPEECH_MS
#define TRAIL_SILENCE_MS CONFIG_CAPTURE_TRAILING_SILENCE_MS

typedef enum {
    IDLE,        /* feeding the AFE, listening for the wake word */
    CAPTURING,   /* accumulating an utterance */
    HANDING_OFF, /* the worker owns the buffer; ignore triggers */
} state_t;

static esp_codec_dev_handle_t s_mic;
static utterance_cb_t         s_callback;

static esp_afe_sr_iface_t *s_afe;
static esp_afe_sr_data_t  *s_afe_data;

static int16_t *s_utterance;        /* mono, MAX_UTTERANCE_S seconds */
static size_t   s_utterance_cap;
static size_t   s_utterance_len;

static volatile state_t s_state = IDLE;
static const char *s_trigger = "button";
static QueueHandle_t s_ready;       /* carries the utterance length */

/* ------------------------------------------------------------------ Feed */

/*
 * Reads the microphone and hands frames to the AFE, forever.
 *
 * This is the only place the codec is read. Both mic channels go in: the AFE
 * beamforms across them, which is the fix for speech sounding distant and
 * boxy at table distance. The old path used channel 0 and threw the other
 * one away, so the array gained nothing.
 */
static void feed_task(void *arg)
{
    const int chunk_samples = s_afe->get_feed_chunksize(s_afe_data);
    const size_t chunk_bytes = (size_t)chunk_samples * MIC_CHANNELS * sizeof(int16_t);

    int16_t *chunk = heap_caps_malloc(chunk_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!chunk) {
        ESP_LOGE(TAG, "no memory for feed buffer (%u bytes)", (unsigned)chunk_bytes);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "feeding %d samples x %d ch per chunk", chunk_samples, MIC_CHANNELS);

    while (true) {
        esp_err_t err = esp_codec_dev_read(s_mic, chunk, chunk_bytes);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "mic read failed: %s", esp_err_to_name(err));
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        s_afe->feed(s_afe_data, chunk);
    }
}

/* ----------------------------------------------------------------- Fetch */

static void begin_capture(const char *trigger)
{
    s_trigger = trigger;
    s_utterance_len = 0;
    s_state = CAPTURING;
    ESP_LOGI(TAG, "capturing (%s)", trigger);
}

/*
 * Pulls processed audio out of the AFE and decides when an utterance is over.
 *
 * Endpointing rule: stop after TRAIL_SILENCE_MS of continuous silence, but
 * only once at least MIN_SPEECH_MS of speech has been seen. Without that
 * floor a cough, or the click of the button itself, ends the utterance
 * immediately and Whisper gets nothing.
 */
static void fetch_task(void *arg)
{
    const int fetch_samples = s_afe->get_fetch_chunksize(s_afe_data);
    const int chunk_ms = (fetch_samples * 1000) / SAMPLE_RATE;

    int speech_ms = 0;
    int silence_ms = 0;
    int64_t started = 0;

    ESP_LOGI(TAG, "listening: wake word or button (%d ms per chunk)", chunk_ms);

    while (true) {
        afe_fetch_result_t *result = s_afe->fetch(s_afe_data);
        if (!result || result->ret_value == ESP_FAIL) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (s_state == IDLE) {
            if (result->wakeup_state == WAKENET_DETECTED) {
                speech_ms = 0;
                silence_ms = 0;
                started = esp_timer_get_time();
                begin_capture("wake");
            }
            continue;
        }

        if (s_state != CAPTURING) continue;

        if (started == 0) {                  /* button path starts here */
            speech_ms = 0;
            silence_ms = 0;
            started = esp_timer_get_time();
        }

        /* Accumulate. result->data is mono at SAMPLE_RATE. */
        size_t room = s_utterance_cap - s_utterance_len;
        size_t take = (size_t)fetch_samples < room ? (size_t)fetch_samples : room;
        if (take > 0) {
            memcpy(s_utterance + s_utterance_len, result->data, take * sizeof(int16_t));
            s_utterance_len += take;
        }

        if (result->vad_state == VAD_SPEECH) {
            speech_ms += chunk_ms;
            silence_ms = 0;
        } else {
            silence_ms += chunk_ms;
        }

        const bool endpointed = (speech_ms >= MIN_SPEECH_MS && silence_ms >= TRAIL_SILENCE_MS);
        const bool full = (s_utterance_len >= s_utterance_cap);

        if (!endpointed && !full) continue;

        const int elapsed_ms = (int)((esp_timer_get_time() - started) / 1000);
        started = 0;

        if (speech_ms < MIN_SPEECH_MS) {
            ESP_LOGW(TAG, "only %d ms of speech in %d ms — discarded",
                     speech_ms, elapsed_ms);
            s_state = IDLE;
            continue;
        }

        ESP_LOGI(TAG, "utterance: %d ms, %d ms speech%s",
                 elapsed_ms, speech_ms, full ? " (hit the cap)" : "");

        s_state = HANDING_OFF;
        size_t length = s_utterance_len;
        if (xQueueSend(s_ready, &length, 0) != pdTRUE) {
            ESP_LOGE(TAG, "handoff queue full");
            s_state = IDLE;
        }
    }
}

/* ---------------------------------------------------------------- Worker */

/*
 * Runs the callback off the fetch task.
 *
 * The callback POSTs to the Pi and plays several seconds of audio back. Doing
 * that inline would stop the feed and fetch loops, and the AFE would overrun.
 */
static void worker_task(void *arg)
{
    size_t length;
    while (true) {
        if (xQueueReceive(s_ready, &length, portMAX_DELAY) != pdTRUE) continue;
        if (s_callback) s_callback(s_utterance, length, s_trigger);
        s_state = IDLE;
    }
}

/* ------------------------------------------------------------------ Init */

void audio_pipeline_trigger(const char *trigger)
{
    if (s_state != IDLE) {
        ESP_LOGW(TAG, "busy, trigger ignored");
        return;
    }
    begin_capture(trigger ? trigger : "button");
}

esp_err_t audio_pipeline_start(esp_codec_dev_handle_t mic, utterance_cb_t on_utterance)
{
    s_mic = mic;
    s_callback = on_utterance;

    srmodel_list_t *models = esp_srmodel_init("model");
    if (!models || models->num == 0) {
        ESP_LOGE(TAG, "no models in the 'model' partition — is srmodels.bin flashed?");
        return ESP_ERR_NOT_FOUND;
    }
    for (int i = 0; i < models->num; i++) {
        ESP_LOGI(TAG, "model: %s", models->model_name[i]);
    }

    /* "MM" is the two microphones and no echo reference: there is no AEC here
     * because the board does not play and listen at the same time yet. That
     * changes the day barge-in goes in, and this string is where it changes. */
    afe_config_t *cfg = afe_config_init("MM", models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    if (!cfg) {
        ESP_LOGE(TAG, "afe_config_init failed");
        return ESP_FAIL;
    }

    s_afe = esp_afe_handle_from_config(cfg);
    s_afe_data = s_afe->create_from_config(cfg);
    if (!s_afe_data) {
        ESP_LOGE(TAG, "AFE create failed");
        return ESP_FAIL;
    }

    s_utterance_cap = (size_t)SAMPLE_RATE * MAX_UTTERANCE_S;
    s_utterance = heap_caps_malloc(s_utterance_cap * sizeof(int16_t),
                                   MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_utterance) {
        ESP_LOGE(TAG, "no PSRAM for a %d second utterance", MAX_UTTERANCE_S);
        return ESP_ERR_NO_MEM;
    }

    s_ready = xQueueCreate(1, sizeof(size_t));
    if (!s_ready) return ESP_ERR_NO_MEM;

    /*
     * Feed on core 0, fetch on core 1 — one busy task per core, which is how
     * Espressif's own AFE examples arrange it.
     *
     * Pinning both to core 1 starved that core's idle task and tripped the
     * task watchdog on IDLE1 during playback, with the backtrace landing in
     * WakeNet inference. WakeNet runs on every 32ms frame and is not cheap;
     * sharing a core with the feed loop and the codec writes left no slack.
     *
     * Splitting them is most of the fix, but not all of it: continuous
     * inference means core 1 is legitimately near-saturated, so
     * CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1 is also disabled in
     * sdkconfig.defaults. That is expected for always-on audio, not a
     * workaround for a stall.
     */
    xTaskCreatePinnedToCore(feed_task,   "afe_feed",  4096, NULL, 6, NULL, 0);
    xTaskCreatePinnedToCore(fetch_task,  "afe_fetch", 8192, NULL, 5, NULL, 1);
    xTaskCreate(worker_task, "utterance", 6144, NULL, 4, NULL);

    ESP_LOGI(TAG, "pipeline up: max %ds, endpoint after %dms silence (min %dms speech)",
             MAX_UTTERANCE_S, TRAIL_SILENCE_MS, MIN_SPEECH_MS);
    return ESP_OK;
}
