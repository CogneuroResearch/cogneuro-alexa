# box3-capture

Firmware for the ESP32-S3-BOX-3 that records a fixed window on a button press
and POSTs it to the capture review page. It does one thing so that when audio
looks wrong, there is only one place to look.

Deliberately absent: wake word (scaffolded, off), the display, Wyoming, Home
Assistant, any transcription. This is a wire from the microphone to your browser.

## What it does

1. Connects to WiFi.
2. Initialises the mic array through Espressif's BOX-3 BSP. The board's
   microphones run through an ES7210 ADC that must be configured over I2C before
   it puts anything on the I2S bus — the BSP handles that.
3. On a press of either physical button, records `CAPTURE_SECONDS` (default 5)
   at 16kHz, 16-bit.
4. Extracts channel 0 to mono and POSTs it as raw PCM (`audio/l16`) with the
   device key. The server wraps it in a WAV header, so no header code here.

## Build

Requires ESP-IDF v5.1 or newer.

```
cd firmware/box3-capture
idf.py set-target esp32s3
idf.py menuconfig
```

Under **Capture firmware configuration**, set:

- WiFi SSID and password
- Capture endpoint URL (defaults to the production deployment)
- Device key — must match `DEVICE_KEY` on the server

Then:

```
idf.py build
idf.py -p /dev/cu.usbmodem101 flash monitor
```

The port name varies; `ls /dev/cu.*` with the board plugged in will show it.
`Ctrl-]` exits the monitor.

## Reading the log

A healthy capture looks like:

```
I (3120) capture: WiFi up, ip 192.168.1.54
I (3400) capture: ready: 2 channels, 16000 Hz, 5s per capture (320000 bytes)
I (3401) capture: press either physical button to capture
I (9820) capture: recording 5 seconds
I (15140) capture: posted 160000 bytes, server said: {"ok":true,...}
```

Failures are specific on purpose: `server returned 401` means the device key is
wrong, `codec read failed` means the mic path, `no WiFi, capture skipped` means
the network dropped.

## When captures sound wrong

The review page shows peak level per clip, which narrows this quickly:

- **Silence, peak near 0** — try `CAPTURE_MIC_CHANNELS` = 4. The ES7210 carries
  two microphones plus two echo-reference channels, and which lands where is the
  first thing to check.
- **Clipping, peak pinned at 100%** — drop `CAPTURE_MIC_GAIN_DB` from 30.
- **Too quiet but not silent** — raise it. 30dB is a starting guess, not a
  measured value.
- **Sounds like noise or is pitched wrong** — a channel-count mismatch, which
  shifts the interleaving. Turn off `CAPTURE_SEND_MONO` to hear each channel
  separately.

## Wake word

`wake_word.c` holds the integration points and is off. Turning it on before the
button path is trusted means a bad capture could be the mic, the gain, the
endpointing, or the detector, all at once. The button path answers the mic
question first.

---

## Field notes (14 Sept 2026)

Things that cost real time and are not obvious from the code.

### Which button works is not stable

BSP enum: `BSP_BUTTON_CONFIG = 0` (BOOT, GPIO0), `BSP_BUTTON_MUTE = 1`,
`BSP_BUTTON_MAIN = 2` (touchscreen — skipped, the display is never started,
so a **black screen is expected, not a fault**).

On 8 Sept the **mute** button triggered captures and BOOT appeared dead.
After a reflash on 14 Sept this **reversed**: BOOT worked, mute did nothing.
No code changed in between. This has now cost an hour twice over.

Diagnose it rather than hunting for the live button each session: log the
`count` returned by `bsp_iot_button_create()` and the return value of each
`iot_button_register_cb()`, and see what the BSP actually finds.

Physical layout: the top edge is indicator LED (left), mute (middle), power
(right). BOOT and RST are on the **bottom** edge either side of the USB-C.

### The serial port name changes with the USB port

`/dev/cu.usbmodem2101` on one port, `/dev/cu.usbmodem101` on another. A wrong
name fails at the flash step with a generic
`CMake Error at run_serial_tool.cmake:67 ... failed`, with the real error
buried in `build/log/idf_py_stderr_output_*`. Always check first:

```bash
ls /dev/cu.*
```

Flash and power through the **main box's own USB-C** on the bottom edge.
BOX-3-DOCK's Type-C is 5V input only and will not enumerate.

### The SSID is compiled in

WiFi credentials live in `sdkconfig` (gitignored) and are baked in at flash
time, so **moving the board to a different network means `idf.py menuconfig`
and a reflash** — unlike the Pi, it cannot be changed over the air. Worth
remembering before assuming the board is broken in a new location.

### The monitor holds the port

An open `idf.py monitor` in another tab makes the next `flash` fail with
"port is busy". Quit it with `Ctrl-]` first. A successful reflash is
confirmed by log timestamps restarting from zero. The monitor window is a
serial console, not a shell — typing into it does nothing, because this
firmware never reads UART input.

### The capture semaphore queues one extra press

It is binary: a second press *during* a recording fires another 5-second
capture the moment the first posts. In the log that is two captures
milliseconds apart; on the review page it is a mysteriously silent clip.
Not a fault.

### Mic gain: 28 dB, box away from the wall

| Gain | Distance | Result |
|---|---|---|
| 30 dB | handheld ~10cm | clipping |
| 15 dB | handheld ~10cm | clean, peak 14% |
| 22 dB | table 50–80cm | intelligible but "a bit distant" |
| **28 dB** | table, away from the wall | **good — settled here** |

The placement change probably mattered more than the gain. Gain multiplies
voice and room reflections equally, so it cannot fix "distant and boxy" —
that is a direct-to-reverberant problem, and the box had been sitting on a
hard table with a wall close behind.

Calibration had to be done **by ear**: the review page's `peak` metric
measures the codec-open transient rather than the speech, so it reads 100%
regardless of gain. Don't trust it until that is fixed.

The remaining lever for presence is not gain — `extract_channel0()` discards
the second mic, so the array currently gains nothing. Feeding both channels
through the esp-sr AFE is the real fix, and lands with the wake word.

## Known bugs, in the order they matter

1. **Codec ownership / asymmetric teardown.** `i2s_channel_disable(1378):
   the channel has not been enabled yet` fires at ERROR level on the second
   and later captures. `record()` opens and closes the codec per capture and
   the teardown is not symmetric. Harmless today — captures either side of
   it post normally — but **fix this before adding playback or the wake
   word**, both of which introduce a second owner of the codec handle. This
   is exactly what `wake_word.c` warns about.

2. **Codec-open transient.** Every capture begins with a full-scale spike
   from `esp_codec_dev_open`. Discard the first ~50 ms. (`brain/speech.py`
   already trims it server-side.)

3. **Empty `server said:`.** The response body is never read — likely a
   chunked response with `content_length == -1` — so the line prints
   regardless of status code. A rejected POST looks identical to an accepted
   one, which makes auth failures invisible. Worth fixing.

4. **Only channel 0 is used.** See the gain note above.

## menuconfig settings that sdkconfig.defaults will NOT give you

`sdkconfig.defaults` is only read when there is no `sdkconfig` yet. An
existing checkout keeps its old values silently, and the failure always looks
like something else. Each of these cost a build-and-flash cycle to find:

| Setting | Where | Symptom if missed |
|---|---|---|
| Custom partition table CSV | Partition Table | `app partition is too small ... size 0x177000` |
| `LWIP_DNS_SUPPORT_MDNS_QUERIES` | Component config → LWIP → DNS | cannot resolve `cogneuro-pi.local` |
| A WakeNet model | ESP Speech Recognition → Load Multiple Wake Words | `no models in the 'model' partition` |
| `ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1` **off** | ESP System Settings → Watchdogs | watchdog backtrace in WakeNet during playback |

If in doubt, delete `sdkconfig` and let the defaults regenerate — but note
that also wipes your WiFi credentials and device key, which live there.

## Wake word

`Hey Nova` (`wn10_heynova`), selected under ESP Speech Recognition → Load
Multiple Wake Words (WakeNet9 or WakeNet10).

The list has three tiers worth knowing: `wn10_` models are the newest
generation (only two English ones — `Hey,Hermes` and `Hey,Nova`); plain
`wn9_` models are properly trained (`Alexa`, `Hi,ESP`); and `wn9_..._tts`
models are trained on synthesised speech and are generally weaker in a real
room. `Alexa` works well but sets off any Echo within earshot.

Each enabled model costs flash and CPU on every frame, so enable one.

## Architecture: one owner of the microphone

`audio_pipeline.c` owns the mic. Nothing else may read the codec.

WakeNet has to see every frame, so the AFE reads continuously; a second
reader in `capture_main.c` would be the first bug you hit. Both triggers —
the wake word and the buttons — call `audio_pipeline_trigger()` and do the
same thing. Utterances come back to `capture_main.c` by callback, on a worker
task, so blocking there for the network round trip and playback is fine.

The AFE pipeline is `[input] -> SE(BSS) -> VAD(WebRTC) -> WakeNet -> [output]`:
both microphones in, beamformed, one clean channel out. `extract_channel0()`
is gone.

Endpointing: an utterance ends after `CAPTURE_TRAILING_SILENCE_MS` (700ms) of
silence, once at least `CAPTURE_MIN_SPEECH_MS` (300ms) of speech has been
seen, capped at `CAPTURE_MAX_SECONDS`. The minimum matters — without it a
cough or the button click itself ends the utterance and Whisper gets nothing.

**Task placement is load-bearing.** Feed runs on core 0, fetch on core 1.
Both on core 1 starved that core's idle task and tripped the task watchdog
during playback. Splitting is most of the fix; core 1 is still legitimately
near-saturated by inference on every 32ms frame, which is why the CPU1 idle
watchdog check is disabled.

## Next: talk to the Pi

Done — assistant mode is the default path now. The review page remains under
a menuconfig switch for judging raw input. The wire format:

```
POST http://cogneuro-pi.local:8080/talk?stream=1
Content-Type: audio/wav        (or raw PCM; send X-Sample-Rate)
-> chunked raw 16-bit PCM @ 16kHz mono, to play through the ES8311
```

Response headers carry `X-Sample-Rate`, `X-Channels`, `X-Bits`. Audio starts
arriving roughly 1.8s in (a short spoken acknowledgement while the model
thinks), so play chunks as they arrive rather than buffering the whole
reply — buffering throws away the entire point of the streaming endpoint.

After that: endpointing (VAD) so a turn ends when the speaker stops, instead
of the fixed 5-second window. See `brain/README.md`.
