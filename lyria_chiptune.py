import asyncio
import queue
import threading
import pyaudio
from google import genai
from google.genai import types
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# Lyria RealTime 공식 오디오 스펙
FORMAT = pyaudio.paInt16
CHANNELS = 2
RATE = 48000
CHUNK_SIZE = 2048

# [수정] ReSpeaker 장치 인덱스 (audiolist 기준 0번)
RESPEAKER_INDEX = 0

audio_queue = queue.Queue()
BUFFER_PRE_ROLL = 50  # 선행 버퍼링 청크 개수
is_playing = False


def audio_playback_thread():
    """오디오 큐에서 데이터를 받아 ReSpeaker로 출력하는 독립 스레드"""
    global is_playing
    p = pyaudio.PyAudio()

    try:
        print(f"🔈 출력 장치 오픈 시도: {p.get_device_info_by_index(RESPEAKER_INDEX)['name']}")
        stream = p.open(format=FORMAT,
                        channels=CHANNELS,
                        rate=RATE,
                        output=True,
                        frames_per_buffer=CHUNK_SIZE,
                        output_device_index=RESPEAKER_INDEX)
    except Exception as e:
        print(f"❌ ReSpeaker 장치 오픈 실패 (인덱스 {RESPEAKER_INDEX}): {e}")
        print("💡 팁: audiolist.py 다시 돌려서 인덱스 확인해봐.")
        return

    print(f"🎵 Lyria 오디오 스트림 대기 중... ReSpeaker(장치 {RESPEAKER_INDEX}) 버퍼를 채우고 있습니다.")

    while True:
        if not is_playing:
            if audio_queue.qsize() >= BUFFER_PRE_ROLL:
                print("▶️ 안정 버퍼 확보 완료! 실시간 재생을 시작합니다!")
                is_playing = True
            else:
                threading.Event().wait(0.1)
                continue

        try:
            data = audio_queue.get(timeout=1)
            stream.write(data)
            audio_queue.task_done()
        except queue.Empty:
            print("⚠️ 네트워크 밀림 감지! 버퍼 재충전을 위해 소리를 잠시 멈춥니다.")
            is_playing = False


async def receive_audio_task(session):
    """Google 서버로부터 실시간 오디오 메시지를 받아 큐에 쌓는 백그라운드 태스크"""
    print("🚀 Lyria 서버로부터 실시간 오디오 수신 루프 가동!")
    count = 0
    try:
        async for message in session.receive():
            if message.server_content and message.server_content.audio_chunks:
                for chunk in message.server_content.audio_chunks:
                    if chunk.data:
                        audio_queue.put(chunk.data)
                        count += 1
                        if count % 10 == 0:
                            print(f"📦 청크 {count}개 수신, 큐 크기: {audio_queue.qsize()}")
            else:
                print(f"📨 오디오 없는 메시지 수신: {message}")
    except Exception as e:
        print(f"❌ 데이터 수신 중 오류 발생: {e}")


async def main():
    # 백그라운드 재생 스레드 시작
    play_thread = threading.Thread(target=audio_playback_thread, daemon=True)
    play_thread.start()

    # 실험용 v1alpha 버전 세팅 유지
    client = genai.Client(http_options={'api_version': 'v1alpha'})

    print("🌐 Lyria RealTime v1alpha 서버에 WebSocket 연결 시도 중...")

    async with (
        client.aio.live.music.connect(model='models/lyria-realtime-exp') as session,
        asyncio.TaskGroup() as tg,
    ):
        tg.create_task(receive_audio_task(session))

        # 프롬프트 조타: Chiptune + Ambient 레트로 우주 감성 무드
        prompt_text = (
            "Chiptune combined with ethereal Ambient soundscape. "
            "8-bit retro gaming synthesizer melodies floating over spacey synth pads. "
            "Dreamy, nostalgic, relaxing, and bright electronic textures."
        )

        await session.set_weighted_prompts(
            prompts=[
                types.WeightedPrompt(text=prompt_text, weight=1.0)
            ]
        )

        # 잔잔하고 감성적인 여백을 위해 밀도(Density) 낮게 조율
        await session.set_music_generation_config(
            config=types.LiveMusicGenerationConfig(
                bpm=80,
                temperature=1.0
            )
        )

        # 재생 스타트
        await session.play()
        print("🎬 play() 호출 완료. 오디오 수신 대기 중...")

        while True:
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ 스트림월드 라디오를 안전하게 종료합니다.")
