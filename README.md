# NanoBanana Radio — Scene-Driven Real-Time Music Radio

A Raspberry Pi photographs a landscape, Gemini 3.1 Flash Lite reads its mood, and
Lyria RealTime generates / varies instrumental music to match. Controlled by
Circuit Playground Express buttons, with current state shown on an E-Ink Gizmo
**as a BMP rendered by the Pi** (the CP just displays it).

## Hardware

- Raspberry Pi (Zero 2 W recommended) + Raspberry Pi Camera
- ReSpeaker 2-Mics Pi HAT v1.0 (WM8960) → 3.5mm → KORG Volca Mix
- Circuit Playground Express + E-Ink Gizmo (200×200, 3-color black/white/red),
  connected to the Pi over USB

## Controls

- **CP Button A**: Toggle auto-capture ON/OFF. While ON, captures on a random
  20–40 s interval, repeatedly. Each shot updates the music prompt.
- **CP Button B**: Vary the track — randomizes `temperature` only, with **BPM
  fixed at 80** so there's no hard cut (no `reset_context` needed).
- **E-Ink**: Pi renders a status BMP (mood / BPM / temp / prompt) and writes it to
  the CP; refreshed at most every 180 s.

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `lyria_radio.py`    | Raspberry Pi | Main app: audio + camera → Gemini → Lyria + buttons + BMP render |
| `lyria_chiptune.py` | Raspberry Pi | Audio integration test (validates the ReSpeaker audio path alone) |
| `code.py`           | CP Express   | Button events + display the Pi's BMP via `show_bmp()` |
| `boot.py`           | CP Express   | USB CDC data channel + remount so the Pi can write to CIRCUITPY |

---

## Why BMP instead of on-device text

The CP Express (SAMD21, ~32 KB RAM) can't even *import* the E-Ink text stack —
`adafruit_il0373` throws `MemoryError` at load time. So the CP does **not** render
text. Instead the Pi (which has plenty of RAM + Pillow) draws a 200×200 3-color
BMP and writes it to the CP's CIRCUITPY drive. The CP only runs `show_bmp()`.

This is the same pattern proven on the NanoBanana Cam:
- The Pi writing `state.bmp` to CIRCUITPY triggers the CP's **auto-reload**.
- On reload (clean memory) `code.py` runs `show_bmp()` **first**, before anything
  heavy, so the E-Ink refresh succeeds even on the tiny SAMD21.
- No NeoPixel, no unnecessary objects.

---

## 1. Raspberry Pi setup

The `~/lyria_radio` folder already exists. Put `lyria_radio.py` (and the test
`lyria_chiptune.py`) there; `code.py` / `boot.py` go on the CP, not the Pi.

```bash
cd ~/lyria_radio

sudo apt install -y portaudio19-dev python3-pyaudio
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install google-genai python-dotenv pyserial pyaudio Pillow

printf 'GEMINI_API_KEY=YOUR_KEY_HERE\n' > .env
chmod 600 .env
```

Check the camera + ReSpeaker volume:
```bash
rpicam-still -n -o /tmp/test.jpg -t 800
alsamixer            # raise/unmute Headphone & Speaker on seeed2micvoicec
sudo alsactl store
```

Find the CIRCUITPY mount path (so the Pi can write the BMP):
```bash
ls /media/$USER/CIRCUITPY        # adjust CIRCUITPY_CANDIDATES in the script if different
```

Run:
```bash
source venv/bin/activate
python3 lyria_radio.py
```

## 2. CP Express setup

Use the **`.mpy` bundle** (not `.py`) — this is what fixes the MemoryError.
From `adafruit-circuitpython-bundle-9.x-mpy-*`, copy into CIRCUITPY/lib:

- `adafruit_il0373.mpy`   ← **.mpy, not .py**
- `adafruit_gizmo/`       (folder)

Then copy to the CIRCUITPY root:
- `boot.py`
- `code.py`

A **displayio-enabled CPX firmware** is required. After adding `boot.py`,
**hard-reset** (unplug power or press reset) for the USB data channel + remount
to apply.

## 3. Bring-up order

1. Confirm audio alone: `python3 lyria_chiptune.py` → hear music → Ctrl+C.
2. Plug CP into the Pi → `ls /dev/ttyACM*` (script auto-probes ACM0/ACM1).
3. `python3 lyria_radio.py` → at boot it pushes an "On Air" BMP once.
4. Press A → first capture → music tracks the scene, E-Ink updates.

---

## How it works

### Scene → music (Gemini 3.1 Flash Lite)
The Pi captures with `rpicam-still`, sends the JPEG to Gemini, and gets back a
2–3 sentence ambient-synth prompt plus a short `MOOD:` label (used on the E-Ink).
The prompt is applied to Lyria via `set_weighted_prompts`.

### Variation (Button B)
Randomizes `temperature` (0.5–1.4) only. BPM stays at 80, so the music morphs
smoothly with no context reset / hard cut.

### E-Ink BMP (180 s guard)
`push_eink()` renders a 200×200 **3-color** BMP (white/black/red, matching the
Gizmo) with Pillow, writes it atomically to `CIRCUITPY/state.bmp`, and `os.sync()`s.
A 180 s guard skips writes that would refresh the E-Ink too often (music + logs
are unaffected). Writing the file auto-reloads the CP, which redraws from clean memory.

### Logging
Every vision update and B-variation is appended to
`~/lyria_radio/radio_logs/radio_live_log_YYYYMMDD.md` — handy as Reels / portfolio
source material. Raw shots are kept in `~/lyria_radio/shots/`.

## Audio: no-dropout design

Proven PyAudio + dedicated playback thread (validated via `lyria_chiptune.py`):
pre-roll buffering (`BUFFER_PRE_ROLL`, default 50 chunks) and **re-prime on
underrun** (pause, refill, resume). Capture and Gemini calls run via
`asyncio.to_thread`, so they never block the audio receive loop. Device is found
by name ("seeed") with index-0 fallback.

## Troubleshooting

- **`MemoryError` on import (CP)**: you're using `.py` libs. Switch to the
  **`.mpy`** bundle for `adafruit_il0373` and `adafruit_gizmo`.
- **E-Ink doesn't update**: check the Pi found CIRCUITPY (console prints
  `[eink] CIRCUITPY 마운트를 못 찾음` if not) — fix `CIRCUITPY_CANDIDATES`.
  Also remember the 180 s minimum between refreshes.
- **Music stutters**: raise `BUFFER_PRE_ROLL` to 80–100; a Zero 2 W helps most.
- **ReSpeaker open fails**: list devices and adjust index/name:
  `python3 -c "import pyaudio; p=pyaudio.PyAudio(); [print(i,p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"`
- **CP port not found**: hard-reset after adding `boot.py`.
- **Pi can't write state.bmp**: `boot.py`'s `storage.remount("/", readonly=True)`
  makes CIRCUITPY writable by the USB host (the Pi). Confirm it applied (hard reset).
