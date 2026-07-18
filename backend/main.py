import os
import sys
import asyncio
import collections
import datetime
import base64
import tempfile
import subprocess
import traceback
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from starlette.requests import ClientDisconnect
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
import anthropic
from anthropic import Anthropic
from elevenlabs.client import ElevenLabs
from PIL import Image
import io
from deepgram import DeepgramClient
from groq import Groq
import typesense
import threading
import time

try:
    from pillow_heif import register_heic_opener
    register_heic_opener()
except ImportError:
    pass

load_dotenv()

_debug_log = collections.deque(maxlen=300)
mode = "wifi"

class _TeeStdout:
    def __init__(self, original):
        self._original = original

    def write(self, data):
        self._original.write(data)
        if data.strip():
            _debug_log.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {data.rstrip()}")

    def flush(self):
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


sys.stdout = _TeeStdout(sys.stdout)

app = FastAPI(title="AI Glasses Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_server_start_time = time.time()
_stats_lock = threading.Lock()
_route_stats = collections.defaultdict(
    lambda: {"count": 0, "errors": 0, "total_ms": 0.0, "min_ms": None, "max_ms": None, "last_ms": None, "last_at": None}
)
_command_stats = collections.defaultdict(
    lambda: {"count": 0, "errors": 0, "total_ms": 0.0, "min_ms": None, "max_ms": None, "last_ms": None, "last_at": None}
)

last_glasses_contact = None
GLASSES_CONTACT_TIMEOUT_S = 15
_DEBUG_PATH_PREFIX = "/debug"


def _record_stat(stats_dict, key, elapsed_ms, error=False):
    with _stats_lock:
        s = stats_dict[key]
        s["count"] += 1
        if error:
            s["errors"] += 1
        s["total_ms"] += elapsed_ms
        s["last_ms"] = elapsed_ms
        s["last_at"] = time.time()
        if s["min_ms"] is None or elapsed_ms < s["min_ms"]:
            s["min_ms"] = elapsed_ms
        if s["max_ms"] is None or elapsed_ms > s["max_ms"]:
            s["max_ms"] = elapsed_ms


def _stats_summary(stats_dict):
    with _stats_lock:
        return {
            key: {
                "count": s["count"],
                "errors": s["errors"],
                "avg_ms": round(s["total_ms"] / s["count"], 1) if s["count"] else 0,
                "min_ms": round(s["min_ms"], 1) if s["min_ms"] is not None else None,
                "max_ms": round(s["max_ms"], 1) if s["max_ms"] is not None else None,
                "last_ms": round(s["last_ms"], 1) if s["last_ms"] is not None else None,
                "last_at": s["last_at"],
            }
            for key, s in sorted(stats_dict.items())
        }


async def _timed_threadpool(stat_key, fn, *args, **kwargs):
    """Like run_in_threadpool, but records timing + error count under
    stat_key in _command_stats for the debug panel."""
    t0 = time.time()
    try:
        result = await run_in_threadpool(fn, *args, **kwargs)
        _record_stat(_command_stats, stat_key, (time.time() - t0) * 1000)
        return result
    except Exception:
        _record_stat(_command_stats, stat_key, (time.time() - t0) * 1000, error=True)
        raise


@app.middleware("http")
async def stats_middleware(request: Request, call_next):
    global last_glasses_contact
    start = time.time()
    path = request.url.path
    is_debug_path = path.startswith(_DEBUG_PATH_PREFIX)
    try:
        response = await call_next(request)
        elapsed_ms = (time.time() - start) * 1000
        _record_stat(_route_stats, path, elapsed_ms, error=response.status_code >= 500)
        if not is_debug_path and response.status_code < 500:
            last_glasses_contact = time.time()
        return response
    except Exception:
        elapsed_ms = (time.time() - start) * 1000
        _record_stat(_route_stats, path, elapsed_ms, error=True)
        raise


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


speech_speed = min(max(load_settings().get("speed", 1.0), SPEED_MIN), SPEED_MAX)


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
    ("walk_interval_3", ("interval to 3 seconds", "interval 3 seconds", "every 3 seconds",
                          "walking interval 3", "check every 3 seconds")),
    ("walk_interval_5", ("interval to 5 seconds", "interval 5 seconds", "every 5 seconds",
                          "walking interval 5", "check every 5 seconds")),
    ("walk_interval_ondemand", ("only when needed", "on demand", "as needed",
                                 "walking interval on demand", "only tell me when needed")),
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
groq_api_key = os.getenv("GROQ_API_KEY")

if not anthropic_api_key:
    raise ValueError("ANTHROPIC_API_KEY environment variable not set")
if not elevenlabs_api_key:
    raise ValueError("ELEVENLABS_API_KEY environment variable not set")
if not deepgram_api_key:
    raise ValueError("DEEPGRAM_API_KEY environment variable not set")

groq_client = Groq(api_key=groq_api_key) if groq_api_key else None

typesense_client = typesense.Client({
    "nodes": [{
        "host": os.getenv("TYPESENSE_HOST", "localhost"),
        "port": os.getenv("TYPESENSE_PORT", "443"),
        "protocol": os.getenv("TYPESENSE_PROTOCOL", "https"),
    }],
    "api_key": os.getenv("TYPESENSE_API_KEY", "not_configured"),
    "connection_timeout_seconds": 5,
})

audio_buffer = bytearray()
buffer_lock = threading.Lock()
last_chunk_time = time.time()
recording_session_id = None
last_saved_audio_path = None
pending_response_text = None

active_task = None
last_task_photo = None

rigged_mode = 0
rigged_pending_start = False
rigged_advance_pending = False

RIGGED_LINE_2 = "Small lobby seating nook: two patterned armchairs on beige carpet, a city-lights photo on the wall to your left, and an open doorway ahead leading to a lounge with a stone fireplace and dark wood floors."

RIGGED_TASK_1 = {
    "task_name": "hotel coffee",
    "intro": "Got it, looks like you've got everything out.",
    "steps": [
        "First, grab an empty cup from the tray on the far left of the counter.",
        "Now move right to the coffee airpots. The first one, closest to you, is labeled Regular Coffee — press the lever at its base to pour, and hold it for about 3 seconds.",

        "That should be enough. Just to the left of the coffee airpots is a white pump bottle of creamer with a red top — press it a couple times into your cup.",
        "Lids are back on the left side of the counter, on the second tray next to the cups — put one on.",
    ],
    "sign_off": "You're all set, enjoy your coffee.",
}

last_debug_image = None

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

WALKING_PROMPT = (
    "You are narrating a live camera feed for a blind person who is actively "
    "walking, so they don't step into danger. Check these categories in "
    "order, and alert on the FIRST one that applies — do not default to "
    "'clear' unless you have genuinely checked all of them and none apply:\n"
    "1. Roads/vehicles: a street, driveway, crosswalk, parking lot, railroad "
    "tracks, or any moving car, bike, scooter, motorcycle, or bus ahead of "
    "them. e.g. 'street ahead', 'car approaching', 'bike coming'.\n"
    "2. Anything that could directly injure them: a knife, axe, blade, gun, "
    "power tool, chainsaw, fire, flame, smoke, exposed wiring, a sparking "
    "outlet, broken glass, or any weapon or sharp object visible anywhere "
    "in frame — whether or not it's directly in their path, and whether or "
    "not anyone is holding it. As urgent as roads/vehicles. e.g. 'knife on "
    "the counter', 'fire ahead', 'broken glass on the ground'.\n"
    "3. Animals: a dog (especially loose or aggressive-looking), or any "
    "other animal in their path.\n"
    "4. Elevation and surface hazards: stairs up or down, curbs, "
    "drop-offs, steep ramps, potholes, an open manhole or hole, uneven or "
    "broken pavement, a wet floor, a spill, ice, or standing water.\n"
    "5. Overhead hazards: low-hanging branches, awnings, scaffolding, "
    "signs, or a door frame low enough to hit their head.\n"
    "6. Temporary or construction hazards: cones, barriers, caution tape, "
    "a ladder, construction equipment, or wet paint.\n"
    "7. Trip/collision hazards directly in their path: poles, furniture, "
    "trash cans, cords or cables on the ground, an open door, a glass "
    "door, or people.\n"
    "Always name the specific thing, never say a vague word like 'obstacle', "
    "'object', or 'something' — say what it actually is, e.g. 'trash can "
    "ahead', 'low branch', 'person on your left', 'stairs down', 'wire "
    "hanging'. If you genuinely can't tell what it is, describe it visually "
    "instead of using a placeholder word, e.g. 'dark shape on the ground' "
    "or 'thin wire hanging' — still concrete, just honest about the "
    "uncertainty. "
    "Respond in one short phrase, under 8 words, no greetings, no filler, "
    "no full sentences — just the alert itself. "
    "Only say exactly 'clear' if you have checked all the categories above "
    "and nothing in any of them is visible."
)

def _describe_hazard_groq(image_b64: str, media_type: str) -> str:
    completion = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        max_tokens=60,
        messages=[
            {"role": "system", "content": WALKING_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                    },
                ],
            },
        ],
    )
    return (completion.choices[0].message.content or "").strip()


def _describe_hazard_claude(image_b64: str, media_type: str) -> str:
    message = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=60,
        system=WALKING_PROMPT,
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
                ],
            }
        ],
    )
    return next((b.text for b in message.content if b.type == "text"), "").strip()


def describe_walking_frame(image_b64: str, media_type: str) -> str:
    """Groq first — its LPU inference is dramatically faster than Claude for
    a short-output task like this, and walking mode is latency-sensitive by
    nature. Falls back to Claude automatically if Groq isn't configured or
    the call fails, so walking mode never just goes silent because one
    provider had an issue."""
    if groq_client is not None:
        try:
            return _describe_hazard_groq(image_b64, media_type)
        except Exception as e:
            print(f"walk tick: Groq failed, falling back to Claude: {e}")
    return _describe_hazard_claude(image_b64, media_type)


TASK_READINESS_PROMPT = (
    "You are watching a live camera feed for a blind user in the middle of "
    "a guided step-by-step task. Their current step is:\n\"{step}\"\n"
    "Look at the photo and decide: does it show they've now done what this "
    "step asks — holding/gathered the items it mentions, in the position "
    "or location it describes, or the action visibly completed? Answer "
    "with exactly one word: 'ready' if the step looks done, or 'waiting' "
    "if not yet. Don't guess generously — only say 'ready' if it's "
    "reasonably clear from the photo."
)


def _check_task_ready_groq(image_b64: str, media_type: str, step_text: str) -> str:
    completion = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        max_tokens=10,
        messages=[
            {"role": "system", "content": TASK_READINESS_PROMPT.format(step=step_text)},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                    },
                ],
            },
        ],
    )
    return (completion.choices[0].message.content or "").strip().lower()


def _check_task_ready_claude(image_b64: str, media_type: str, step_text: str) -> str:
    message = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=10,
        system=TASK_READINESS_PROMPT.format(step=step_text),
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
                ],
            }
        ],
    )
    return next((b.text for b in message.content if b.type == "text"), "").strip().lower()


def check_task_ready(image_b64: str, media_type: str, step_text: str) -> str:
    """Groq first (fast, this runs periodically while a task is active),
    Claude as automatic fallback — same pattern as walking mode."""
    if groq_client is not None:
        try:
            return _check_task_ready_groq(image_b64, media_type, step_text)
        except Exception as e:
            print(f"task tick: Groq failed, falling back to Claude: {e}")
    return _check_task_ready_claude(image_b64, media_type, step_text)


def load_commands() -> list:
    import json
    commands_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "commands.json")
    try:
        with open(commands_path) as f:
            return json.load(f)["commands"]
    except Exception as e:
        print(f"Failed to load commands.json: {e}")
        return []


TYPESENSE_COMMANDS_COLLECTION = "commands"
typesense_ready = False


TYPESENSE_MAX_VECTOR_DISTANCE = 0.8


def sync_typesense_commands():
    """(Re)builds the commands collection from commands.json. Called once at
    startup — cheap and small, so drop + recreate is simpler than diffing.
    Uses Typesense's built-in auto-embedding (semantic vector search)
    instead of plain keyword search — these command descriptions are long
    prose and spoken commands are paraphrased, so literal token overlap
    matches poorly (e.g. "read this to me" sharing no exact words with the
    read_text entry); embeddings match on meaning instead."""
    global typesense_ready
    commands = load_commands()
    schema = {
        "name": TYPESENSE_COMMANDS_COLLECTION,
        "fields": [
            {"name": "cmd_name", "type": "string"},
            {"name": "trigger", "type": "string"},
            {
                "name": "embedding",
                "type": "float[]",
                "embed": {
                    "from": ["trigger"],
                    "model_config": {"model_name": "ts/all-MiniLM-L12-v2"},
                },
            },
        ],
    }
    try:
        try:
            typesense_client.collections[TYPESENSE_COMMANDS_COLLECTION].delete()
        except Exception:
            pass
        typesense_client.collections.create(schema)
        docs = [
            {
                "id": c["name"],
                "cmd_name": c["name"],
                "trigger": c["trigger"],
            }
            for c in commands
        ]
        if docs:
            typesense_client.collections[TYPESENSE_COMMANDS_COLLECTION].documents.import_(
                docs, {"action": "upsert"}
            )
        typesense_ready = True
        print(f"[typesense] indexed {len(docs)} commands")
    except Exception as e:
        typesense_ready = False
        print(f"[typesense] setup failed, command routing will fall back to 'none': {e}")


sync_typesense_commands()


def pick_command_typesense(transcript: str) -> str:
    """Replaces Claude for the command-picking decision: semantic vector
    search (via Typesense's auto-embedding) against the indexed
    commands.json entries, taking the top hit if it's close enough. Falls
    back to 'none' if Typesense isn't reachable, nothing matches, or the
    closest hit is too far to be a real match — the caller treats that the
    same as no command."""
    if not typesense_ready:
        return "none"
    try:
        results = typesense_client.collections[TYPESENSE_COMMANDS_COLLECTION].documents.search({
            "q": transcript,
            "query_by": "embedding",
        })
    except Exception as e:
        print(f"[typesense] search failed: {e}")
        return "none"
    hits = results.get("hits", [])
    if not hits:
        return "none"
    top = hits[0]
    distance = top.get("vector_distance")
    if distance is not None and distance > TYPESENSE_MAX_VECTOR_DISTANCE:
        return "none"
    return top["document"]["cmd_name"]


def interpret_command(transcript: str) -> tuple[str, str]:
    """Typesense decides WHICH command matched (replacing Claude for that
    part); Claude is only used afterward to word the spoken response —
    including writing the actual scene description / read-aloud text
    directly when a photo is attached, same as before."""
    import json

    global last_task_photo

    commands = load_commands()
    if not commands:
        return "none", ""

    valid_names = {c["name"] for c in commands}

    command = pick_command_typesense(transcript)
    if command not in valid_names:
        command = "none"

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
            f"A command-matching system has already determined the matched command "
            f"is: {command!r} (or 'none' if nothing matched).\n\n"
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
            + "Write a short, upbeat spoken confirmation (one sentence, cheerful and "
            "friendly) for the matched command above. If the matched command is "
            "'none', cheerfully say you didn't catch a command. Be creative with your "
            "response, mix it up every time! Don't be repetitive."
        ),
        messages=[{"role": "user", "content": content}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "response": {"type": "string"},
                    },
                    "required": ["response"],
                    "additionalProperties": False,
                },
            }
        },
    )

    text = next((b.text for b in message.content if b.type == "text"), "{}")
    data = json.loads(text)
    response = data.get("response", "")

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

    img = img.rotate(-90, expand=True)

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
            asyncio.create_task(run_in_threadpool(start_live_transcription))
        await run_in_threadpool(feed_live_transcription, chunk)

        return {"status": "buffered", "size": len(audio_buffer)}

    except ClientDisconnect:
        print("transcribe chunk: client disconnected mid-send, dropping this chunk")
        await run_in_threadpool(abandon_live_transcription)
        raise HTTPException(status_code=499, detail={"error": "client disconnected"})
    except Exception as e:
        print(f"Error in /transcribe: {type(e).__name__}: {e}")
        traceback.print_exc()
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

        transcript = await _timed_threadpool("live_transcription_finalize", finish_live_transcription)
        if transcript:
            print(f"Live transcript: {transcript!r}")
        else:
            try:
                deepgram = DeepgramClient(api_key=deepgram_api_key)
                with open(wav_path, "rb") as f:
                    wav_bytes = f.read()
                response = await _timed_threadpool(
                    "batch_transcription",
                    deepgram.listen.v1.media.transcribe_file,
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
        rigged_task_active = (
            rigged_mode == 1 and active_task is not None and active_task.get("rigged")
        )
        if rigged_task_active:
            print(f"Transcript: {transcript!r} -> ignored (rigged mode active)")
        elif transcript and not transcript.startswith("["):
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
                elif settings_command in ("walk_interval_3", "walk_interval_5", "walk_interval_ondemand"):
                    command = settings_command
                    response_text = {
                        "walk_interval_3": "Okay, checking every 3 seconds.",
                        "walk_interval_5": "Okay, checking every 5 seconds.",
                        "walk_interval_ondemand": "Okay, I'll only speak up when something changes.",
                    }[settings_command]
                elif active_task is not None:
                    response_text = await _timed_threadpool("continue_task", continue_task, transcript)
                else:
                    command, response_text = await _timed_threadpool("interpret_command", interpret_command, transcript)
                    if command == "start_task":
                        response_text = await _timed_threadpool("start_task", start_task, transcript)
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

    await run_in_threadpool(abandon_live_transcription)
    with buffer_lock:
        audio_buffer.clear()

    return {"status": "buffer cleared"}


@app.post("/scene/raw")
async def scene_from_glasses(request: Request):
    global pending_response_text, last_debug_image

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_debug_image = image_data

        os.makedirs("./logs/photos", exist_ok=True)
        timestamp = int(time.time() * 1000)
        photo_path = f"./logs/photos/scene_{timestamp}.jpg"
        with open(photo_path, "wb") as f:
            f.write(image_data)

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        message = await _timed_threadpool(
            "scene_raw_vision",
            anthropic_client.messages.create,
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


@app.post("/walk/tick")
async def walk_tick(request: Request):
    """
    One tick of walking-mode narration: raw JPEG in, audio out — directly,
    in this same response, not via the pending_response_text + a follow-up
    GET /response/latest fetch. That two-round-trip pattern is fine for
    regular commands but too slow for a safety-relevant "street ahead"
    alert, so this streams the TTS audio straight back here instead.

    Only a 'clear' result gets suppressed — a real hazard is always spoken,
    every tick, even if it's the same one as last time, since a persistent
    hazard is exactly when repetition is wanted, not when it should go quiet.
    """
    global last_debug_image

    t_start = time.time()

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_debug_image = image_data
        t_received = time.time()
        print(f"walk tick: received {len(image_data)}B image (+{t_received - t_start:.2f}s)")
    
        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")
        t_compressed = time.time()
        print(f"walk tick: compressed to {len(compressed_data)}B (+{t_compressed - t_received:.2f}s)")

        description = await _timed_threadpool("walk_tick_vision", describe_walking_frame, image_b64, media_type)
        t_vision = time.time()
        print(f"walk tick: vision responded (+{t_vision - t_compressed:.2f}s, total {t_vision - t_start:.2f}s)")

        is_clear = description.lower().startswith("clear")

        if is_clear:
            print("walk tick: skipping (clear)")
            return StreamingResponse(iter(()), media_type="application/octet-stream")

        print(f"Walking mode: {description}")

        def generate():
            t_tts_start = time.time()
            audio_stream = elevenlabs_client.text_to_speech.convert(
                text=description,
                voice_id=el_voice_id,
                model_id="eleven_turbo_v2_5",
                output_format="pcm_16000",
                voice_settings={"speed": speech_speed},
            )
            first_chunk = True
            for chunk in audio_stream:
                if first_chunk:
                    print(f"walk tick: first TTS byte (+{time.time() - t_tts_start:.2f}s "
                          f"since TTS call, total {time.time() - t_start:.2f}s)")
                    first_chunk = False
                yield chunk
            print(f"walk tick: TTS stream done (total {time.time() - t_start:.2f}s)")

        return StreamingResponse(generate(), media_type="application/octet-stream")

    except HTTPException:
        raise
    except anthropic.OverloadedError:
        print("walk tick: Claude overloaded, skipping this tick")
        return StreamingResponse(iter(()), media_type="application/octet-stream")
    except Exception as e:
        print(f"Error in /walk/tick: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/task/tick")
async def task_tick(request: Request):
    """
    Periodic visual check during an active multistep task: does the photo
    show the current step is done? If so, this is treated exactly like the
    user saying 'ok' out loud — it calls the same continue_task() used by
    the voice path, so history/step-index/completion detection and the
    actual wording all stay identical whether advancement came from speech
    or from the camera. Streams the transition audio directly back here,
    same one-round-trip pattern as /walk/tick. If the task isn't ready yet
    (or no task is active at all), returns an empty body — nothing to play.
    """
    global last_debug_image, last_task_photo

    if active_task is None:
        return StreamingResponse(iter(()), media_type="application/octet-stream")

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_debug_image = image_data

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        steps = active_task["steps"]
        idx = active_task["step_index"]
        current_step = steps[idx]

        verdict = await _timed_threadpool("task_tick_vision", check_task_ready, image_b64, media_type, current_step)
        print(f"task tick: step {idx + 1}/{len(steps)} verdict={verdict!r}")

        if "ready" not in verdict:
            return StreamingResponse(iter(()), media_type="application/octet-stream")

        print("task tick: visually ready, auto-advancing")
        last_task_photo = image_data
        response_text = await _timed_threadpool("continue_task", continue_task, "okay, I'm ready, done with this step")

        def generate():
            audio_stream = elevenlabs_client.text_to_speech.convert(
                text=response_text,
                voice_id=el_voice_id,
                model_id="eleven_turbo_v2_5",
                output_format="pcm_16000",
                voice_settings={"speed": speech_speed},
            )
            for chunk in audio_stream:
                yield chunk

        return StreamingResponse(generate(), media_type="application/octet-stream")

    except HTTPException:
        raise
    except anthropic.OverloadedError:
        print("task tick: Claude overloaded, skipping this tick")
        return StreamingResponse(iter(()), media_type="application/octet-stream")
    except Exception as e:
        print(f"Error in /task/tick: {e}")
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.get("/rigged/tick")
async def rigged_tick():
    """Always-polled, no-photo, lightweight endpoint — the glasses have no
    other way to learn that an admin started/advanced a rigged task from the
    debug panel, since the server can't push to them. Empty body unless
    there's something to say. Voice/vision play no part in rigged mode; the
    debug panel button (/debug/rigged_button) is the only thing that sets
    rigged_pending_start / rigged_advance_pending."""
    global active_task, rigged_pending_start, rigged_advance_pending

    if rigged_mode == 0:
        return StreamingResponse(iter(()), media_type="application/octet-stream")

    response_text = None

    if rigged_mode == 2:
        if rigged_advance_pending:
            rigged_advance_pending = False
            response_text = RIGGED_LINE_2
            print("[rigged] mode 2: spoke line")
    elif rigged_pending_start:
        rigged_pending_start = False
        active_task = {
            "task_name": RIGGED_TASK_1["task_name"],
            "steps": RIGGED_TASK_1["steps"],
            "step_index": 0,
            "history": [],
            "rigged": True,
        }
        response_text = f"{RIGGED_TASK_1['intro']} {RIGGED_TASK_1['steps'][0]}".strip()
        print("[rigged] task 1 started")

    elif rigged_advance_pending and active_task is not None and active_task.get("rigged"):
        rigged_advance_pending = False
        steps = active_task["steps"]
        idx = active_task["step_index"] + 1
        if idx >= len(steps):
            response_text = RIGGED_TASK_1["sign_off"]
            active_task = None
            print("[rigged] task 1 complete")
        else:
            active_task["step_index"] = idx
            response_text = steps[idx]
            print(f"[rigged] advanced to step {idx + 1}/{len(steps)}")
    else:
        rigged_advance_pending = False

    if not response_text:
        return StreamingResponse(iter(()), media_type="application/octet-stream")

    def generate():
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=response_text,
            voice_id=el_voice_id,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",
            voice_settings={"speed": speech_speed},
        )
        for chunk in audio_stream:
            yield chunk

    return StreamingResponse(generate(), media_type="application/octet-stream")


@app.post("/announce")
async def announce(request: Request):
    """Speak an arbitrary short text on the glasses (used for things like
    the walking-mode on/off confirmation) by reusing the same pending-text
    + streaming /response/latest mechanism as everything else."""
    global pending_response_text
    body = await request.body()
    text = body.decode("utf-8").strip()
    if text:
        pending_response_text = text
    return {"status": "ok"}


@app.post("/ocr/raw")
async def ocr_from_glasses(request: Request):
    global pending_response_text, last_debug_image

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_debug_image = image_data

        os.makedirs("./logs/photos", exist_ok=True)
        timestamp = int(time.time() * 1000)
        photo_path = f"./logs/photos/ocr_{timestamp}.jpg"
        with open(photo_path, "wb") as f:
            f.write(image_data)

        compressed_data, media_type = compress_image(image_data)
        image_b64 = base64.b64encode(compressed_data).decode("utf-8")

        message = await _timed_threadpool(
            "ocr_raw_vision",
            anthropic_client.messages.create,
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
    global last_debug_image
    try:
        photo_data = await request.body()
        if not photo_data:
            raise HTTPException(status_code=400, detail="Empty photo")
        last_debug_image = photo_data

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
    global last_task_photo, last_debug_image

    try:
        image_data = await request.body()
        if not image_data:
            raise HTTPException(status_code=400, detail="Empty image")
        last_task_photo = image_data
        last_debug_image = image_data
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





@app.get("/debug/state")
async def debug_state():
    """Server-side-only state dump for the debug panel. The glasses never
    call this — it exists purely for local inspection."""
    task = None
    if active_task is not None:
        task = {
            "task_name": active_task.get("task_name"),
            "step_index": active_task.get("step_index"),
            "total_steps": len(active_task.get("steps", [])),
            "current_step": (
                active_task["steps"][active_task["step_index"]]
                if active_task.get("steps") and active_task["step_index"] < len(active_task["steps"])
                else None
            ),
            "steps": active_task.get("steps"),
            "history_length": len(active_task.get("history", [])),
            "rigged": bool(active_task.get("rigged")),
        }

    now = time.time()
    glasses_connected = (
        last_glasses_contact is not None
        and (now - last_glasses_contact) < GLASSES_CONTACT_TIMEOUT_S
    )

    route_stats = _stats_summary(_route_stats)
    command_stats = _stats_summary(_command_stats)
    total_requests = sum(s["count"] for s in route_stats.values())
    total_errors = sum(s["errors"] for s in route_stats.values())

    with buffer_lock:
        audio_buffer_bytes = len(audio_buffer)

    return {
        "active_task": task,
        "last_task_photo_pending": last_task_photo is not None,
        "pending_response_text": pending_response_text,
        "speech_speed": speech_speed,
        "speed_range": [SPEED_MIN, SPEED_MAX],
        "groq_configured": groq_client is not None,
        "has_debug_image": last_debug_image is not None,
        "logs": list(_debug_log),

        "uptime_s": round(now - _server_start_time, 1),
        "glasses_connected": glasses_connected,
        "glasses_last_seen_s_ago": (
            round(now - last_glasses_contact, 1) if last_glasses_contact else None
        ),
        "audio_buffer_bytes": audio_buffer_bytes,
        "live_deepgram_active": live_dg_client is not None,
        "last_saved_audio_path": last_saved_audio_path,
        "total_requests": total_requests,
        "total_errors": total_errors,
        "route_stats": route_stats,
        "command_stats": command_stats,
        "rigged_mode": rigged_mode,
    }


@app.get("/debug/image")
async def debug_image():
    """The most recent image received from any endpoint — describe_scene,
    read_text, walking mode, take_photo, or a task photo. Rotated 90deg CCW
    for display, matching the correction applied in compress_image() for the
    camera's physical mounting. Debug-only."""
    if last_debug_image is None:
        raise HTTPException(status_code=404, detail="No image yet")
    try:
        img = Image.open(io.BytesIO(last_debug_image))
        img = img.rotate(-90, expand=True)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=80)
        return Response(content=output.getvalue(), media_type="image/jpeg")
    except Exception:
        return Response(content=last_debug_image, media_type="image/jpeg")


@app.post("/debug/clear_task")
async def debug_clear_task():
    """Reset multistep task state — the thing this panel exists for."""
    global active_task, last_task_photo
    had_task = active_task is not None
    active_task = None
    last_task_photo = None
    print(f"[debug] task context cleared (was {'active' if had_task else 'already empty'})")
    return {"status": "cleared", "had_task": had_task}


@app.post("/debug/rigged")
async def debug_set_rigged(request: Request):
    """Set rigged mode. Body: {"rigged": 0, 1, or 2}."""
    global rigged_mode, rigged_pending_start, rigged_advance_pending
    body = await request.json()
    mode = body.get("rigged")
    rigged_mode = mode if mode in (0, 1, 2) else 0
    rigged_pending_start = False
    rigged_advance_pending = False
    print(f"[rigged] mode set to {rigged_mode}")
    return {"rigged": rigged_mode}


@app.post("/debug/rigged_button")
async def debug_rigged_button():
    """The single admin control for rigged mode:
    - mode 1: starts task 1 if nothing's active yet, otherwise advances the
      current step.
    - mode 2: speaks RIGGED_LINE_2 every press, no state.
    Picked up by the glasses' always-on /rigged/tick poll, not delivered
    directly."""
    global rigged_pending_start, rigged_advance_pending
    if rigged_mode == 2:
        rigged_advance_pending = True
        print("[rigged] mode 2 line requested")
        return {"status": "line_requested"}
    if rigged_mode != 1:
        return {"status": "ignored", "reason": "rigged mode is off"}
    if active_task is None:
        rigged_pending_start = True
        print("[rigged] start requested")
        return {"status": "start_requested"}
    rigged_advance_pending = True
    print("[rigged] advance requested")
    return {"status": "advance_requested"}


@app.get("/debug", response_class=HTMLResponse)
async def debug_panel():
    return """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Clarity Debug Panel</title>
<style>
  body { background:#0e0e12; color:#e6e6e6; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin:0; padding:24px; }
  h1 { font-size:18px; margin:0 0 20px; color:#9d8cff; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }
  .card { background:#1a1a22; border:1px solid #2a2a35; border-radius:10px; padding:16px; }
  .card h2 { font-size:13px; text-transform:uppercase; letter-spacing:0.05em; color:#8a8a9a; margin:0 0 12px; }
  .row { display:flex; justify-content:space-between; padding:4px 0; font-size:14px; border-bottom:1px solid #22222c; }
  .row:last-child { border-bottom:none; }
  .label { color:#8a8a9a; }
  .val { color:#e6e6e6; font-weight:500; text-align:right; max-width:60%; }
  .ok { color:#5fd97a; }
  .off { color:#5a5a68; }
  button { background:#5a3fd6; color:white; border:none; border-radius:8px; padding:10px 16px; font-size:14px; cursor:pointer; }
  button:hover { background:#6d50ea; }
  button:disabled { background:#3a3a45; cursor:default; }
  .steps { margin-top:8px; }
  .step { padding:6px 10px; margin:4px 0; border-radius:6px; background:#22222c; font-size:13px; }
  .step.current { background:#332a5c; border-left:3px solid #9d8cff; }
  #log { background:#0a0a0d; border:1px solid #2a2a35; border-radius:10px; padding:12px; height:320px; overflow-y:auto; font-family:ui-monospace,Menlo,monospace; font-size:12px; line-height:1.5; white-space:pre-wrap; }
  .empty { color:#5a5a68; font-style:italic; }
  #image-wrap { display:flex; align-items:center; justify-content:center; background:#0a0a0d; border-radius:8px; min-height:180px; }
  #image-wrap img { max-width:100%; max-height:360px; border-radius:8px; display:block; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th, td { text-align:right; padding:5px 8px; border-bottom:1px solid #22222c; }
  th:first-child, td:first-child { text-align:left; }
  th { color:#8a8a9a; font-weight:500; text-transform:uppercase; font-size:10.5px; letter-spacing:.04em; }
  td.errcount { color:#ff6b6b; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
  .dot.ok { background:#5fd97a; }
  .dot.off { background:#5a5a68; }
</style>
</head>
<body>
  <h1>Clarity — Debug Panel</h1>
  <div class="grid">
    <div class="card">
      <h2>Multistep Task</h2>
      <div id="task-body"><div class="empty">loading...</div></div>
      <div style="margin-top:12px;">
        <button id="clear-btn" onclick="clearTask()">Clear task context</button>
      </div>
    </div>
    <div class="card">
      <h2>Server State</h2>
      <div id="state-body"><div class="empty">loading...</div></div>
    </div>
  </div>
  <div class="card" style="margin-bottom:16px;">
    <h2>Rigged Mode (scripted demo)</h2>
    <div id="rigged-body"><div class="empty">loading...</div></div>
    <div style="margin-top:12px; display:flex; gap:8px;">
      <button id="rigged-toggle-btn" onclick="toggleRigged()">Rigged mode</button>
      <button id="rigged-advance-btn" onclick="riggedButton()">Start / Continue</button>
    </div>
  </div>
  <div class="card" style="margin-bottom:16px;">
    <h2>Latest Image</h2>
    <div id="image-wrap"><div class="empty">no image yet</div></div>
  </div>
  <div class="grid">
    <div class="card">
      <h2>Route Timings (ms)</h2>
      <div id="route-stats"><div class="empty">loading...</div></div>
    </div>
    <div class="card">
      <h2>Command / AI Call Timings (ms)</h2>
      <div id="command-stats"><div class="empty">loading...</div></div>
    </div>
  </div>
  <div class="card">
    <h2>Server Log</h2>
    <div id="log"></div>
  </div>

<script>
async function clearTask() {
  const btn = document.getElementById('clear-btn');
  btn.disabled = true;
  await fetch('/debug/clear_task', {method: 'POST'});
  btn.disabled = false;
  refresh();
}

async function toggleRigged() {
  const btn = document.getElementById('rigged-toggle-btn');
  btn.disabled = true;
  const current = parseInt(btn.dataset.rigged || '0', 10);
  const next = (current + 1) % 3;
  await fetch('/debug/rigged', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rigged: next}),
  });
  btn.disabled = false;
  refresh();
}

async function riggedButton() {
  const btn = document.getElementById('rigged-advance-btn');
  btn.disabled = true;
  await fetch('/debug/rigged_button', {method: 'POST'});
  btn.disabled = false;
  refresh();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function refresh() {
  let data;
  try {
    data = await (await fetch('/debug/state')).json();
  } catch (e) {
    return;
  }

  const taskBody = document.getElementById('task-body');
  if (data.active_task) {
    const t = data.active_task;
    let stepsHtml = '<div class="steps">';
    (t.steps || []).forEach((s, i) => {
      stepsHtml += `<div class="step ${i === t.step_index ? 'current' : ''}">${i + 1}. ${esc(s)}</div>`;
    });
    stepsHtml += '</div>';
    taskBody.innerHTML = `
      <div class="row"><span class="label">Task</span><span class="val">${esc(t.task_name || '(unnamed)')}</span></div>
      <div class="row"><span class="label">Step</span><span class="val">${t.step_index + 1} / ${t.total_steps}</span></div>
      <div class="row"><span class="label">History length</span><span class="val">${t.history_length}</span></div>
      ${stepsHtml}
    `;
  } else {
    taskBody.innerHTML = '<div class="empty">No active task</div>';
  }

  const riggedBody = document.getElementById('rigged-body');
  const toggleBtn = document.getElementById('rigged-toggle-btn');
  const advanceBtn = document.getElementById('rigged-advance-btn');
  const modeLabels = {0: '0 — normal', 1: '1 — scripted task', 2: '2 — single line'};
  toggleBtn.dataset.rigged = data.rigged_mode;
  toggleBtn.textContent = `Rigged mode: ${modeLabels[data.rigged_mode]} (click to cycle)`;
  advanceBtn.textContent = data.rigged_mode === 2 ? 'Speak line' : 'Start / Continue (rigged)';
  riggedBody.innerHTML = `
    <div class="row"><span class="label">Mode</span><span class="val"><span class="dot ${data.rigged_mode ? 'ok' : 'off'}"></span>${modeLabels[data.rigged_mode]}</span></div>
    <div class="row"><span class="label">Rigged task running</span><span class="val">${data.active_task && data.active_task.rigged ? 'yes' : 'no'}</span></div>
  `;

  function fmtUptime(s) {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
    return h > 0 ? `${h}h ${m}m ${sec}s` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  }

  const stateBody = document.getElementById('state-body');
  stateBody.innerHTML = `
    <div class="row"><span class="label">Glasses connectivity</span><span class="val"><span class="dot ${data.glasses_connected ? 'ok' : 'off'}"></span>${data.glasses_connected ? 'online' : 'offline'}${data.glasses_last_seen_s_ago != null ? ` (last seen ${data.glasses_last_seen_s_ago.toFixed(0)}s ago)` : ' (never seen)'}</span></div>
    <div class="row"><span class="label">Server uptime</span><span class="val">${fmtUptime(data.uptime_s)}</span></div>
    <div class="row"><span class="label">Total requests / errors</span><span class="val">${data.total_requests} / <span class="${data.total_errors ? 'errcount' : ''}">${data.total_errors}</span></span></div>
    <div class="row"><span class="label">Speech speed</span><span class="val">${data.speech_speed} (range ${data.speed_range[0]}–${data.speed_range[1]})</span></div>
    <div class="row"><span class="label">Groq configured</span><span class="val ${data.groq_configured ? 'ok' : 'off'}">${data.groq_configured ? 'yes' : 'no (Claude fallback only)'}</span></div>
    <div class="row"><span class="label">Live Deepgram connection</span><span class="val ${data.live_deepgram_active ? 'ok' : 'off'}">${data.live_deepgram_active ? 'open' : 'idle'}</span></div>
    <div class="row"><span class="label">Audio buffer</span><span class="val">${data.audio_buffer_bytes} bytes</span></div>
    <div class="row"><span class="label">Task photo pending</span><span class="val">${data.last_task_photo_pending ? 'yes' : 'no'}</span></div>
    <div class="row"><span class="label">Last recording saved</span><span class="val">${data.last_saved_audio_path ? esc(data.last_saved_audio_path) : '(none)'}</span></div>
    <div class="row"><span class="label">Pending response text</span><span class="val">${data.pending_response_text ? esc(data.pending_response_text) : '(none)'}</span></div>
  `;

  function renderStatsTable(stats) {
    const keys = Object.keys(stats);
    if (!keys.length) return '<div class="empty">no calls yet</div>';
    let rows = '';
    keys.forEach(k => {
      const s = stats[k];
      const agoS = s.last_at ? (Date.now() / 1000 - s.last_at) : null;
      rows += `<tr>
        <td>${esc(k)}</td>
        <td>${s.count}</td>
        <td class="${s.errors ? 'errcount' : ''}">${s.errors}</td>
        <td>${s.avg_ms}</td>
        <td>${s.min_ms ?? '—'}</td>
        <td>${s.max_ms ?? '—'}</td>
        <td>${agoS != null ? agoS.toFixed(0) + 's ago' : '—'}</td>
      </tr>`;
    });
    return `<table><thead><tr><th>Key</th><th>Count</th><th>Err</th><th>Avg</th><th>Min</th><th>Max</th><th>Last</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  document.getElementById('route-stats').innerHTML = renderStatsTable(data.route_stats || {});
  document.getElementById('command-stats').innerHTML = renderStatsTable(data.command_stats || {});

  const imageWrap = document.getElementById('image-wrap');
  if (data.has_debug_image) {
    imageWrap.innerHTML = `<img src="/debug/image?t=${Date.now()}" alt="latest capture">`;
  } else {
    imageWrap.innerHTML = '<div class="empty">no image yet</div>';
  }

  const log = document.getElementById('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = (data.logs || []).join('\\n') || '(no logs yet)';
  if (atBottom) log.scrollTop = log.scrollHeight;
}

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    if mode == "wifi":
     uvicorn.run(app, host="192.168.1.2", port=8000)
    elif mode == "hotspot":
        uvicorn.run(app, host="172.20.10.4", port=8000)
