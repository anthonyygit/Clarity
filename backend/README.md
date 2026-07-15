# AI Glasses Backend

FastAPI backend for an AI glasses project designed for blind/visually impaired users. Processes images from a Raspberry Pi Pico 2W, generates descriptions via Claude, and returns audio files for playback.

## Features

- **Scene Description**: Send an image, get a conversational description converted to speech (via ElevenLabs)
- **OCR Text Reader**: Extract text from images and convert to speech (via ElevenLabs)
- **Health Check**: Verify server availability
- **Optimized Audio**: WAV files formatted for I2S audio (16kHz mono 16-bit PCM)

## Prerequisites

- Python 3.9+
- `ffmpeg` (required by pydub)
- Tesseract OCR (required by pytesseract)
- Anthropic API key
- ElevenLabs API key

### Install ffmpeg and Tesseract

**macOS:**
```bash
brew install ffmpeg tesseract
```

**Ubuntu/Debian:**
```bash
sudo apt-get install ffmpeg tesseract-ocr
```

**Windows:**
- Download ffmpeg: https://ffmpeg.org/download.html
- Download Tesseract: https://github.com/UB-Mannheim/tesseract/wiki

## Setup

1. **Clone and navigate:**
   ```bash
   cd backend
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env and add your Anthropic and ElevenLabs API keys
   nano .env
   ```
   Add both:
   ```
   ANTHROPIC_API_KEY=your_anthropic_key
   ELEVENLABS_API_KEY=your_elevenlabs_key
   ```

5. **Run the server:**
   ```bash
   python main.py
   ```

The server will start on `http://0.0.0.0:8000` and be accessible on your local network.

## API Endpoints

### 1. Health Check
**GET /health**

Check if the server is running.

**Response:**
```json
{"status": "ok"}
```

**Example:**
```bash
curl http://localhost:8000/health
```

---

### 2. Scene Description
**POST /scene**

Send an image and receive a conversational description as a WAV audio file.

**Request:**
- Multipart form data with `image` field (JPEG or PNG)

**Response:**
- WAV audio file (16kHz mono 16-bit PCM)

**Example:**
```bash
curl -X POST http://localhost:8000/scene \
  -F "image=@/path/to/image.jpg" \
  --output scene.wav
```

**How it works:**
1. Encodes the image as base64
2. Sends to Claude with a system prompt for blind-user-friendly descriptions
3. Claude returns 1-3 clear sentences describing the scene
4. Converts the text to speech using ElevenLabs (natural-sounding audio)
5. Returns as a WAV file optimized for the Pico 2W's MAX98357 I2S DAC

---

### 3. OCR Text Reader
**POST /ocr**

Send an image with text and receive the extracted text read aloud as a WAV audio file.

**Request:**
- Multipart form data with `image` field (JPEG or PNG)

**Response:**
- WAV audio file (16kHz mono 16-bit PCM)

**Example:**
```bash
curl -X POST http://localhost:8000/ocr \
  -F "image=@/path/to/document.jpg" \
  --output ocr.wav
```

**How it works:**
1. Receives the image
2. Runs pytesseract OCR to extract text
3. Cleans the extracted text (removes extra whitespace/newlines)
4. Prepends "The text reads: " for context
5. Converts to speech using ElevenLabs (natural-sounding audio)
6. Returns as a WAV file optimized for the Pico 2W

---

## Audio Format Specifications

All WAV files returned are formatted specifically for the Raspberry Pi Pico 2W with a MAX98357 I2S DAC amplifier:

- **Sample Rate:** 16,000 Hz
- **Channels:** 1 (Mono)
- **Bit Depth:** 16-bit
- **Codec:** PCM (Signed 16-bit Little Endian)
- **No metadata:** Raw WAV suitable for I2S playback

---

## Error Handling

- **400 Bad Request:** Missing image file
- **500 Internal Server Error:** Processing failure (check logs for details)

All errors return JSON with an error message:
```json
{"detail": {"error": "error description"}}
```

---

## Environment Variables

**`ANTHROPIC_API_KEY`**: Your Anthropic API key (required)
- Get it from: https://console.anthropic.com

**`ELEVENLABS_API_KEY`**: Your ElevenLabs API key (required for text-to-speech)
- Get it from: https://elevenlabs.io
- Free tier includes 10,000 characters/month
- Create an account, go to API Keys, and copy your key

---

## Testing with Python

```python
import requests

# Test health check
response = requests.get("http://localhost:8000/health")
print(response.json())

# Test scene description
with open("test_image.jpg", "rb") as f:
    response = requests.post(
        "http://localhost:8000/scene",
        files={"image": f}
    )
    with open("output_scene.wav", "wb") as out:
        out.write(response.content)

# Test OCR
with open("test_document.jpg", "rb") as f:
    response = requests.post(
        "http://localhost:8000/ocr",
        files={"image": f}
    )
    with open("output_ocr.wav", "wb") as out:
        out.write(response.content)
```

---

## CORS

The server allows requests from all origins (`*`) to support the Raspberry Pi Pico 2W connecting from any IP address. Modify the CORS settings in `main.py` if needed for production.

---

## Troubleshooting

**Import errors for pytesseract/pydub:**
- Ensure ffmpeg and tesseract are installed and in your PATH

**Slow responses:**
- API calls to Claude typically take 1-2 seconds
- gTTS conversion adds another 1-2 seconds
- Total latency ~3-5 seconds per request is normal

**API key not found:**
- Verify `.env` file exists and `ANTHROPIC_API_KEY` is set
- Run `python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(os.getenv('ANTHROPIC_API_KEY'))"`

**WAV playback issues on Pico:**
- Verify the server is returning audio/wav content-type
- Check that sample rate is 16000 Hz and channels is 1
- Test with: `ffprobe output.wav` (if ffmpeg is installed)
