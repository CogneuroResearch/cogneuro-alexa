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
