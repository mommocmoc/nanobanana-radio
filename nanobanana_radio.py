"""
NanoBanana Radio — 풍경 기반 실시간 음악 라디오 (Pi 쪽)
========================================================
Raspberry Pi + Camera + ReSpeaker 2-Mics(WM8960) + CP Express + E-Ink Gizmo

흐름:
  CP A 버튼 토글 ON → 20~40초 랜덤 간격 자동 촬영 반복
    촬영 → Gemini 3.1 Flash Lite 무드 분석 → Lyria 프롬프트 업데이트
  CP B 버튼          → temperature만 랜덤 조율(BPM 고정 → 끊김 없는 변주)
  상태 변화 시       → Pi가 상태 BMP를 생성해 CIRCUITPY에 기록(CP가 표시)

오디오: Lyria 48kHz/16-bit/스테레오 → PyAudio(독립 스레드) → ReSpeaker → Volca Mix

E-Ink BMP (NanoBanana Cam 방식):
  Pi가 200x200 3색 BMP를 만들어 CIRCUITPY/state.bmp 로 복사.
  CP는 파일 변경에 따른 auto-reload 후 깨끗한 메모리에서 show_bmp()만 수행.
  E-Ink는 180초보다 자주 리프레시 금지 → Pi가 전송 주기를 가드.

사전 준비:
    python3 -m venv ~/lyria_radio/venv
    source ~/lyria_radio/venv/bin/activate
    sudo apt install -y portaudio19-dev python3-pyaudio
    pip install google-genai python-dotenv pyserial pyaudio Pillow
    # ~/lyria_radio/.env 에 GEMINI_API_KEY=... 한 줄

실행:
    source venv/bin/activate
    python3 lyria_radio.py
"""

import asyncio
import os
import queue
import random
import threading
import time

import pyaudio
import serial
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

# ===========================================================================
# 경로 / 설정
# ===========================================================================
PROJECT_DIR = os.path.expanduser("~/lyria_radio")
LOG_DIR = os.path.join(PROJECT_DIR, "radio_logs")
SHOTS_DIR = os.path.join(PROJECT_DIR, "shots")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(SHOTS_DIR, exist_ok=True)
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# --- 오디오 (Lyria 규격) -----------------------------------------------------
FORMAT = pyaudio.paInt16
CHANNELS = 2
RATE = 48000
CHUNK_SIZE = 2048
RESPEAKER_INDEX = 0          # audiolist 기준. 안 맞으면 이름 탐색으로 폴백(아래)
RESPEAKER_NAME_HINT = "seeed"

audio_queue = queue.Queue()
BUFFER_PRE_ROLL = 50
QUEUE_MAX_CHUNKS = 400
is_playing = False

# --- 시리얼 / 모델 -----------------------------------------------------------
# CP에서 usb_cdc.data 채널을 켜면 포트가 2개 생긴다:
#   ACM0 = console(REPL), ACM1 = data(버튼 신호) ← 우리가 봐야 할 건 data!
# 그래서 ACM1을 먼저 시도한다. 환경에 따라 다르면 아래 순서를 조정.
SERIAL_CANDIDATES = ["/dev/ttyACM1", "/dev/ttyACM0"]
BAUD = 115200
GEMINI_MODEL = "gemini-3.1-flash-lite"

# --- E-Ink BMP ---------------------------------------------------------------
# CIRCUITPY 마운트 경로 (Pi에서 CP가 USB로 붙으면 보통 여기 마운트됨)
CIRCUITPY_CANDIDATES = [
    "/media/cowcowwow/CIRCUITPY",
    "/media/pi/CIRCUITPY",
    os.path.expanduser("~/CIRCUITPY"),
]
EINK_W, EINK_H = 200, 200
EINK_MIN_REFRESH = 180.0     # Adafruit 공식: 180초보다 자주 리프레시 금지
last_eink_refresh = -EINK_MIN_REFRESH
eink_lock = threading.Lock()

# --- 라디오 상태 -------------------------------------------------------------
continuous_mode = False
FIXED_BPM = 80               # BPM 고정 → 변주 시 reset_context 불필요(끊김 없음)
current_temp = 1.0
current_density = 0.5        # 음 밀도 (0=성김, 1=빽빽). reset 불필요로 실시간 반영
current_brightness = 0.5     # 음색 밝기 (0=어두움, 1=밝음). reset 불필요
current_prompt = ""          # 현재 메인 프롬프트(weight 변주용 보관)
current_mood = "Booting"     # E-Ink 표시용 짧은 무드 요약

DEFAULT_PROMPT = (
    "Chiptune combined with ethereal Ambient soundscape. "
    "8-bit retro gaming synthesizer melodies floating over spacey synth pads. "
    "Dreamy, nostalgic, relaxing, and bright electronic textures."
)

# --- 변주(B 버튼) 튜닝 -------------------------------------------------------
VARY_STEPS = 6          # 목표값까지 나눠 이동할 스텝 수(많을수록 더 부드러움)
VARY_STEP_SLEEP = 0.8   # 스텝 간 간격(초). steps*sleep ≈ 전체 전환 시간
VARY_MAX_DELTA = 0.35   # density/brightness가 한 변주에서 이동하는 최대 폭


# ===========================================================================
# 오디오 재생 스레드 (검증된 PyAudio + 재충전 방식)
# ===========================================================================
def find_respeaker_index(p):
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except Exception:
            continue
        if info.get("maxOutputChannels", 0) >= 1 and \
           RESPEAKER_NAME_HINT in str(info.get("name", "")).lower():
            return i
    return RESPEAKER_INDEX


def audio_playback_thread():
    global is_playing
    p = pyaudio.PyAudio()
    idx = find_respeaker_index(p)
    try:
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True,
                        frames_per_buffer=CHUNK_SIZE, output_device_index=idx)
    except Exception as e:
        print(f"❌ ReSpeaker 장치 오픈 실패(index {idx}): {e}")
        p.terminate()
        return

    print(f"🎵 오디오 스트림 대기(장치 {idx}). 버퍼 채우는 중...")
    while True:
        if not is_playing:
            if audio_queue.qsize() >= BUFFER_PRE_ROLL:
                print("▶️ 버퍼 확보! 실시간 방송 시작")
                is_playing = True
            else:
                threading.Event().wait(0.1)
                continue
        try:
            data = audio_queue.get(timeout=1)
            stream.write(data)
            audio_queue.task_done()
        except queue.Empty:
            print("⚠️ 네트워크 밀림 → 재충전 위해 일시정지")
            is_playing = False


# ===========================================================================
# E-Ink BMP 생성 / 전송 (NanoBanana Cam 방식)
# ===========================================================================
def find_circuitpy():
    for path in CIRCUITPY_CANDIDATES:
        if os.path.isdir(path):
            return path
    return None


def render_state_bmp(mood, bpm, temp, prompt_short):
    """200x200 3색(흑/백/적) BMP를 만들어 경로 반환. E-Ink Gizmo 사양."""
    img = Image.new("RGB", (EINK_W, EINK_H), "white")
    d = ImageDraw.Draw(img)
    try:
        font_b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_m = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        font_b = font_m = font_s = ImageFont.load_default()

    RED = (255, 0, 0)
    BLACK = (0, 0, 0)

    # 헤더(적색 바)
    d.rectangle([0, 0, EINK_W, 28], fill=RED)
    d.text((8, 5), "NanoBanana Radio", font=font_b, fill="white")

    # 무드
    d.text((8, 38), mood[:22], font=font_m, fill=BLACK)
    # BPM / Temp
    d.text((8, 64), f"BPM {bpm}   Temp {temp}", font=font_m, fill=BLACK)
    # 구분선
    d.line([8, 88, EINK_W - 8, 88], fill=BLACK, width=1)
    # 프롬프트 요약 (여러 줄로 wrap)
    y = 96
    words = prompt_short.split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if d.textlength(test, font=font_s) > EINK_W - 16:
            d.text((8, y), line, font=font_s, fill=BLACK)
            y += 16
            line = w
            if y > EINK_H - 20:
                break
        else:
            line = test
    if line and y <= EINK_H - 20:
        d.text((8, y), line, font=font_s, fill=BLACK)

    out = os.path.join(PROJECT_DIR, "state.bmp")
    # E-Ink Gizmo는 3색(흑/백/적). 3색 팔레트로 양자화하면 파일이 작고
    # CP의 OnDiskBitmap이 안정적으로 읽는다.
    pal_img = Image.new("P", (1, 1))
    # 팔레트: index0=흰, 1=검, 2=적 (나머지는 흰색으로 채움)
    palette = [255, 255, 255,  0, 0, 0,  255, 0, 0] + [255, 255, 255] * 253
    pal_img.putpalette(palette)
    img.convert("RGB").quantize(palette=pal_img, dither=Image.FLOYDSTEINBERG).save(out, "BMP")
    return out


def push_eink(mood, bpm, temp, prompt_short, force=False):
    """상태 BMP를 만들어 CIRCUITPY로 복사. 180초 가드.
    blocking(파일 IO)이라 호출부에서 to_thread로 감쌀 것."""
    global last_eink_refresh
    with eink_lock:
        now = time.monotonic()
        if not force and (now - last_eink_refresh) < EINK_MIN_REFRESH:
            return  # 너무 이름. 건너뜀(음악/로그엔 영향 없음)
        dest_dir = find_circuitpy()
        if not dest_dir:
            print("[eink] CIRCUITPY 마운트를 못 찾음(표시 건너뜀)")
            return
        try:
            bmp = render_state_bmp(mood, bpm, temp, prompt_short)
            dest = os.path.join(dest_dir, "state.bmp")
            # NanoBanana Cam과 동일하게 대상 파일에 직접 쓴다.
            # (rename보다 파일 쓰기가 CP의 auto-reload를 확실히 트리거)
            with open(bmp, "rb") as src:
                data = src.read()
            with open(dest, "wb") as dst:
                dst.write(data)
            os.sync()
            last_eink_refresh = now
            print(f"[eink] 상태 BMP 전송: {mood} / BPM {bpm} / Temp {temp}")
            # CP는 파일 변경(auto-reload)으로 스스로 재시작해 다시 그린다.
            # → SHOW 신호 같은 별도 전송이 필요 없다(NanoBanana Cam과 동일).
        except Exception as e:
            print(f"[eink] 전송 실패: {e}")


# ===========================================================================
# 로그 (MD) — Reels/포트폴리오 기록용
# ===========================================================================
def log_live_event(ts, entry_type, details):
    log_file = os.path.join(LOG_DIR, f"radio_live_log_{time.strftime('%Y%m%d')}.md")
    if entry_type == "VISION":
        entry = (
            f"\n## [VISION UPDATE] {ts}\n"
            f"- model: `{GEMINI_MODEL}`\n"
            f"- image: `{details['img_path']}`\n"
            f"- prompt: \"{details['prompt']}\"\n"
            f"- spec: BPM {FIXED_BPM} / Temp {current_temp}\n---\n"
        )
    else:  # VARIATION
        entry = (
            f"\n## [LIVE REMIX] {ts} (B)\n"
            f"- temp: {details['old_temp']} -> {details['new_temp']} (BPM {FIXED_BPM} 고정)\n"
            f"- effect: {details['effect_desc']}\n---\n"
        )
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)


# ===========================================================================
# 비전 → 음악
# ===========================================================================
async def process_vision_to_music(client, session, trigger_type):
    global current_mood, current_prompt
    ts = time.strftime("%Y%m%d_%H%M%S")
    raw_img = os.path.join(SHOTS_DIR, f"{ts}_{trigger_type}.jpg")

    print(f"📸 [{trigger_type}] 촬영...")
    await asyncio.to_thread(
        os.system,
        f"rpicam-still -o {raw_img} --width 1280 --height 720 -t 1000 -n")
    if not os.path.exists(raw_img):
        print("❌ 캡처 실패")
        return

    print(f"🤖 {GEMINI_MODEL} 무드 분석...")
    vision_prompt = (
        "Analyze this image and describe its mood, atmosphere, and dominant visual "
        "elements in 2-3 English sentences for an ambient synth music prompt. "
        "Do not include intros."
    )
    mood_prompt = (
        "Also, on a separate final line starting with 'MOOD:', give a max-4-word "
        "mood label (e.g. 'MOOD: Misty calm dawn')."
    )
    try:
        with open(raw_img, "rb") as f:
            img_bytes = f.read()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                      vision_prompt + " " + mood_prompt],
        )
        text = response.text.strip()

        # MOOD: 라인 분리(있으면 E-Ink용으로, 본문은 음악 프롬프트로)
        mood_label = current_mood
        music_lines = []
        for ln in text.splitlines():
            if ln.strip().upper().startswith("MOOD:"):
                mood_label = ln.split(":", 1)[1].strip()[:22]
            else:
                music_lines.append(ln)
        generated_prompt = " ".join(music_lines).strip() or text
        current_mood = mood_label

        print(f"✨ 새 프롬프트: {generated_prompt}")
        log_live_event(time.strftime("%Y-%m-%d %H:%M:%S"), "VISION",
                       {"img_path": raw_img, "prompt": generated_prompt})

        # 이전 프롬프트 → 새 프롬프트로 cross-fade(부드러운 전환).
        # 이전 프롬프트 weight를 줄이며 새 것을 키운다. (BPM 고정이라 reset 없음)
        old_prompt = current_prompt
        for step in range(1, 5 + 1):
            wn = round(step / 5, 2)            # 새 프롬프트 0.2→1.0
            wo = round(1.0 - wn, 2)            # 이전 프롬프트 1.0→0.0
            wp = [types.WeightedPrompt(text=generated_prompt, weight=max(wn, 0.1))]
            if old_prompt and wo > 0:
                wp.append(types.WeightedPrompt(text=old_prompt, weight=max(wo, 0.1)))
            await session.set_weighted_prompts(prompts=wp)
            await asyncio.sleep(0.8)
        current_prompt = generated_prompt

        # E-Ink 갱신(blocking → 스레드)
        await asyncio.to_thread(
            push_eink, current_mood, FIXED_BPM, current_temp, generated_prompt[:120])
    except Exception as e:
        print(f"❌ 비전 파이프라인 오류: {e}")


def _clamp01(v):
    return max(0.0, min(1.0, v))


async def apply_random_variation(session):
    """B 버튼: density/brightness/temperature를 목표값까지 여러 스텝으로 '보간'해
    부드럽게 모핑한다. prompt weight도 함께 미세하게 흔들어 질감을 바꾼다.
    BPM은 고정이라 reset_context가 없어 하드컷이 생기지 않는다."""
    global current_temp, current_density, current_brightness
    if session is None:
        return

    # 시작값
    d0, b0, t0 = current_density, current_brightness, current_temp
    # 목표값: 현재값에서 ±VARY_MAX_DELTA 안에서 랜덤 이동(0~1로 클램프)
    d1 = _clamp01(d0 + random.uniform(-VARY_MAX_DELTA, VARY_MAX_DELTA))
    b1 = _clamp01(b0 + random.uniform(-VARY_MAX_DELTA, VARY_MAX_DELTA))
    t1 = round(random.uniform(0.6, 1.4), 2)

    # 효과 설명(로그/모니터용) — 어느 방향으로 움직이는지
    moves = []
    moves.append("밀도↑" if d1 > d0 else "밀도↓")
    moves.append("밝기↑" if b1 > b0 else "밝기↓")
    moves.append("실험적" if t1 >= 1.1 else ("차분" if t1 <= 0.7 else "균형"))
    effect = f"{' / '.join(moves)} (density {d0:.2f}→{d1:.2f}, bright {b0:.2f}→{b1:.2f}, temp {t0}→{t1})"
    print(f"\n🎲 [B 변주] {effect}  [BPM {FIXED_BPM} 고정, 부드럽게 {VARY_STEPS}스텝]\n")

    try:
        # 여러 스텝에 걸쳐 선형 보간하며 config를 갱신 → 점프 없이 모핑
        for step in range(1, VARY_STEPS + 1):
            f = step / VARY_STEPS
            d = round(d0 + (d1 - d0) * f, 3)
            b = round(b0 + (b1 - b0) * f, 3)
            t = round(t0 + (t1 - t0) * f, 3)
            await session.set_music_generation_config(
                config=types.LiveMusicGenerationConfig(
                    bpm=FIXED_BPM,          # 고정 → reset 불필요
                    temperature=t,
                    density=d,
                    brightness=b,
                )
            )
            # prompt weight도 살짝 흔들어 질감 변화(현재 프롬프트가 있을 때만)
            if current_prompt:
                w = round(0.7 + 0.6 * random.random(), 2)  # 0.7~1.3 사이
                await session.set_weighted_prompts(
                    prompts=[types.WeightedPrompt(text=current_prompt, weight=w)])
            await asyncio.sleep(VARY_STEP_SLEEP)

        # 최종값 확정
        current_density, current_brightness, current_temp = d1, b1, t1

        log_live_event(time.strftime("%Y-%m-%d %H:%M:%S"), "VARIATION",
                       {"old_temp": t0, "new_temp": t1, "effect_desc": effect})
        await asyncio.to_thread(
            push_eink, current_mood, FIXED_BPM, current_temp, "Live remix variation")
    except Exception as e:
        print(f"❌ 변주 적용 실패: {e}")


# ===========================================================================
# 백그라운드 태스크
# ===========================================================================
async def receive_audio_task(session):
    async for message in session.receive():
        if message.server_content and message.server_content.audio_chunks:
            for chunk in message.server_content.audio_chunks:
                if chunk.data:
                    if audio_queue.qsize() > QUEUE_MAX_CHUNKS:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                    audio_queue.put(chunk.data)


async def serial_listener_task(client, session, ser):
    """CP 버튼 신호 수신. CP가 auto-reload로 재시작하면 USB 시리얼이 잠깐
    끊겼다 재연결될 수 있으므로, readline 예외를 삼키고 계속 시도한다."""
    global continuous_mode
    print("🔌 CPX 버튼 리스너 가동")
    while True:
        try:
            line = await asyncio.to_thread(ser.readline)
        except Exception as e:
            # CP 재시작 중 포트가 잠깐 사라질 수 있음 → 잠시 쉬고 재시도
            print(f"[serial] 읽기 일시 오류(아마 CP 재시작): {e}")
            await asyncio.sleep(0.5)
            continue
        if not line:
            await asyncio.sleep(0.02)
            continue
        cmd = line.decode(errors="ignore").strip().upper()
        if cmd not in ("A", "B"):
            continue
        print(f"📥 [CPX] {cmd}")
        if cmd == "A":
            continuous_mode = not continuous_mode
            print(f"📢 자동촬영 {'🔴 ON' if continuous_mode else '⚪ OFF'} (20~40초 간격)")
        elif cmd == "B":
            asyncio.create_task(apply_random_variation(session))
        await asyncio.sleep(0.05)


async def auto_shoot_loop_task(client, session):
    global continuous_mode
    while True:
        if continuous_mode:
            interval = random.randint(20, 40)
            print(f"⏱️ 다음 스캔까지 {interval}초")
            for _ in range(interval):
                if not continuous_mode:
                    break
                await asyncio.sleep(1)
            if continuous_mode:
                await process_vision_to_music(client, session, "AUTO_A")
        else:
            await asyncio.sleep(1)


# ===========================================================================
# 메인
# ===========================================================================
def open_serial():
    """CP의 data 시리얼 채널을 연다.
    1순위: pyserial list_ports로 CircuitPython 'data' 인터페이스를 이름으로 탐색
           (console/data 포트가 섞여 있어도 data를 정확히 집어냄)
    2순위: SERIAL_CANDIDATES 순서(ACM1 우선)로 폴백."""
    # --- 1순위: 포트 메타데이터로 data 채널 식별 ---
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        # CircuitPython data 채널은 description/interface에 단서가 있는 경우가 많다.
        # 'data', 'CDC2', 'CircuitPython'을 우선 후보로.
        def score(p):
            text = " ".join(str(x) for x in
                            [p.description, p.interface, p.product, p.device]).lower()
            s = 0
            if "data" in text: s += 3
            if "cdc2" in text or "cdc 2" in text: s += 3
            if "circuitpython" in text: s += 1
            # console로 보이면 감점
            if "console" in text or "repl" in text: s -= 3
            return s
        cand = sorted(ports, key=score, reverse=True)
        for p in cand:
            if score(p) > 0:
                try:
                    s = serial.Serial(p.device, BAUD, timeout=0.5)
                    print(f"✅ CPX data 채널 연결(자동): {p.device}  [{p.description}]")
                    return s
                except Exception as e:
                    print(f"⚠️ {p.device} 열기 실패: {e}")
    except Exception as e:
        print(f"[serial] 자동 탐색 건너뜀: {e}")

    # --- 2순위: 후보 순서대로 폴백(ACM1 우선) ---
    for port in SERIAL_CANDIDATES:
        if os.path.exists(port):
            try:
                s = serial.Serial(port, BAUD, timeout=0.5)
                print(f"✅ CPX 시리얼 연결(폴백): {port}")
                return s
            except Exception as e:
                print(f"⚠️ {port} 열기 실패: {e}")
    print("❌ CPX 시리얼 포트를 못 찾음")
    return None


async def run_session(client, ser, first_boot):
    """Lyria 세션 1회 수명. 끊기면 예외가 위로 전파되어 main이 재연결한다.
    재연결이어도 마지막 상태(current_*)를 그대로 복구한다."""
    global current_prompt
    print("🌐 Lyria 실시간 스트리밍 접속 중...")
    async with (
        client.aio.live.music.connect(model="models/lyria-realtime-exp") as session,
        asyncio.TaskGroup() as tg,
    ):
        tg.create_task(receive_audio_task(session))
        tg.create_task(serial_listener_task(client, session, ser))
        tg.create_task(auto_shoot_loop_task(client, session))

        # 첫 부팅이면 기본 프롬프트, 재연결이면 마지막 프롬프트를 복구
        if first_boot or not current_prompt:
            current_prompt = DEFAULT_PROMPT
        restore_prompt = current_prompt
        print(f"{'🎬 첫 접속' if first_boot else '🔄 재연결 — 마지막 상태 복구'}: "
              f"prompt='{restore_prompt[:40]}...' "
              f"temp={current_temp} density={current_density} bright={current_brightness}")

        await session.set_weighted_prompts(
            prompts=[types.WeightedPrompt(text=restore_prompt, weight=1.0)])
        await session.set_music_generation_config(
            config=types.LiveMusicGenerationConfig(
                bpm=FIXED_BPM,
                temperature=current_temp,
                density=current_density,
                brightness=current_brightness,
            ))
        await session.play()
        print("🎵 온에어!")

        # E-Ink: 첫 부팅 때만 강제 표시(재연결 때는 180초 가드에 맡김)
        if first_boot:
            await asyncio.to_thread(
                push_eink, "On Air", FIXED_BPM, current_temp, restore_prompt[:120], True)

        while True:
            await asyncio.sleep(1)


async def main():
    if not os.environ.get("GEMINI_API_KEY"):
        print("⚠️ GEMINI_API_KEY 가 .env 에 없을 수 있음(클라이언트 생성은 시도).")

    play_thread = threading.Thread(target=audio_playback_thread, daemon=True)
    play_thread.start()

    client = genai.Client(http_options={"api_version": "v1alpha"})
    ser = open_serial()
    if ser is None:
        return

    # --- 재연결 루프 ---
    # Lyria는 실험 모델이라 서버측 1011(서비스 불가)로 끊길 수 있다.
    # 끊기면 backoff 후 마지막 상태로 자동 재접속한다.
    first_boot = True
    backoff = 2.0           # 초기 재시도 대기(초)
    BACKOFF_MAX = 30.0      # 최대 대기
    while True:
        try:
            await run_session(client, ser, first_boot)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except BaseException as e:
            # TaskGroup은 ExceptionGroup으로 감싸 던지므로 풀어서 원인 확인
            msgs = []
            if isinstance(e, BaseExceptionGroup):
                for sub in e.exceptions:
                    msgs.append(f"{type(sub).__name__}: {sub}")
            else:
                msgs.append(f"{type(e).__name__}: {e}")
            reason = " | ".join(msgs)
            print(f"\n⚠️ Lyria 세션 끊김 → {backoff:.0f}초 후 재연결. 원인: {reason}")
            # 재생 스레드는 살아있고, 큐가 마르면 알아서 재충전 대기로 들어간다.
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, BACKOFF_MAX)   # 지수 backoff
            first_boot = False
            continue
        else:
            # run_session이 정상 반환(이론상 무한루프라 거의 없음) → 그래도 재시도
            first_boot = False
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ 방송 종료")
