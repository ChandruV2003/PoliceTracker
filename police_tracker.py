#!/usr/bin/env python3
"""
PoliceTracker - Automatic police radio listener and alert system
"""
import os
import sys
import logging
import signal
import subprocess
import threading
import time
import uuid
import wave
import shutil
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import yaml
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv
from difflib import SequenceMatcher

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG_FILE = Path("config.yaml")
CONFIG_LOCAL_FILE = Path("config.local.yaml")
AUDIO_CACHE_DIR = Path("audio_cache")
AUDIO_CACHE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
active_streams: Dict[str, subprocess.Popen] = {}
stream_status: Dict[str, Dict] = {}
web_api_url: Optional[str] = None
webhook_url: Optional[str] = None
api_token: Optional[str] = None
processing_config: Dict = {}
transcription_backend: Optional[str] = None
whisper_model = None
whisper_device: str = "cpu"
transcription_lock = threading.Lock()


def init_transcription_backend(model_name: str) -> Tuple[Optional[str], Optional[object]]:
    """Initialize transcription backend if available."""
    try:
        import whisper  # type: ignore
        import torch  # type: ignore

        device = "cpu"
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"

        model = whisper.load_model(model_name)
        # Whisper ships a sparse alignment_heads buffer which breaks .to("mps") on some torch builds.
        try:
            if hasattr(model, "alignment_heads") and isinstance(model.alignment_heads, torch.Tensor) and model.alignment_heads.is_sparse:
                model.alignment_heads = model.alignment_heads.to_dense()
        except Exception as e:
            logger.warning(f"Failed to densify alignment_heads buffer: {e}")
        try:
            model = model.to(device)
        except Exception as e:
            logger.warning(f"Failed to move whisper model to {device}: {e}. Falling back to CPU.")
            device = "cpu"
            model = model.to(device)

        global whisper_device
        whisper_device = device
        logger.info(f"Using openai-whisper backend on {device} for transcription (model={model_name})")
        return "openai-whisper", model
    except Exception as e:
        logger.warning(f"Transcription backend not available: {e}")
        return None, None


def send_event_to_api(
    channel: str,
    keywords: List[str],
    transcript: str = "",
    priority: int = 5,
    location: str = "",
    api_url: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    geo_source: str = "channel",
    geo_confidence: float = 0.25,
    geo_query: str = "",
):
    """Send detected event to web API"""
    api_endpoint = api_url or web_api_url
    
    if not api_endpoint:
        logger.debug("No web API URL configured, skipping event send")
        return False
    
    event_data = {
        "channel": channel,
        "timestamp": time.time(),
        "keywords": keywords,
        "transcript": transcript,
        "priority": priority,
        "location": location
    }

    if lat is not None and lon is not None:
        event_data["lat"] = float(lat)
        event_data["lon"] = float(lon)
        event_data["geo_source"] = geo_source
        event_data["geo_confidence"] = float(geo_confidence)
        event_data["geo_query"] = geo_query or location
    
    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    try:
        response = requests.post(api_endpoint, json=event_data, headers=headers, timeout=5)
        if response.status_code == 201:
            logger.info(f"Event sent to web API: {channel} - {keywords}")
            return True
        else:
            logger.warning(f"Failed to send event: {response.status_code} - {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending event to web API: {e}")
        return False


def send_event_to_webhook(
    channel: str,
    keywords: List[str],
    transcript: str = "",
    priority: int = 5,
    location: str = "",
    webhook: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    geo_source: str = "channel",
    geo_confidence: float = 0.25,
    geo_query: str = "",
):
    """Send detected event to webhook"""
    endpoint = webhook or webhook_url
    if not endpoint:
        return False

    payload = {
        "channel": channel,
        "timestamp": time.time(),
        "keywords": keywords,
        "transcript": transcript,
        "priority": priority,
        "location": location
    }
    if lat is not None and lon is not None:
        payload["lat"] = float(lat)
        payload["lon"] = float(lon)
        payload["geo_source"] = geo_source
        payload["geo_confidence"] = float(geo_confidence)
        payload["geo_query"] = geo_query or location
    try:
        response = requests.post(endpoint, json=payload, timeout=5)
        if 200 <= response.status_code < 300:
            logger.info(f"Event sent to webhook: {channel} - {keywords}")
            return True
        logger.warning(f"Failed to send webhook: {response.status_code} - {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending webhook: {e}")
        return False


def detect_keywords(transcript: str, keywords: List[str], threshold: float) -> List[str]:
    """Detect keywords in transcript using exact and fuzzy matching."""
    if not transcript:
        return []

    transcript_lc = transcript.lower()
    tokens = transcript_lc.split()
    detected: List[str] = []

    for keyword in keywords:
        keyword_lc = keyword.lower().strip()
        if not keyword_lc:
            continue

        if keyword_lc in transcript_lc:
            detected.append(keyword)
            continue

        kw_tokens = keyword_lc.split()
        if len(kw_tokens) == 1:
            for token in tokens:
                similarity = SequenceMatcher(None, keyword_lc, token).ratio()
                if similarity >= threshold:
                    detected.append(keyword)
                    break
        else:
            window_size = len(kw_tokens)
            for i in range(max(0, len(tokens) - window_size + 1)):
                window = " ".join(tokens[i:i + window_size])
                similarity = SequenceMatcher(None, keyword_lc, window).ratio()
                if similarity >= threshold:
                    detected.append(keyword)
                    break

    return detected


def looks_like_ad(transcript_lc: str) -> bool:
    """Best-effort filter for Broadcastify-style ad inserts.

    We keep this intentionally conservative: it should only suppress very obvious ad reads.
    """
    if not transcript_lc:
        return False

    strong_phrases = (
        "promo code",
        "terms and conditions",
        "sponsored by",
        "download the app",
        "download the",
        "use code",
        "kalshi",
        "sportsbook",
    )
    if any(p in transcript_lc for p in strong_phrases):
        # Require a second hint for generic phrases like "use code" or "download the"
        if "promo code" in transcript_lc or "terms and conditions" in transcript_lc or "sponsored by" in transcript_lc:
            return True
        if "download" in transcript_lc and ("promo" in transcript_lc or "code" in transcript_lc):
            return True
        if "kalshi" in transcript_lc:
            return True

    return False


def has_dispatch_context(transcript_lc: str) -> bool:
    """Heuristic: does this sound like an actual dispatch call vs generic chatter/ads?"""
    if not transcript_lc:
        return False

    dispatch_markers = (
        "dispatch",
        "respond",
        "responding",
        "en route",
        "caller",
        "reports",
        "reported",
        "be advised",
        "units",
        "unit",
        "copy",
        "received",
        "staging",
        "on scene",
    )
    if any(m in transcript_lc for m in dispatch_markers):
        return True

    # Location-ish phrases + a number (e.g., "route 22", "exit 9", "123 main st")
    location_markers = (
        "route",
        "rt ",
        "highway",
        "turnpike",
        "parkway",
        "exit",
        "mile",
        "intersection",
        "street",
        " st",
        "road",
        " rd",
        "avenue",
        " ave",
        "boulevard",
        " blvd",
        "township",
        "county",
    )
    if any(m in transcript_lc for m in location_markers):
        if re.search(r"\\b\\d{1,5}\\b", transcript_lc):
            return True

    return False


def apply_detection_filters(transcript: str, detected_keywords: List[str], cfg: Dict) -> List[str]:
    """Filter/clean detected keywords to reduce obvious false positives."""
    if not detected_keywords:
        return []

    transcript = (transcript or "").strip()
    if not transcript:
        return []

    min_len = int(cfg.get("min_transcript_length", 18))
    if len(transcript) < min_len:
        return []

    transcript_lc = transcript.lower()

    if bool(cfg.get("ad_block_enabled", True)) and looks_like_ad(transcript_lc):
        return []

    # Only gate a small set of ambiguous single-word keywords to avoid missing real incidents.
    ambiguous_single_words = set(
        (cfg.get("ambiguous_single_word_keywords") or ["shooting", "robbery", "armed", "pursuit"])
    )
    gate_ambiguous = bool(cfg.get("gate_ambiguous_single_words", True))
    if not gate_ambiguous:
        return detected_keywords

    if not has_dispatch_context(transcript_lc):
        filtered = []
        for kw in detected_keywords:
            kw_lc = kw.lower().strip()
            if " " in kw_lc:
                filtered.append(kw)  # phrases are usually safer
                continue
            if kw_lc in ambiguous_single_words:
                continue
            filtered.append(kw)
        return filtered

    return detected_keywords


def approximate_channel_center(channel_name: str, location: str) -> Optional[Tuple[float, float]]:
    """Return an approximate (lat, lon) for a channel to power a coarse map view (v1).

    This is intentionally rough. It prefers explicit city/county names and falls back to broad NJ regions.
    """
    channel_lc = (channel_name or "").lower()
    loc_lc = (location or "").lower()

    # A small NJ-focused lookup. Add more as needed.
    # Sources: public city/county centroids (approx) and common regional anchors.
    lookup = {
        # Cities
        "newark": (40.7357, -74.1724),
        "jersey city": (40.7178, -74.0431),
        "trenton": (40.2204, -74.7643),
        # Counties (approx centroids)
        "essex county": (40.7876, -74.2452),
        "somerset county": (40.5606, -74.6400),
        "ocean county": (39.9153, -74.2846),
        "camden county": (39.8023, -74.9517),
        "burlington county": (39.8395, -74.7173),
        "gloucester county": (39.7176, -75.1410),
        "atlantic county": (39.4778, -74.6338),
        "mercer county": (40.2829, -74.7027),
        # Regions
        "north nj": (40.8850, -74.2700),
        "central nj": (40.3573, -74.5082),
        "south nj": (39.8200, -74.9900),
        "statewide nj": (40.0583, -74.4057),
    }

    # Prefer explicit city hints in the channel name first
    for key in ("newark", "jersey city", "trenton"):
        if key in channel_lc:
            return lookup[key]

    # Then look at the location string
    for key, coords in lookup.items():
        if key in loc_lc:
            return coords

    # Finally, look for county names in the channel name if location is generic
    for key, coords in lookup.items():
        if "county" in key and key.split(" county", 1)[0] in channel_lc:
            return coords

    if "troop a" in channel_lc:
        return lookup["north nj"]
    if "troop b" in channel_lc:
        return lookup["central nj"]
    if "troop c" in channel_lc:
        return lookup["south nj"]

    # Default: center of NJ
    if "nj" in channel_lc or "new jersey" in channel_lc or "nj" in loc_lc:
        return lookup["statewide nj"]

    return None


def transcribe_audio(audio_path: Path, model_name: str) -> str:
    """Transcribe audio using configured backend."""
    global transcription_backend, whisper_model

    if not transcription_backend:
        # Avoid racing multiple model loads on startup when several channels begin at once.
        with transcription_lock:
            if not transcription_backend:
                transcription_backend, whisper_model = init_transcription_backend(model_name)

    if transcription_backend == "openai-whisper" and whisper_model is not None:
        try:
            # Prefer deterministic, fast-ish defaults.
            # fp16 must be disabled on CPU. On MPS, fp16 can silently produce empty transcripts on
            # some Torch/Whisper builds (no exception), so we keep it off there too.
            fp16 = whisper_device == "cuda"
            with transcription_lock:
                try:
                    result = whisper_model.transcribe(
                        str(audio_path),
                        language="en",
                        task="transcribe",
                        temperature=0.0,
                        fp16=fp16,
                        condition_on_previous_text=False,
                        # whisper's Python API shows tqdm progress bars when verbose is False; use None to silence.
                        verbose=None,
                    )
                except Exception as e:
                    raise
            return (result.get("text") or "").strip()
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return ""

    logger.warning("Transcription requested but no backend is available")
    return ""


def write_wav(path: Path, pcm_data: bytes, sample_rate: int, channels: int, sample_width: int) -> None:
    """Write raw PCM (s16le) bytes to a WAV file."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(int(channels))
        wf.setsampwidth(int(sample_width))
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm_data)


def safe_basename(name: str) -> str:
    """Make a name safe for use as a filename."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    safe = re.sub(r"_+", "_", safe).strip("_.-")
    return safe or "channel"


class ChannelMonitor:
    """Monitors a single police radio channel"""
    
    def __init__(self, channel_config: Dict):
        self.name = channel_config.get("name", "Unknown")
        self.url = channel_config.get("url", "")
        self.keywords = channel_config.get("keywords", [])
        self.enabled = channel_config.get("enabled", True)
        self.location = channel_config.get("location", "")
        self.priority = channel_config.get("priority", 5)
        self.last_error: Optional[str] = None
        self.last_audio_time: Optional[float] = None
        self.last_event_time: Optional[float] = None
        self.restart_count: int = 0
        self.last_restart_time: Optional[float] = None
        self.last_exit_code: Optional[int] = None
        self.last_keyword_event_times: Dict[str, float] = {}
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self._stop_event = threading.Event()
        
    def trigger_event(self, detected_keywords: List[str], transcript: str = "", priority: int = 5):
        """Trigger an event when keywords are detected"""
        logger.info(f"Event detected on {self.name}: {detected_keywords}")
        center = approximate_channel_center(self.name, self.location)
        lat = center[0] if center else None
        lon = center[1] if center else None
        send_event_to_api(
            channel=self.name,
            keywords=detected_keywords,
            transcript=transcript,
            priority=priority,
            location=self.location,
            lat=lat,
            lon=lon,
            geo_source="channel",
            geo_confidence=0.25,
            geo_query=self.location,
        )
        send_event_to_webhook(
            channel=self.name,
            keywords=detected_keywords,
            transcript=transcript,
            priority=priority,
            location=self.location,
            lat=lat,
            lon=lon,
            geo_source="channel",
            geo_confidence=0.25,
            geo_query=self.location,
        )
        self.last_event_time = time.time()
        for kw in detected_keywords:
            self.last_keyword_event_times[str(kw).lower().strip()] = self.last_event_time
        
    def start(self):
        """Start monitoring this channel"""
        if not self.enabled or not self.url:
            logger.warning(f"Channel {self.name} is disabled or has no URL")
            return False

        if shutil.which("ffmpeg") is None:
            self.last_error = "ffmpeg not found in PATH"
            logger.error(f"Cannot start {self.name}: {self.last_error}")
            return False
        
        logger.info(f"Starting monitor for channel: {self.name}")
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._run, name=f"Monitor-{self.name}", daemon=True)
        self.thread.start()
        self.running = True
        return True

    def _run(self):
        """Main monitoring loop for channel."""
        sample_rate = processing_config.get("sample_rate", 16000)
        channels = 1
        sample_width = 2
        chunk_duration = float(processing_config.get("audio_chunk_duration", 10))
        threshold = float(processing_config.get("keyword_match_threshold", 0.8))
        keyword_cooldown_seconds = float(processing_config.get("keyword_cooldown_seconds", 180))
        use_transcription = bool(processing_config.get("use_transcription", False))
        model_name = processing_config.get("transcription_model", "base")
        bytes_per_second = sample_rate * channels * sample_width
        chunk_size = int(bytes_per_second * chunk_duration)
        warned_no_transcription = False

        backoff_seconds = 1

        while not self._stop_event.is_set():
            try:
                # ffmpeg options:
                # -reconnect*: keep streamed HTTP audio resilient
                # -rw_timeout: fail fast if the socket stalls
                command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-nostdin",
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "2",
                    "-rw_timeout", "15000000",
                    "-i", self.url,
                    "-f", "s16le",
                    "-ac", str(channels),
                    "-ar", str(sample_rate),
                    "-acodec", "pcm_s16le",
                    "-"
                ]
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                active_streams[self.name] = self.process
                self.last_error = None
                self.last_exit_code = None

                while not self._stop_event.is_set():
                    if not self.process or not self.process.stdout:
                        break
                    data = self.process.stdout.read(chunk_size)
                    if not data:
                        break
                    # Guard against short/partial reads which can confuse transcription.
                    if len(data) < int(bytes_per_second * 2):
                        continue

                    self.last_audio_time = time.time()
                    if not use_transcription:
                        if not warned_no_transcription:
                            logger.warning(f"Transcription disabled for {self.name}; keyword detection will be inactive")
                            warned_no_transcription = True
                        continue

                    chunk_id = uuid.uuid4().hex
                    audio_path = AUDIO_CACHE_DIR / f"{safe_basename(self.name)}_{chunk_id}.wav"
                    write_wav(audio_path, data, sample_rate, channels, sample_width)

                    transcript = transcribe_audio(audio_path, model_name)
                    detected_keywords = detect_keywords(transcript, self.keywords, threshold)
                    detected_keywords = apply_detection_filters(transcript, detected_keywords, processing_config)
                    if detected_keywords:
                        now = time.time()
                        should_emit = False
                        for kw in detected_keywords:
                            key = str(kw).lower().strip()
                            last = self.last_keyword_event_times.get(key, 0.0)
                            if now - last >= keyword_cooldown_seconds:
                                should_emit = True
                                break

                        if should_emit:
                            self.trigger_event(detected_keywords, transcript, priority=self.priority)
                        else:
                            logger.info(
                                f"Suppressed duplicate keywords on {self.name} within cooldown ({int(keyword_cooldown_seconds)}s): {detected_keywords}"
                            )

                    try:
                        audio_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                # If we ended unexpectedly, capture a small error summary from stderr (if any).
                if self.process and not self._stop_event.is_set():
                    try:
                        if self.process.poll() is None:
                            self.process.terminate()
                            self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            self.process.kill()
                            self.process.wait(timeout=5)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    try:
                        self.last_exit_code = self.process.poll()
                    except Exception:
                        self.last_exit_code = None

                    try:
                        if self.process.stderr:
                            err = self.process.stderr.read() or b""
                            tail = err[-2000:].decode("utf-8", errors="replace").strip()
                            if tail:
                                last_line = tail.splitlines()[-1].strip()
                                if self.last_exit_code is not None and self.last_exit_code != 0:
                                    self.last_error = f"ffmpeg exit {self.last_exit_code}: {last_line}"[:300]
                                else:
                                    self.last_error = last_line[:300]
                    except Exception:
                        pass
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Error in channel {self.name}: {e}")
            finally:
                active_streams.pop(self.name, None)
                if self.process:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                    self.process = None

            if not self._stop_event.is_set():
                self.restart_count += 1
                self.last_restart_time = time.time()
                reason = f" (last_error={self.last_error})" if self.last_error else ""
                logger.warning(f"Restarting channel {self.name} in {backoff_seconds}s{reason}")
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)
    
    def stop(self):
        """Stop monitoring this channel"""
        logger.info(f"Stopping monitor for channel: {self.name}")
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
        self.running = False
    
    def get_status(self) -> Dict:
        """Get current status of this channel"""
        now = time.time()
        audio_age_s = None
        if self.last_audio_time is not None:
            try:
                audio_age_s = now - float(self.last_audio_time)
            except Exception:
                audio_age_s = None
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self.running,
            "url": self.url,
            "keywords": self.keywords,
            "location": self.location,
            "priority": self.priority,
            "last_audio_time": self.last_audio_time,
            "audio_age_s": audio_age_s,
            "last_event_time": self.last_event_time,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "last_restart_time": self.last_restart_time,
            "last_exit_code": self.last_exit_code,
        }


def load_config() -> Dict:
    """Load configuration from YAML file"""
    def deep_merge(base: object, override: object) -> object:
        if isinstance(base, dict) and isinstance(override, dict):
            merged = dict(base)
            for key, value in override.items():
                merged[key] = deep_merge(base.get(key), value)
            return merged
        # Lists and scalars: override wins
        return override

    base_config: Dict = {}
    local_config: Dict = {}

    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            base_config = yaml.safe_load(f) or {}

    if CONFIG_LOCAL_FILE.exists():
        with open(CONFIG_LOCAL_FILE, "r") as f:
            local_config = yaml.safe_load(f) or {}

    if not base_config and not local_config:
        logger.error(f"No config found. Expected {CONFIG_FILE} and/or {CONFIG_LOCAL_FILE}")
        logger.info("Create config.local.yaml from config.yaml.example and configure it")
        sys.exit(1)

    if local_config:
        return deep_merge(base_config, local_config)  # type: ignore[arg-type]
    return base_config


def start_monitors(config: Dict) -> List[ChannelMonitor]:
    """Start monitoring all enabled channels"""
    monitors = []
    channels = config.get("channels", [])
    
    for channel_config in channels:
        monitor = ChannelMonitor(channel_config)
        if monitor.start():
            monitors.append(monitor)
            stream_status[monitor.name] = monitor.get_status()
    
    return monitors


def stop_monitors(monitors: List[ChannelMonitor]):
    """Stop all monitors"""
    for monitor in monitors:
        monitor.stop()


@app.route('/status')
def status():
    """Get status of all channels"""
    return jsonify({
        "status": "running",
        "channels": [monitor.get_status() for monitor in active_monitors],
        "timestamp": time.time()
    })


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})


def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info("Shutting down PoliceTracker...")
    stop_monitors(active_monitors)
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global monitors list
active_monitors: List[ChannelMonitor] = []

if __name__ == "__main__":
    logger.info("Starting PoliceTracker...")
    
    # Load configuration
    config = load_config()
    
    # Get web API endpoint from config
    alerts_config = config.get("alerts", {})
    web_api_url = alerts_config.get("api_endpoint") or os.environ.get("WEB_API_URL")
    webhook_url = alerts_config.get("webhook_url") or os.environ.get("WEBHOOK_URL")
    api_token = alerts_config.get("api_token") or os.environ.get("API_TOKEN")
    processing_config = config.get("processing", {})
    if web_api_url:
        logger.info(f"Web API endpoint configured: {web_api_url}")
    else:
        logger.warning("No web API endpoint configured - events will not be sent to dashboard")
    if webhook_url:
        logger.info("Webhook endpoint configured")
    
    # Start monitoring channels
    active_monitors = start_monitors(config)
    
    if not active_monitors:
        logger.warning("No channels enabled or configured")
    
    # Start Flask API server
    server_config = config.get("server", {})
    host = server_config.get("host", "0.0.0.0")
    port = server_config.get("port", 8891)
    
    logger.info(f"Starting API server on {host}:{port}")
    logger.info(f"Monitoring {len(active_monitors)} channel(s)")
    
    app.run(host=host, port=port, debug=server_config.get("debug", False), threaded=True)
