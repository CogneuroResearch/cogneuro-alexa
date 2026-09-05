#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

/*
 * Wake-word trigger. Off by default (CONFIG_CAPTURE_TRIGGER_WAKE_WORD).
 *
 * Give it the same semaphore the button path uses, and a pointer to the trigger
 * label so captures report how they were started. Enabling it before the button
 * path has proven the audio is a way to make three problems look like one.
 */
void wake_word_start(SemaphoreHandle_t capture_request, const char **trigger_label);
