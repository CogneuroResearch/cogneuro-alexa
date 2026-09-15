#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_codec_dev.h"

/*
 * The single owner of the microphone.
 *
 * Everything that wants mic audio goes through here. The board used to read
 * the codec directly from capture_task, which cannot coexist with a wake word:
 * the AFE has to read continuously, and two readers of one codec handle is the
 * first bug you would hit.
 *
 * So the AFE is always running, and both triggers — the wake word and the
 * button — do the same thing: ask for the next utterance to be captured.
 *
 * Utterances end when the speaker stops, not after a fixed window. The AFE
 * gives us voice activity detection alongside the wake word, and the fixed
 * five-second window was by far the largest remaining source of latency.
 */

/* Called from a worker task once an utterance is complete. The buffer is
 * mono 16-bit at CONFIG_CAPTURE_SAMPLE_RATE and is reused afterwards, so
 * copy anything you need to keep. */
typedef void (*utterance_cb_t)(const int16_t *pcm, size_t samples, const char *trigger);

esp_err_t audio_pipeline_start(esp_codec_dev_handle_t mic, utterance_cb_t on_utterance);

/* Start capturing without a wake word — the button path. Ignored while an
 * utterance is already being captured or handled. */
void audio_pipeline_trigger(const char *trigger);
