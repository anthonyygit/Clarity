import os
import base64
import tempfile
import subprocess
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from anthropic import Anthropic
from elevenlabs.client import ElevenLabs
from PIL import Image
import io
from deepgram import DeepgramClient
import threading
import time

try:
    from pillow_heif import register_heic_opener
    register_heic_opener()
except ImportError:
    pass

load_dotenv()

app = FastAPI(title="AI Glasses Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

anthropic_client = Anthropic()
elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

el_voice_id="lxYfHSkYm1EzQzGhdbfc"

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
SPEED_STEP = 0.1
SPEED_MIN = 0.7
SPEED_MAX = 1.2


def load_settings() -> dict:
    import json
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings: dict):
    import json
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f)


speech_speed = load_settings().get("speed", 1.0)


def adjust_speech_speed(delta: float) -> float:
    global speech_speed
    speech_speed = min(max(round(speech_speed + delta, 2), SPEED_MIN), SPEED_MAX)
    settings = load_settings()
    settings["speed"] = speech_speed
    save_settings(settings)
    return speech_speed


_SETTINGS_PHRASES = (
    ("volume_max", ("as high as it goes", "max volume", "maximum volume",
                     "full volume", "loudest", "all the way up")),
    ("volume_min", ("as low as it goes", "min volume", "minimum volume",
                     "mute", "silent", "all the way down")),
    ("volume_up", ("volume up", "turn it up", "turn up the volume",
                    "louder", "increase the volume")),
    ("volume_down", ("volume down", "turn it down", "turn down the volume",
                      "quieter", "softer", "decrease the volume", "lower the volume")),
    ("speed_up", ("talk faster", "speak faster", "speed up", "faster please")),
    ("speed_down", ("talk slower", "speak slower", "slow down", "slower please")),
)


def match_settings_command(transcript: str):
    lowered = transcript.strip().lower()
    for name, phrases in _SETTINGS_PHRASES:
        if any(p in lowered for p in phrases):
            return name
    return None


anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")

if not anthropic_api_key:
    raise ValueError("ANTHROPIC_API_KEY environment variable not set")
if not elevenlabs_api_key:
    raise ValueError("ELEVENLABS_API_KEY environment variable not set")
if not deepgram_api_key:
    raise ValueError("DEEPGRAM_API_KEY environment variable not set")

audio_buffer = bytearray()
buffer_lock = threading.Lock()
last_chunk_time = time.time()
recording_session_id = None
last_saved_audio_path = None
pending_response_text = None

active_task = None
last_task_photo = None

# Live Deepgram transcription, run alongside the existing buffered/batch
# path (never replacing it). Every stage is wrapped so any failure here
# just leaves live_dg_client as None, and /transcribe/done transparently
# falls back to the proven batch transcribe_file() call on the full
# recording — this can never make transcription worse than it was before,
# only sometimes faster.
live_dg_ctx = None
live_dg_client = None
live_dg_reader_thread = None
live_dg_finalize_event = threading.Event()
live_dg_transcript_lock = threading.Lock()
live_dg_transcript_parts = []


def _live_dg_reader(client, finalize_event):
    global live_dg_transcript_parts
    try:
        for message in client:
            if isinstance(message, (bytes, bytearray)):
                continue
            if type(message).__name__ != "ListenV1Results":
                continue
            try:
                alt = message.channel.alternatives[0]
            except Exception:
                continue
            text = (alt.transcript or "").strip()
            if text and (message.speech_final or message.is_final):
                with live_dg_transcript_lock:
                    live_dg_transcript_parts.append(text)
            if getattr(message, "from_finalize", False):
                finalize_event.set()
    except Exception as e:
        print(f"live deepgram reader stopped: {e}")
    finally:
        finalize_event.set()


def start_live_transcription():
    global live_dg_ctx, live_dg_client, live_dg_reader_thread, live_dg_transcript_parts
    live_dg_transcript_parts = []
    live_dg_finalize_event.clear()
    try:
        dg = DeepgramClient(api_key=deepgram_api_key)
        ctx = dg.listen.v1.connect(
            model="nova-2",
            encoding="linear16",
            sample_rate=16000,
            channels=1,
            smart_format=True,
            language="en",
            interim_results=True,
        )
        client = ctx.__enter__()
        live_dg_ctx = ctx
        live_dg_client = client
        live_dg_reader_thread = threading.Thread(
            target=_live_dg_reader, args=(client, live_dg_finalize_event), daemon=True
        )
        live_dg_reader_thread.start()
    except Exception as e:
        print(f"live deepgram connect failed, will use batch transcription instead: {e}")
        live_dg_ctx = None
        live_dg_client = None


def feed_live_transcription(chunk: bytes):
    global live_dg_client
    if live_dg_client is None:
        return
    try:
        live_dg_client.send_media(chunk)
    except Exception as e:
        print(f"live deepgram send failed, falling back to batch: {e}")
        live_dg_client = None


def finish_live_transcription(timeout: float = 1.5):
    """Finalize + close the live connection and return the transcript, or
    None if the live path never worked this turn (caller falls back to the
    existing batch transcription of the full recording)."""
    global live_dg_ctx, live_dg_client, live_dg_reader_thread
    client = live_dg_client
    ctx = live_dg_ctx
    live_dg_client = None
    live_dg_ctx = None
    if client is None:
        return None

    try:
        live_dg_finalize_event.clear()
        client.send_finalize()
        live_dg_finalize_event.wait(timeout=timeout)
    except Exception as e:
        print(f"live deepgram finalize failed: {e}")

    try:
        client.send_close_stream()
    except Exception:
        pass
    if live_dg_reader_thread:
        live_dg_reader_thread.join(timeout=1.0)
    try:
        ctx.__exit__(None, None, None)
    except Exception:
        pass

    with live_dg_transcript_lock:
        parts = list(live_dg_transcript_parts)
    if not parts:
        return None
    return " ".join(parts).strip()


def abandon_live_transcription():
    """Close the live connection quickly without waiting for a final
    transcript — used when a recording is aborted/reset rather than
    finished, so a dangling websocket doesn't leak between turns."""
    global live_dg_ctx, live_dg_client
    client = live_dg_client
    ctx = live_dg_ctx
    live_dg_client = None
    live_dg_ctx = None
    if client is None:
        return
    try:
        client.send_close_stream()
    except Exception:
        pass
    try:
        ctx.__exit__(None, None, None)
    except Exception:
        pass

SCENE_PROMPT = (
    "You are an assistant embedded in smart glasses for a blind person. "
    "Describe what you see in the image in 1-3 clear, natural sentences. "
    "Be concise and focus on what's most important and immediately useful — "
    "objects, people, hazards, context. Do not say 'I see' or 'the image shows'. "
    "Speak directly as if narrating to the person."
)

OCR_PROMPT = ("Extract and read all the text you see in this image. If there is no text, say 'I don't see any text infront of you.'"
              "If the user is requesting you to read a book, then just start reading to them at a normal pace. Pretend you are the user's eyes, as this is meant as a visibility device for the blind."
              "If the book is open so you can see both pages, start from the top left of the left page, so like the first character, and read on from there as a normal person would.")


def load_commands() -> list:
    import json
    commands_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands.json")
    try:
        with open(commands_path) as f:
            return json.load(f)["commands"]
    except Exception as e:
        print(f"Failed to load commands.json: {e}")
        return []


def interpret_command(transcript: str) -> tuple[str, str]:
    import json

    global last_task_photo

    commands = load_commands()
    if not commands:
        return "none", ""

    command_list = "\n".join(
        f"- {c['name']}: {c['description']} Trigger: {c['trigger']}"
        for c in commands
    )
    valid_names = {c["name"] for c in commands}

    content = []
    has_photo = False
    photo = last_task_photo
    if photo:
        try:
            compressed_data, media_type = compress_image(photo)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(compressed_data).decode("utf-8"),
                },
            })
            has_photo = True
        except Exception as e:
            print(f"interpret_command photo attach error: {e}")
        last_task_photo = None
    content.append({"type": "text", "text": transcript})

    message = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1000 if has_photo else 150,
        system=(
            "You are the voice assistant inside smart glasses for a blind user. "
            "Given a transcript of what they said, decide which command they want to run.\n\n"
            f"Available commands:\n{command_list}\n\n"
            + (
                "A photo from the glasses' camera is attached, showing what the user "
                "is currently looking at.\n"
                "If the matched command is 'describe_scene': skip the placeholder "
                "acknowledgment and write the actual scene description directly in "
                "'response', following these guidelines: " + SCENE_PROMPT + "\n"
                "If the matched command is 'read_text': skip the placeholder "
                "acknowledgment and write the actual text read aloud directly in "
                "'response', following these guidelines: " + OCR_PROMPT + "\n"
                "For every other command, keep 'response' as a short, cheerful "
                "acknowledgment as usual.\n\n"
                if has_photo else ""
            )
            + "Also write a short, upbeat spoken confirmation (one sentence, cheerful and "
            "friendly,). If no command matches, the "
            "response should cheerfully say you didn't catch a command. Be creative with your response, mix it up every time! Dont be repetetive."
        ),
        messages=[{"role": "user", "content": content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "response": {"type": "string"},
                    },
                    "required": ["command", "response"],
                    "additionalProperties": False,
                },
            }
        },
    )

    text = next((b.text for b in message.content if b.type == "text"), "{}")
    data = json.loads(text)
    command = data.get("command", "none").strip().lower()
    response = data.get("response", "")

    if command not in valid_names:
        command = "none"

    if has_photo and command in ("describe_scene", "read_text"):
        command = "none"

    return command, response


def start_task(transcript: str) -> str:
    import json

    global active_task, last_task_photo

    content = []
    has_photo = False
    if last_task_photo:
        try:
            compressed_data, media_type = compress_image(last_task_photo)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(compressed_data).decode("utf-8"),
                },
            })
            has_photo = True
        except Exception as e:
            print(f"task photo attach error: {e}")
        last_task_photo = None
    content.append({"type": "text", "text": transcript})

    message = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=600,
        system=(
            "You are a voice assistant guiding a blind or visually impaired user "
            "through a physical, real-world task step by step, entirely by voice, "
            "with no screen. The user just asked for help with a task. Break it "
            "into a short sequence of simple, concrete physical steps (typically "
            "3-8 steps). Each step should be one clear instruction, one or two "
            "sentences, phrased naturally and warmly, assuming the user cannot "
            "see anything. Also write a short warm intro sentence acknowledging "
            "the task, separate from the first step itself."
            + (
                " An attached photo shows what the user's glasses camera "
                "currently sees — use it to tailor the steps to what's actually "
                "in front of them (e.g. what's already out, what's missing), but "
                "don't describe the photo itself."
                if has_photo else ""
            )
        ),
        messages=[{"role": "user", "content": content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "task_name": {"type": "string"},
                        "intro": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["task_name", "intro", "steps"],
                    "additionalProperties": False,
                },
            }
        },
    )

    text = next((b.text for b in message.content if b.type == "text"), "{}")
    data = json.loads(text)
    steps = data.get("steps") or []
    intro = data.get("intro", "")

    if not steps:
        active_task = None
        return intro or "Sorry, I couldn't figure out the steps for that."

    response_text = (intro + " " + steps[0]).strip()
    active_task = {
        "task_name": data.get("task_name", transcript),
        "steps": steps,
        "step_index": 0,
        "history": [
            {"role": "user", "content": transcript},
            {"role": "assistant", "content": response_text},
        ],
    }
    return response_text


_CANCEL_PHRASES = (
    "cancel", "stop", "nevermind", "never mind", "quit", "exit",
    "forget it", "abort", "end task", "i'm done", "im done",
)


def continue_task(transcript: str) -> str:
    import json

    global active_task, last_task_photo

    lowered = transcript.strip().lower()
    if any(p in lowered for p in _CANCEL_PHRASES):
        active_task = None
        last_task_photo = None
        return "Okay, I've stopped the task."

    steps = active_task["steps"]
    idx = active_task["step_index"]
    current_step = steps[idx]
    remaining = steps[idx + 1:]

    content = []
    has_photo = False
    if last_task_photo:
        try:
            compressed_data, media_type = compress_image(last_task_photo)
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(compressed_data).decode("utf-8"),
                },
            })
            has_photo = True
        except Exception as e:
            print(f"task photo attach error: {e}")
        last_task_photo = None
    content.append({"type": "text", "text": transcript})

    message = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        system=(
            "You are guiding a blind or visually impaired user through the task "
            f"'{active_task['task_name']}', speaking one step at a time, no screen.\n"
            f"Current step: \"{current_step}\"\n"
            f"Remaining steps after this one, in order: {remaining}\n\n"
            + (
                "An attached photo shows what the user's glasses camera currently "
                "sees — use it to check whether they've actually done the current "
                "step correctly, spot something they're missing, or answer a "
                "question about what's in front of them. Only mention the photo's "
                "contents if it's relevant to their step or question.\n\n"
                if has_photo else ""
            )
            + "The user just said something in response to the current step. Decide "
            "what they need and set 'action' to one of:\n"
            "- 'advance': they indicated they finished the step (e.g. 'ok', 'done', "
            "'got it', 'next'). Set response to a short, warm transition into the "
            "NEXT step's instruction. If there are no remaining steps, instead give "
            "a short, natural sign-off congratulating them (e.g. 'ok, enjoy your "
            "omelet!').\n"
            "- 'answer': they're asking a question or need help with the CURRENT "
            "step (e.g. 'where's the pan?', 'what do you mean?', 'how long?'). Set "
            "response to a short, helpful answer. Do not reveal or move to future steps.\n"
            "- 'end': they want to stop or cancel the task entirely. Set response to "
            "a short acknowledgement that you've stopped.\n"
            "If it's unclear what they mean, use 'answer' and just restate the "
            "current step clearly. Earlier turns in this conversation are prior "
            "steps and exchanges from this same task — use them for context "
            "(e.g. if they already said they don't have an ingredient or tool).\n\n"
            "Also set 'task_complete': true if your response is a genuine final "
            "sign-off and the real-world task is actually finished now (whether or "
            "not the listed steps technically ran out) — this clears the task so the "
            "next thing they say is treated as a fresh request, not another step. "
            "Set it false for anything still mid-task, including the last step's "
            "instruction itself (only the reply *after* they've completed that last "
            "step is the sign-off)."
        ),
        messages=active_task["history"] + [{"role": "user", "content": content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["advance", "answer", "end"]},
                        "response": {"type": "string"},
                        "task_complete": {"type": "boolean"},
                    },
                    "required": ["action", "response", "task_complete"],
                    "additionalProperties": False,
                },
            }
        },
    )

    text = next((b.text for b in message.content if b.type == "text"), "{}")
    data = json.loads(text)
    action = data.get("action", "answer")
    response_text = data.get("response", "")
    task_complete = bool(data.get("task_complete", False))

    active_task["history"].append({"role": "user", "content": transcript})
    active_task["history"].append({"role": "assistant", "content": response_text})
    max_history = 20
    if len(active_task["history"]) > max_history:
        active_task["history"] = active_task["history"][-max_history:]

    if action == "advance":
        active_task["step_index"] += 1

    if action == "end":
        active_task = None
    elif task_complete or active_task["step_index"] >= len(steps):
        active_task = None

    return response_text


if not anthropic_api_key:
    raise ValueError("ANTHROPIC_API_KEY environment variable not set")
if not elevenlabs_api_key:
    raise ValueError("ELEVENLABS_API_KEY environment variable not set")


def save_audio_as_wav(audio_data: bytes, sample_rate: int = 16000) -> str:
    import wave

    os.makedirs("./logs/recordings", exist_ok=True)

    timestamp = int(time.time() * 1000)
    wav_path = f"./logs/recordings/recording_{timestamp}.wav"

    duration_seconds = len(audio_data) / (sample_rate * 2)

    with wave.open(wav_path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data)

    print(f"Audio saved: {len(audio_data)} bytes = {duration_seconds:.1f}s @ {sample_rate}Hz")
    return wav_path


def compress_image(image_data: bytes, max_size_mb: float = 9) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(image_data))
    max_bytes = int(max_size_mb * 1024 * 1024)

    if img.width > 4096 or img.height > 4096:
        img.thumbnail((4096, 4096), Image.Resampling.LANCZOS)

    if img.mode == "RGBA":
        rgb_img = Image.new("RGB", img.size, (255, 255, 255))
        rgb_img.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
        img = rgb_img

    quality = 75
    while quality >= 10:
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        compressed = output.getvalue()

        if len(compressed) <= max_bytes:
            return compressed, "image/jpeg"
        quality -= 5

    img.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=40, optimize=True)
    return output.getvalue(), "image/jpeg"


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe_chunk(request: Request):
    global audio_buffer, last_chunk_time

    try:
        chunk = await request.body()
        chunk_size = len(chunk)

        with buffer_lock:
            is_first_chunk = len(audio_buffer) == 0
            audio_buffer.extend(chunk)
            last_chunk_time = time.time()
            total = len(audio_buffer)
            duration = total / (16000 * 2)

            print(f"[CHUNK] +{chunk_size:5}B | Total: {total:7}B ({duration:.2f}s)")

        if is_first_chunk:
            start_live_transcription()
        feed_live_transcription(chunk)

        return {"status": "buffered", "size": len(audio_buffer)}

    except Exception as e:
        print(f"Error in /transcribe: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/transcribe/done")
async def transcribe_done():
    global audio_buffer, recording_session_id, last_saved_audio_path

    try:
        with buffer_lock:
            if len(audio_buffer) == 0:
                print("Error: No audio data in buffer")
                return {"error": "No audio data"}

            audio_data = bytes(audio_buffer)
            print(f"Total audio data: {len(audio_data)} bytes")
            audio_buffer.clear()

        wav_path = save_audio_as_wav(audio_data)
        last_saved_audio_path = wav_path

        transcript = finish_live_transcription()
        if transcript:
            print(f"Live transcript: {transcript!r}")
        else:
            try:
                deepgram = DeepgramClient(api_key=deepgram_api_key)
                with open(wav_path, "rb") as f:
                    wav_bytes = f.read()
                response = deepgram.listen.v1.media.transcribe_file(
                    request=wav_bytes,
                    model="nova-2",
                    smart_format=True,
                    language="en",
                )
                transcript = response.results.channels[0].alternatives[0].transcript
            except Exception as deepgram_error:
                print(f"Deepgram error: {deepgram_error}")
                transcript = "[Transcription failed]"

        print(f"Recording saved: {wav_path}")

        command = "none"
        response_text = ""
        if transcript and not transcript.startswith("["):
            try:
                settings_command = match_settings_command(transcript)
                if settings_command == "speed_up":
                    adjust_speech_speed(SPEED_STEP)
                    response_text = "Sure, talking a bit faster now."
                elif settings_command == "speed_down":
                    adjust_speech_speed(-SPEED_STEP)
                    response_text = "Okay, slowing down a bit."
                elif settings_command in ("volume_up", "volume_down", "volume_max", "volume_min"):
                    command = settings_command
                    response_text = {
                        "volume_up": "Sure, turning it up a bit.",
                        "volume_down": "Okay, turning it down a bit.",
                        "volume_max": "Cranking it all the way up.",
                        "volume_min": "Turning it all the way down.",
                    }[settings_command]
                elif active_task is not None:
                    response_text = continue_task(transcript)
                else:
                    command, response_text = interpret_command(transcript)
                    if command == "start_task":
                        response_text = start_task(transcript)
                        command = "none"
                    elif command == "speed_up":
                        adjust_speech_speed(SPEED_STEP)
                        response_text = "Sure, talking a bit faster now."
                        command = "none"
                    elif command == "speed_down":
                        adjust_speech_speed(-SPEED_STEP)
                        response_text = "Okay, slowing down a bit."
                        command = "none"
            except Exception as e:
                print(f"Command interpretation error: {e}")
        print(f"Transcript: {transcript!r} -> Command: {command} | Response: {response_text!r}")

        global pending_response_text
        if response_text:
            pending_response_text = response_text

        return {
            "command": command,
            "transcript": transcript,
            "response": response_text,
            "task_active": active_task is not None,
        }

    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/transcribe/reset")
async def transcribe_reset():
    global audio_buffer

    abandon_live_transcription()
    with buffer_lock:
        audio_buffer.clear()

    return {"status": "buffer cleared"}


@app.post("/scene/raw")
async def scene_from_glasses(request: Request):
    global pending_response_text

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")

        os.makedirs("./logs/photos", exist_ok=True)
        timestamp = int(time.time() * 1000)
        photo_path = f"./logs/photos/scene_{timestamp}.jpg"
        with open(photo_path, "wb") as f:
            f.write(image_data)

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        message = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            system=SCENE_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": "You are an AI Assistant inside a pair of smart glasses for a blind or partially blind individual. Be specific, as well as pointing out any hazards sharp stuff, hot stuffs, or any important information."},
                    ],
                }
            ],
        )
        description = next(
            (b.text for b in message.content if b.type == "text"), ""
        )
        print(f"Scene: {description}")

        pending_response_text = description

        return {"description": description, "photo": photo_path}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /scene/raw: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/ocr/raw")
async def ocr_from_glasses(request: Request):
    global pending_response_text

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")

        os.makedirs("./logs/photos", exist_ok=True)
        timestamp = int(time.time() * 1000)
        photo_path = f"./logs/photos/ocr_{timestamp}.jpg"
        with open(photo_path, "wb") as f:
            f.write(image_data)

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        message = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
        )
        extracted_text = next(
            (b.text for b in message.content if b.type == "text"), ""
        )
        print(f"OCR: {extracted_text[:120]}...")

        pending_response_text = extracted_text

        return {"text": extracted_text, "photo": photo_path}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /ocr/raw: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/photo")
async def receive_photo(request: Request):
    try:
        photo_data = await request.body()
        if not photo_data:
            raise HTTPException(status_code=400, detail="Empty photo")

        os.makedirs("./logs/photos", exist_ok=True)
        timestamp = int(time.time() * 1000)
        photo_path = f"./logs/photos/photo_{timestamp}.jpg"
        with open(photo_path, "wb") as f:
            f.write(photo_data)

        print(f"Photo saved: {photo_path} ({len(photo_data)} bytes)")
        return {"saved": photo_path, "bytes": len(photo_data)}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /photo: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/task/photo")
async def task_photo(request: Request):
    global last_task_photo

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_task_photo = image_data
        return {"status": "ok", "bytes": len(image_data)}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in /task/photo: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.get("/response/latest")
async def response_latest():
    global pending_response_text

    text = pending_response_text
    if not text:
        raise HTTPException(status_code=404, detail="No response audio available")
    pending_response_text = None

    def generate():
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=el_voice_id,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",
            voice_settings={"speed": speech_speed},
        )
        for chunk in audio_stream:
            yield chunk

    return StreamingResponse(generate(), media_type="application/octet-stream")


@app.get("/playback/latest")
async def playback_latest():
    global last_saved_audio_path

    if not last_saved_audio_path or not os.path.exists(last_saved_audio_path):
        raise HTTPException(status_code=404, detail="No recording available")

    return FileResponse(
        path=last_saved_audio_path,
        media_type="audio/wav",
        filename="recording.wav",
    )


@app.post("/scene")
async def scene_description(image: UploadFile = File(...)):
    if not image:
        raise HTTPException(status_code=400, detail="No image provided")

    try:
        image_data = await image.read()

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        message = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            system=SCENE_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": "Describe this scene."},
                    ],
                }
            ],
        )

        description = message.content[0].text

        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=description,
            voice_id=el_voice_id,
            model_id="eleven_turbo_v2_5",
            output_format="mp3_22050_32",
        )

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_file:
            mp3_path = mp3_file.name
            for chunk in audio_stream:
                mp3_file.write(chunk)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
            wav_path = wav_file.name

        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                mp3_path,
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-y",
                wav_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")

        os.unlink(mp3_path)

        return FileResponse(
            path=wav_path,
            media_type="audio/wav",
            filename="scene.wav",
            headers={"Content-Disposition": "attachment; filename=scene.wav"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/ocr")
async def ocr_reader(image: UploadFile = File(...), system_prompt: str = None):
    if not image:
        raise HTTPException(status_code=400, detail="No image provided")

    try:
        image_data = await image.read()

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        prompt_to_use = system_prompt if system_prompt else OCR_PROMPT

        message = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt_to_use},
                    ],
                }
            ],
        )

        extracted_text = message.content[0].text
        full_text = f"{extracted_text}"

        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=full_text,
            voice_id=el_voice_id,
            model_id="eleven_turbo_v2_5",
            output_format="mp3_22050_32",
        )

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_file:
            mp3_path = mp3_file.name
            for chunk in audio_stream:
                mp3_file.write(chunk)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
            wav_path = wav_file.name

        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                mp3_path,
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-y",
                wav_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")

        os.unlink(mp3_path)

        return FileResponse(
            path=wav_path,
            media_type="audio/wav",
            filename="ocr.wav",
            headers={"Content-Disposition": "attachment; filename=ocr.wav"},
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="172.20.10.4", port=8000)
