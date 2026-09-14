/*
 * ESP32-S3-BOX-3 capture firmware.
 *
 * Press a button, record a fixed window from the mic array, POST it to the
 * review page. Nothing else. This exists so the raw input can be judged before
 * any of it is transcribed or acted on.
 *
 * The audio path goes through Espressif's BSP for the board: the BOX-3's
 * microphones hang off an ES7210 ADC that has to be configured over I2C before
 * it emits anything on I2S, and bsp_audio_codec_microphone_init() is what does
 * that correctly.
 */

#include <string.h>
#include <strings.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "mdns.h"

#include "bsp/esp-box-3.h"
#include "esp_codec_dev.h"
#include "iot_button.h"

#include "wake_word.h"

static const char *TAG = "capture";

#define SAMPLE_RATE     CONFIG_CAPTURE_SAMPLE_RATE
#define CAPTURE_SECONDS CONFIG_CAPTURE_SECONDS
#define MIC_CHANNELS    CONFIG_CAPTURE_MIC_CHANNELS
#define FIRMWARE_VERSION "0.1.0"

#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_wifi_events;
static SemaphoreHandle_t s_capture_request;
static esp_codec_dev_handle_t s_mic;
#if CONFIG_CAPTURE_ASSISTANT_MODE
static esp_codec_dev_handle_t s_speaker;
#endif
static const char *s_trigger = "button";

/* ------------------------------------------------------------------ WiFi */

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "WiFi dropped, reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "WiFi up, ip " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_start(void)
{
    s_wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_cfg = { 0 };
    strncpy((char *)wifi_cfg.sta.ssid, CONFIG_CAPTURE_WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, CONFIG_CAPTURE_WIFI_PASSWORD, sizeof(wifi_cfg.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
}

static int current_rssi(void)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) return ap.rssi;
    return 0;
}

/* --------------------------------------------------------------- Capture */

/*
 * The microphone is opened once and left open for the life of the firmware.
 *
 * It used to be opened and closed around every capture, which produced
 *
 *     E i2s_common: i2s_channel_disable(1378): the channel has not been
 *                   enabled yet
 *
 * on the second and every subsequent capture: the close disables an I2S
 * channel that the previous cycle had already left disabled, so open/close
 * were not symmetric. Captures still worked, which is why it sat as a
 * cosmetic annoyance for a week.
 *
 * It stops being cosmetic the moment anything else wants the codec. Playback
 * needs it, and the esp-sr AFE behind the wake word holds it open
 * continuously — two owners of one handle with an unbalanced close between
 * them is a real bug waiting for a bad day.
 *
 * Holding it open costs a little idle power and nothing else, and it is what
 * the wake word will need anyway.
 */
static esp_err_t mic_start(void)
{
    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = MIC_CHANNELS,
        .sample_rate = SAMPLE_RATE,
    };

    esp_err_t err = esp_codec_dev_open(s_mic, &fs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "codec open failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_codec_dev_set_in_gain(s_mic, (float)CONFIG_CAPTURE_MIC_GAIN_DB);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "could not set gain: %s", esp_err_to_name(err));
    }

    ESP_LOGI(TAG, "mic open and held: %d ch, %d Hz, gain %d dB",
             MIC_CHANNELS, SAMPLE_RATE, CONFIG_CAPTURE_MIC_GAIN_DB);
    return ESP_OK;
}

static esp_err_t record(int16_t *dst, size_t frames)
{
    /* esp_codec_dev_read blocks until the buffer is full, so one call is the
     * whole window. Reading in chunks would only add places to go wrong. */
    int bytes = (int)(frames * MIC_CHANNELS * sizeof(int16_t));
    esp_err_t err = esp_codec_dev_read(s_mic, dst, bytes);
    if (err != ESP_OK) ESP_LOGE(TAG, "codec read failed: %s", esp_err_to_name(err));
    return err;
}

/* Take channel 0 out of the interleaved stream, in place. */
static void extract_channel0(int16_t *buf, size_t frames)
{
    for (size_t i = 0; i < frames; i++) {
        buf[i] = buf[i * MIC_CHANNELS];
    }
}

#if !CONFIG_CAPTURE_ASSISTANT_MODE
static void post_capture(const int16_t *pcm, size_t bytes, int channels)
{
    char url[320];
    snprintf(url, sizeof(url),
             "%s?device=box3&trigger=%s&sr=%d&ch=%d&bits=16&rssi=%d&fw=%s",
             CONFIG_CAPTURE_ENDPOINT, s_trigger, SAMPLE_RATE, channels,
             current_rssi(), FIRMWARE_VERSION);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 30000,
        .buffer_size_tx = 2048,
    };

    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        ESP_LOGE(TAG, "http client init failed");
        return;
    }

    /* audio/l16 is raw signed 16-bit little-endian PCM. The server wraps it in
     * a WAV header using the sr/ch/bits query parameters, so the firmware never
     * builds one. */
    esp_http_client_set_header(client, "Content-Type", "audio/l16");
    esp_http_client_set_header(client, "X-Device-Key", CONFIG_CAPTURE_DEVICE_KEY);

    esp_err_t err = esp_http_client_open(client, (int)bytes);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "connect failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return;
    }

    const char *cursor = (const char *)pcm;
    size_t remaining = bytes;
    while (remaining > 0) {
        int chunk = (int)(remaining > 4096 ? 4096 : remaining);
        int written = esp_http_client_write(client, cursor, chunk);
        if (written < 0) {
            ESP_LOGE(TAG, "write failed with %d bytes left", (int)remaining);
            esp_http_client_cleanup(client);
            return;
        }
        cursor += written;
        remaining -= written;
    }

    int content_length = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);

    char body[256] = { 0 };
    if (content_length != 0) {
        int read = esp_http_client_read_response(client, body, sizeof(body) - 1);
        if (read > 0) body[read] = '\0';
    }

    if (status == 200) {
        ESP_LOGI(TAG, "posted %u bytes, server said: %s", (unsigned)bytes, body);
    } else {
        ESP_LOGE(TAG, "server returned %d: %s", status, body);
    }

    esp_http_client_cleanup(client);
}
#endif /* !CONFIG_CAPTURE_ASSISTANT_MODE */

#if CONFIG_CAPTURE_ASSISTANT_MODE

/* ------------------------------------------------------------- Assistant */

#define PLAY_CHUNK_BYTES 4096

/*
 * The speaker, like the microphone, is opened once and held.
 *
 * The BSP puts both on the same I2S peripheral and switches the rx channel
 * to slave for full-duplex, so the two can be open together. Cycling either
 * one is what caused the unbalanced-teardown bug; don't reintroduce it here.
 */
static esp_err_t speaker_start(void)
{
    s_speaker = bsp_audio_codec_speaker_init();
    if (!s_speaker) {
        ESP_LOGE(TAG, "speaker init failed");
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = 1,
        .sample_rate = SAMPLE_RATE,
    };
    esp_err_t err = esp_codec_dev_open(s_speaker, &fs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "speaker open failed: %s", esp_err_to_name(err));
        return err;
    }

    esp_codec_dev_set_out_vol(s_speaker, CONFIG_CAPTURE_SPEAKER_VOLUME);
    ESP_LOGI(TAG, "speaker open and held: 1 ch, %d Hz, volume %d%%",
             SAMPLE_RATE, CONFIG_CAPTURE_SPEAKER_VOLUME);
    return ESP_OK;
}

/* Filled in by the header callback so the board can log what it heard and
 * said, without anyone having to read the Pi's log. */
static char s_heard[192];
static char s_reply[256];
static bool s_reply_is_audio;

static esp_err_t assistant_http_event(esp_http_client_event_t *evt)
{
    if (evt->event_id != HTTP_EVENT_ON_HEADER) return ESP_OK;

    if (strcasecmp(evt->header_key, "X-Heard") == 0) {
        snprintf(s_heard, sizeof(s_heard), "%s", evt->header_value);
    } else if (strcasecmp(evt->header_key, "X-Reply") == 0) {
        snprintf(s_reply, sizeof(s_reply), "%s", evt->header_value);
    } else if (strcasecmp(evt->header_key, "Content-Type") == 0) {
        s_reply_is_audio = (strncasecmp(evt->header_value, "audio/", 6) == 0);
    }
    return ESP_OK;
}

/*
 * Play the response body as it arrives.
 *
 * The Pi streams raw 16-bit mono PCM chunk by chunk: a short spoken
 * acknowledgement first, then the answer in clause-sized pieces as the model
 * produces them. Buffering the whole body before playing would throw away
 * the entire point of that — first sound would go from under two seconds to
 * over five.
 *
 * esp_codec_dev_write blocks until the samples are consumed, which paces the
 * loop for free: reads only happen as fast as the speaker drains.
 */
static esp_err_t play_response(esp_http_client_handle_t client)
{
    uint8_t *chunk = heap_caps_malloc(PLAY_CHUNK_BYTES + 1, MALLOC_CAP_8BIT | MALLOC_CAP_DMA);
    if (!chunk) {
        ESP_LOGE(TAG, "no memory for playback buffer");
        return ESP_ERR_NO_MEM;
    }

    size_t total = 0;
    bool have_carry = false;   /* a read can split a 16-bit sample in half */
    uint8_t carry = 0;
    int64_t first_at = 0;
    int64_t started = esp_timer_get_time();
    esp_err_t result = ESP_OK;

    while (!esp_http_client_is_complete_data_received(client)) {
        int offset = 0;
        if (have_carry) {
            chunk[0] = carry;
            offset = 1;
            have_carry = false;
        }

        int got = esp_http_client_read(client, (char *)chunk + offset,
                                       PLAY_CHUNK_BYTES - offset);
        if (got < 0) {
            ESP_LOGE(TAG, "read failed after %u bytes", (unsigned)total);
            result = ESP_FAIL;
            break;
        }
        if (got == 0 && offset == 0) break;

        int usable = got + offset;
        if (usable & 1) {                 /* odd byte count: hold the tail */
            carry = chunk[usable - 1];
            have_carry = true;
            usable -= 1;
        }
        if (usable <= 0) continue;

        if (first_at == 0) {
            first_at = esp_timer_get_time() - started;
            ESP_LOGI(TAG, "first audio after %d ms", (int)(first_at / 1000));
        }

        esp_err_t err = esp_codec_dev_write(s_speaker, chunk, usable);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "speaker write failed: %s", esp_err_to_name(err));
            result = err;
            break;
        }
        total += usable;
    }

    free(chunk);
    ESP_LOGI(TAG, "played %u bytes (%.1fs)",
             (unsigned)total, (float)total / (SAMPLE_RATE * 2.0f));
    return result;
}

static void exchange_with_assistant(const int16_t *pcm, size_t bytes)
{
    s_heard[0] = '\0';
    s_reply[0] = '\0';
    s_reply_is_audio = false;

    esp_http_client_config_t cfg = {
        .url = CONFIG_CAPTURE_ASSISTANT_ENDPOINT,
        .method = HTTP_METHOD_POST,
        .event_handler = assistant_http_event,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 30000,
        .buffer_size_tx = 2048,
    };

    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        ESP_LOGE(TAG, "http client init failed");
        return;
    }

    esp_http_client_set_header(client, "Content-Type", "audio/l16");
    esp_http_client_set_header(client, "X-Device-Key", CONFIG_CAPTURE_DEVICE_KEY);
    char rate[16];
    snprintf(rate, sizeof(rate), "%d", SAMPLE_RATE);
    esp_http_client_set_header(client, "X-Sample-Rate", rate);

    char rssi[16];
    snprintf(rssi, sizeof(rssi), "%d", current_rssi());
    esp_http_client_set_header(client, "X-RSSI", rssi);

    esp_err_t err = esp_http_client_open(client, (int)bytes);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "connect to assistant failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return;
    }

    const char *cursor = (const char *)pcm;
    size_t remaining = bytes;
    while (remaining > 0) {
        int written = esp_http_client_write(client, cursor,
                                            (int)(remaining > 4096 ? 4096 : remaining));
        if (written < 0) {
            ESP_LOGE(TAG, "upload failed with %u bytes left", (unsigned)remaining);
            esp_http_client_cleanup(client);
            return;
        }
        cursor += written;
        remaining -= written;
    }

    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);

    if (status != 200) {
        char body[192] = { 0 };
        int read = esp_http_client_read_response(client, body, sizeof(body) - 1);
        if (read > 0) body[read] = '\0';
        ESP_LOGE(TAG, "assistant returned %d: %s", status, body);
        esp_http_client_cleanup(client);
        return;
    }

    if (s_heard[0]) ESP_LOGI(TAG, "heard: %s", s_heard);

    if (!s_reply_is_audio) {
        /* The Pi answers with JSON when it heard nothing worth sending on. */
        char body[192] = { 0 };
        int read = esp_http_client_read_response(client, body, sizeof(body) - 1);
        if (read > 0) body[read] = '\0';
        ESP_LOGW(TAG, "no audio in reply: %s", body);
        esp_http_client_cleanup(client);
        return;
    }

    play_response(client);
    if (s_reply[0]) ESP_LOGI(TAG, "said: %s", s_reply);

    esp_http_client_cleanup(client);
}

#endif /* CONFIG_CAPTURE_ASSISTANT_MODE */

static void capture_task(void *arg)
{
    const size_t frames = (size_t)SAMPLE_RATE * CAPTURE_SECONDS;
    const size_t raw_bytes = frames * MIC_CHANNELS * sizeof(int16_t);

    int16_t *buf = heap_caps_malloc(raw_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buf) {
        ESP_LOGE(TAG, "could not allocate %u bytes in PSRAM", (unsigned)raw_bytes);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "ready: %d channels, %d Hz, %ds per capture (%u bytes)",
             MIC_CHANNELS, SAMPLE_RATE, CAPTURE_SECONDS, (unsigned)raw_bytes);

    while (true) {
        xSemaphoreTake(s_capture_request, portMAX_DELAY);

        if (!(xEventGroupGetBits(s_wifi_events) & WIFI_CONNECTED_BIT)) {
            ESP_LOGW(TAG, "no WiFi, capture skipped");
            continue;
        }

        ESP_LOGI(TAG, "recording %d seconds", CAPTURE_SECONDS);
        if (record(buf, frames) != ESP_OK) continue;

#if CONFIG_CAPTURE_SEND_MONO
        extract_channel0(buf, frames);
        const size_t send_bytes = frames * sizeof(int16_t);
        const int send_channels = 1;
#else
        const size_t send_bytes = raw_bytes;
        const int send_channels = MIC_CHANNELS;
#endif

#if CONFIG_CAPTURE_ASSISTANT_MODE
        (void)send_channels;
        exchange_with_assistant(buf, send_bytes);
#else
        post_capture(buf, send_bytes, send_channels);
#endif
    }
}

/* --------------------------------------------------------------- Buttons */

static void on_button(void *button_handle, void *usr_data)
{
    s_trigger = "button";
    xSemaphoreGive(s_capture_request);
}

static void buttons_start(void)
{
    button_handle_t btns[BSP_BUTTON_NUM] = { NULL };
    int count = 0;

    /* BSP_BUTTON_MAIN is the touchscreen and needs the display running, which
     * this firmware deliberately doesn't start. The two physical buttons are
     * enough: either one fires a capture. */
    esp_err_t err = bsp_iot_button_create(btns, &count, BSP_BUTTON_NUM);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "button init failed: %s", esp_err_to_name(err));
        return;
    }

    for (int i = 0; i < count && i < BSP_BUTTON_NUM; i++) {
        if (i == BSP_BUTTON_MAIN || btns[i] == NULL) continue;
        iot_button_register_cb(btns[i], BUTTON_SINGLE_CLICK, on_button, NULL);
    }
    ESP_LOGI(TAG, "press either physical button to capture");
}

/* ------------------------------------------------------------------ Main */

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    if (strlen(CONFIG_CAPTURE_DEVICE_KEY) == 0) {
        ESP_LOGE(TAG, "CAPTURE_DEVICE_KEY is empty; the server will reject every post");
    }

    s_capture_request = xSemaphoreCreateBinary();

    wifi_start();

    s_mic = bsp_audio_codec_microphone_init();
    if (!s_mic) {
        ESP_LOGE(TAG, "microphone init failed");
        return;
    }
    if (mic_start() != ESP_OK) return;

#if CONFIG_CAPTURE_ASSISTANT_MODE
    /* lwIP hands .local lookups to mDNS once this is up, so the endpoint can
     * name the Pi rather than an address that moves with the DHCP lease. */
    esp_err_t mdns_err = mdns_init();
    if (mdns_err != ESP_OK) {
        ESP_LOGW(TAG, "mdns_init failed (%s) — .local names will not resolve",
                 esp_err_to_name(mdns_err));
    }

    if (speaker_start() != ESP_OK) return;
    ESP_LOGI(TAG, "assistant endpoint: %s", CONFIG_CAPTURE_ASSISTANT_ENDPOINT);
#endif

    xTaskCreate(capture_task, "capture", 6144, NULL, 5, NULL);
    buttons_start();

#if CONFIG_CAPTURE_TRIGGER_WAKE_WORD
    wake_word_start(s_capture_request, &s_trigger);
#endif
}
