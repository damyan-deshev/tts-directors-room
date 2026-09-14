#!/usr/bin/env python3
"""Local, dependency-free direction editor for the Higgs narration layer."""

from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import directors_room as director
from project_store import ProjectStore


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
EXPORT_DIR = APP_DIR / "exports"
PREVIEW_DIR = APP_DIR / "preview-audio"
LOG_DIR = APP_DIR / "logs"
LAB_GENERATION_LOG = LOG_DIR / "lab-generation.jsonl"
STORY_PATH = DATA_DIR / "story.json"
ANNOTATION_PATH = DATA_DIR / "annotations.json"
LAB_PATH = DATA_DIR / "lab.json"
DIRECTOR_PATH = DATA_DIR / "directors-room.json"
TAKES_DIR = APP_DIR / "takes"
RENDER_DIR = APP_DIR / "renders"
WAVEFORM_DIR = APP_DIR / "waveforms"
CONFIG_PATH = APP_DIR / "config.local.json"
DIRECTOR_STORE = ProjectStore(DIRECTOR_PATH)


def configured_higgs_endpoint() -> str:
    explicit = os.getenv("HIGGS_ENDPOINT") or os.getenv("HIGGS_PREVIEW_URL")
    if explicit:
        return explicit.rstrip("/")
    if CONFIG_PATH.is_file():
        try:
            configured = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            endpoint = str(configured.get("higgs_endpoint", "")).strip()
            if endpoint:
                return endpoint.rstrip("/")
        except (OSError, ValueError, TypeError):
            pass
    return "http://127.0.0.1:8765"


HIGGS_ENDPOINT = configured_higgs_endpoint()
PREVIEW_LOCK = threading.Lock()
LAB_LOCK = threading.RLock()
AUDIT_LOG_LOCK = threading.Lock()
SPEAKER_QA_LOCK = threading.RLock()
SPEAKER_QA_RUNTIME = None
SPEAKER_QA_ANCHORS: dict[str, object] = {}
SPEAKER_QA_THRESHOLD = 0.95
DIRECTOR_ACTIVITY_LOCK = threading.RLock()
DIRECTOR_GENERATION_LOCK = threading.Lock()
DIRECTOR_GENERATION_ACTIVITY: dict[str, object] = {
    "state": "idle",
    "cancellable": False,
    "message": "No Higgs request is running.",
}
BASELINE_ACTIVITY_LOCK = threading.RLock()
BASELINE_ACTIVITY: dict[str, object] = {
    "state": "idle",
    "message": "No baseline first-read job is running.",
}
QA_LOCK = threading.Lock()
QA_JOBS_LOCK = threading.RLock()
QA_JOBS: dict[str, dict] = {}
RENDER_LOCK = threading.Lock()
EXPORT_LOCK = threading.Lock()
ASR_MODEL = os.getenv("DIRECTORS_ROOM_ASR_MODEL", "mlx-community/whisper-large-v3-turbo")

EMOTIONS = [
    "elation", "amusement", "enthusiasm", "determination", "pride",
    "contentment", "affection", "relief", "contemplation", "confusion",
    "surprise", "awe", "longing", "arousal", "anger", "fear", "disgust",
    "bitterness", "sadness", "shame", "helplessness",
]
EXPRESSIVENESS = ["", "expressive_low", "expressive_high"]
STYLES = ["", "singing", "shouting", "whispering"]
SPEEDS = ["", "speed_very_slow", "speed_slow", "speed_fast", "speed_very_fast"]
PITCHES = ["", "pitch_low", "pitch_high"]
OPENING_LEAD_IN_EMOTIONS = {"sadness", "bitterness"}
TAG_RE = re.compile(r"<\|(?:emotion|style|prosody):[a-z_]+\|>")

VOICE_CONFIG_PATH = Path(
    os.getenv("HIGGS_VOICES_CONFIG", str(APP_DIR / "voices.local.json"))
).expanduser()


def load_voice_references() -> dict[str, dict]:
    """Load private voice paths from an untracked operator-owned registry."""
    if not VOICE_CONFIG_PATH.is_file():
        return {}
    raw = json.loads(VOICE_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("voices.local.json must contain an object keyed by voice id.")
    voices: dict[str, dict] = {}
    for voice_id, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"Voice {voice_id} must be an object.")
        audio = Path(str(value.get("audio", ""))).expanduser()
        transcript = Path(str(value.get("transcript", ""))).expanduser()
        if not audio.is_absolute():
            audio = (VOICE_CONFIG_PATH.parent / audio).resolve()
        if not transcript.is_absolute():
            transcript = (VOICE_CONFIG_PATH.parent / transcript).resolve()
        voices[str(voice_id)] = {
            "label": str(value.get("label") or voice_id),
            "description": str(value.get("description") or ""),
            "audio": audio,
            "transcript": transcript,
        }
    return voices


VOICE_REFERENCES = load_voice_references()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def log_lab_generation(event: str, request_id: str, **fields: object) -> dict:
    """Append one privacy-safe request/voice/QA event to the local audit log."""
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "event": event,
        "request_id": request_id,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with AUDIT_LOG_LOCK:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LAB_GENERATION_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(f"LAB_GENERATION {line}", flush=True)
    return record


def set_director_generation_activity(**fields: object) -> None:
    with DIRECTOR_ACTIVITY_LOCK:
        DIRECTOR_GENERATION_ACTIVITY.clear()
        DIRECTOR_GENERATION_ACTIVITY.update(fields)


def director_generation_activity() -> dict:
    with DIRECTOR_ACTIVITY_LOCK:
        return deepcopy(DIRECTOR_GENERATION_ACTIVITY)


def set_baseline_activity(*, replace: bool = False, **fields: object) -> None:
    with BASELINE_ACTIVITY_LOCK:
        if replace:
            BASELINE_ACTIVITY.clear()
        BASELINE_ACTIVITY.update(fields)


def baseline_activity() -> dict:
    with BASELINE_ACTIVITY_LOCK:
        return deepcopy(BASELINE_ACTIVITY)


def delivery_diagnostics(prompt: str, controls: dict) -> dict:
    global_tokens = [value for value in controls.values() if value]
    inline_tokens = TAG_RE.findall(prompt)
    return {
        "global_tokens": global_tokens,
        "global_token_count": len(global_tokens),
        "inline_token_count": len(inline_tokens),
        "stacked_global_controls": len(global_tokens) > 1,
    }


def _speaker_runtime() -> dict:
    """Load WavLM lazily once; the editor process keeps it resident afterward."""
    global SPEAKER_QA_RUNTIME
    with SPEAKER_QA_LOCK:
        if SPEAKER_QA_RUNTIME is not None:
            return SPEAKER_QA_RUNTIME
        import librosa
        import numpy as np
        import torch
        from transformers import AutoFeatureExtractor, WavLMForXVector

        model_id = "microsoft/wavlm-base-plus-sv"
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        extractor = AutoFeatureExtractor.from_pretrained(model_id, local_files_only=True)
        model = WavLMForXVector.from_pretrained(model_id, local_files_only=True).to(device).eval()
        SPEAKER_QA_RUNTIME = {
            "librosa": librosa,
            "np": np,
            "torch": torch,
            "extractor": extractor,
            "model": model,
            "model_id": model_id,
            "device": device,
        }
        return SPEAKER_QA_RUNTIME


def _speaker_embedding(audio, sample_rate: int, runtime: dict):
    librosa = runtime["librosa"]
    torch = runtime["torch"]
    if sample_rate != 16_000:
        audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16_000)
    inputs = runtime["extractor"](audio, sampling_rate=16_000, return_tensors="pt")
    inputs = {key: value.to(runtime["device"]) for key, value in inputs.items()}
    with torch.inference_mode():
        embedding = runtime["model"](**inputs).embeddings[0]
    embedding = embedding / embedding.norm()
    return embedding.cpu().numpy()


def speaker_qa(audio_path: Path, voice_mode: str) -> dict:
    """Compare a generated preview with its selected immutable audio anchor."""
    voice = VOICE_REFERENCES[voice_mode]
    try:
        with SPEAKER_QA_LOCK:
            runtime = _speaker_runtime()
            librosa = runtime["librosa"]
            np = runtime["np"]
            audio, sample_rate = librosa.load(audio_path, sr=None, mono=True)
            anchor_key = f"{voice_mode}:{file_sha256(voice['audio'])}"
            anchor = SPEAKER_QA_ANCHORS.get(anchor_key)
            if anchor is None:
                anchor_audio, anchor_rate = librosa.load(voice["audio"], sr=None, mono=True)
                anchor = {
                    "embedding": _speaker_embedding(anchor_audio, anchor_rate, runtime),
                    "median_f0_hz": _median_f0(anchor_audio, anchor_rate, runtime),
                }
                SPEAKER_QA_ANCHORS[anchor_key] = anchor
            embedding = _speaker_embedding(audio, sample_rate, runtime)
            cosine = float(embedding @ anchor["embedding"])
            return {
                "available": True,
                "model": runtime["model_id"],
                "device": runtime["device"],
                "reference_cosine": round(cosine, 4),
                "threshold": SPEAKER_QA_THRESHOLD,
                "speaker_anomaly": cosine < SPEAKER_QA_THRESHOLD,
                "median_f0_hz": _median_f0(audio, sample_rate, runtime),
                "anchor_median_f0_hz": anchor["median_f0_hz"],
            }
    except Exception as exc:
        return {
            "available": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _median_f0(audio, sample_rate: int, runtime: dict) -> float | None:
    frequencies, _, _ = runtime["librosa"].pyin(
        audio,
        fmin=55,
        fmax=400,
        sr=sample_rate,
    )
    voiced = frequencies[runtime["np"].isfinite(frequencies)]
    return round(float(runtime["np"].median(voiced)), 1) if len(voiced) else None


def tts_text(text: str) -> str:
    """Apply approved narration-only normalizations; never touch canonical text."""
    if text.strip() == "- ?":
        return "— Хм?"
    spoken = re.sub(r"^-\s+", "— ", text)
    return spoken.replace("Уга буга", "у̀га бу̀га")


def spoken_higgs_text(text: str) -> str:
    return " ".join(TAG_RE.sub("", text).split())


def qa_policy_snapshot(project: dict, take_audio_sha256: str, voice: dict) -> dict:
    configured = project.get("qa_policy") or {}
    return {
        "version": 1,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "take_audio_sha256": take_audio_sha256,
        "reference_audio_sha256": voice.get("audio_sha256"),
        "gates": {
            "speaker_similarity": {
                "enabled": bool((configured.get("speaker_similarity") or {}).get("enabled", True)),
                "model": "microsoft/wavlm-base-plus-sv",
                "threshold": SPEAKER_QA_THRESHOLD,
                "minimum_duration_seconds": 4.0,
            },
            "asr_content": {
                "enabled": bool((configured.get("asr_content") or {}).get("enabled", True)),
                "model": ASR_MODEL,
                "minimum_expected_words": 4,
            },
        },
    }


def initial_qa_evaluations(snapshot: dict) -> list[dict]:
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    return [
        {
            "id": f"qa-{uuid.uuid4().hex[:12]}",
            "gate": gate,
            "state": "pending" if policy.get("enabled") else "skipped",
            "created_at": created,
            "policy": deepcopy(policy),
            "result": {},
        }
        for gate, policy in snapshot["gates"].items()
    ]


def _normalized_asr_words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ѝ", "и"))


def transcript_metrics(expected: str, transcript: str) -> dict:
    expected_words = _normalized_asr_words(expected)
    transcript_words = _normalized_asr_words(transcript)
    matcher = difflib.SequenceMatcher(None, expected_words, transcript_words, autojunk=False)
    opcodes = matcher.get_opcodes()
    missing_runs = [e1 - e0 for tag, e0, e1, _, _ in opcodes if tag in {"delete", "replace"}]
    suffix_size = min(18, max(4, len(expected_words) // 4), len(expected_words), len(transcript_words))
    suffix = (
        difflib.SequenceMatcher(None, expected_words[-suffix_size:], transcript_words[-suffix_size:]).ratio()
        if suffix_size else 0.0
    )
    repeated = False
    for size in range(min(10, len(transcript_words) // 2), 3, -1):
        expected_counts = collections.Counter(
            tuple(expected_words[index:index + size])
            for index in range(max(0, len(expected_words) - size + 1))
        )
        actual_counts = collections.Counter(
            tuple(transcript_words[index:index + size])
            for index in range(max(0, len(transcript_words) - size + 1))
        )
        if any(count >= 2 and count > expected_counts[phrase] for phrase, count in actual_counts.items()):
            repeated = True
            break
    length_ratio = len(transcript_words) / max(1, len(expected_words))
    similarity = matcher.ratio()
    longest_missing = max(missing_runs, default=0)
    acceptable = (
        length_ratio >= 0.85
        and suffix >= 0.65
        and similarity >= 0.72
        and longest_missing <= max(3, round(len(expected_words) * 0.12))
        and not repeated
    )
    return {
        "expected_words": len(expected_words),
        "transcript_words": len(transcript_words),
        "length_ratio": round(length_ratio, 4),
        "full_similarity": round(similarity, 4),
        "suffix_similarity": round(suffix, 4),
        "longest_missing_run_words": longest_missing,
        "unexpected_repetition": repeated,
        "content_acceptable": acceptable,
    }


def asr_qa(audio_path: Path, expected: str) -> dict:
    words = _normalized_asr_words(expected)
    if len(words) < 4:
        return {"state": "abstain", "reason": "expected_text_too_short", "expected_words": len(words)}
    try:
        import mlx_whisper

        started = time.perf_counter()
        transcription = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=ASR_MODEL,
            language="bg",
            verbose=False,
            condition_on_previous_text=False,
            word_timestamps=True,
        )
        transcript = str(transcription.get("text") or "").strip()
        metrics = transcript_metrics(expected, transcript)
        return {
            "state": "pass" if metrics["content_acceptable"] else "fail",
            "model": ASR_MODEL,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "expected_text_sha256": director.sha256_text(expected),
            "transcript_sha256": director.sha256_text(transcript),
            "metrics": metrics,
        }
    except Exception as exc:
        return {"state": "error", "error_type": type(exc).__name__, "error": str(exc)}


def available_voices() -> list[dict]:
    """Expose safe labels, fingerprints, and preview URLs; never local paths."""
    voices = []
    for voice_id, voice in VOICE_REFERENCES.items():
        if voice["audio"].is_file() and voice["transcript"].is_file():
            voices.append(
                {
                    "id": voice_id,
                    "label": voice["label"],
                    "description": voice["description"],
                    "preview_url": f"/reference-audio/{voice_id}.wav",
                    "audio_sha256": file_sha256(voice["audio"]),
                    "transcript_sha256": file_sha256(voice["transcript"]),
                }
            )
    return voices


def default_voice_mode() -> str:
    voices = available_voices()
    return voices[0]["id"] if voices else ""


def migrated_lab_text(story: dict, annotations: dict) -> str:
    """Turn the old hidden utterance layer into visible, editable Higgs tags."""
    utterances_by_paragraph: dict[str, list[dict]] = {}
    utterance_by_id = {utterance["id"]: utterance for utterance in story["utterances"]}
    for utterance in story["utterances"]:
        utterances_by_paragraph.setdefault(utterance["paragraph_id"], []).append(utterance)

    base_emotion = annotations["global"]["emotion"]
    active_emotion = base_emotion
    paragraphs: list[str] = []
    if annotations["include_title"]:
        paragraphs.append(story["title"])
        if story.get("author"):
            byline = "От" if story.get("language") == "bg" else "By"
            paragraphs.append(f"{byline} {story['author']}")

    for paragraph in story["paragraphs"]:
        parts: list[str] = []
        for unit in paragraph_units(
            paragraph,
            700,
            utterances_by_paragraph.get(paragraph["id"], []),
        ):
            utterance = utterance_by_id.get(unit["utterance_id"])
            selected = (
                annotations["utterances"].get(utterance["id"], {}).get("emotion")
                if utterance
                else None
            )
            desired_emotion = selected if selected and selected != "inherit" else base_emotion
            prefix = ""
            if desired_emotion != active_emotion and desired_emotion:
                prefix = f"<|emotion:{desired_emotion}|>"
            active_emotion = desired_emotion
            parts.append(prefix + unit["text"])
        paragraphs.append("".join(parts))
    return "\n\n".join(paragraphs)


def default_lab_state(story: dict, annotations: dict) -> dict:
    return {
        "version": 1,
        "story_sha256": story["source"]["narration_sha256"],
        "text": migrated_lab_text(story, annotations),
        "voice_mode": default_voice_mode(),
        "controls": {
            # The validated long-form baseline deliberately stacks no emotion
            # with expressiveness. Combined delivery tokens can overpower the
            # reference speaker and reproducibly snap to a different voice.
            "emotion": "",
            "style": "",
            "speed": "",
            "pitch": "",
            "expressiveness": "expressive_low",
        },
        "target_chars": annotations["target_chars"],
        "temperature": 0.8,
        "top_k": 50,
        "seed": 42,
        "max_tokens": 3000,
        "migration": "visible-tags-from-annotations-v3",
    }


def validate_lab(payload: dict, story: dict) -> dict:
    text = str(payload.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("The laboratory text cannot be empty.")
    if len(text) > 500_000:
        raise ValueError("The laboratory text is too large.")
    validate_inline_higgs_tags(text)

    voices = {voice["id"] for voice in available_voices()}
    voice_mode = str(payload.get("voice_mode") or default_voice_mode())
    if voice_mode not in voices:
        raise ValueError("The selected voice reference is unavailable.")

    incoming_controls = payload.get("controls") or {}
    controls = {
        "emotion": str(incoming_controls.get("emotion", "")),
        "style": str(incoming_controls.get("style", "")),
        "speed": str(incoming_controls.get("speed", "")),
        "pitch": str(incoming_controls.get("pitch", "")),
        "expressiveness": str(incoming_controls.get("expressiveness", "")),
    }
    valid_controls = {
        "emotion": {"", *EMOTIONS},
        "style": set(STYLES),
        "speed": set(SPEEDS),
        "pitch": set(PITCHES),
        "expressiveness": set(EXPRESSIVENESS),
    }
    for name, value in controls.items():
        if value not in valid_controls[name]:
            raise ValueError(f"Unknown {name} control: {value}")

    target_chars = int(payload.get("target_chars", 380))
    if not 220 <= target_chars <= 700:
        raise ValueError("Target chunk length must be between 220 and 700 characters.")
    temperature = float(payload.get("temperature", 0.8))
    if not 0.1 <= temperature <= 2.0:
        raise ValueError("Temperature must be between 0.1 and 2.0.")
    top_k = int(payload.get("top_k", 50))
    if not 1 <= top_k <= 200:
        raise ValueError("Top K must be between 1 and 200.")
    seed = int(payload.get("seed", 42))
    if not 0 <= seed <= 2_147_483_647:
        raise ValueError("Seed must be between 0 and 2147483647.")
    max_tokens = int(payload.get("max_tokens", 3000))
    if not 256 <= max_tokens <= 4096:
        raise ValueError("Max frames must be between 256 and 4096.")

    return {
        "version": 1,
        "story_sha256": story["source"]["narration_sha256"],
        "text": text,
        "voice_mode": voice_mode,
        "controls": controls,
        "target_chars": target_chars,
        "temperature": temperature,
        "top_k": top_k,
        "seed": seed,
        "max_tokens": max_tokens,
        "migration": str(payload.get("migration", "manual-lab")),
    }


def validate_inline_higgs_tags(text: str) -> None:
    """Reject malformed or invented Higgs inline tokens before they are spoken."""
    remainder = TAG_RE.sub("", text)
    if "<|" in remainder or "|>" in remainder:
        raise ValueError("Malformed or unsupported Higgs inline token.")
    valid = {
        "emotion": set(EMOTIONS),
        "style": {value for value in STYLES if value},
        "prosody": {value for value in EXPRESSIVENESS if value},
    }
    for match in TAG_RE.finditer(text):
        category, value = match.group()[2:-2].split(":", 1)
        if value not in valid[category]:
            raise ValueError(f"Unsupported inline Higgs token: {match.group()}")


def load_lab_state(story: dict, annotations: dict) -> dict:
    if LAB_PATH.is_file():
        payload = load_json(LAB_PATH)
        if payload.get("story_sha256") != story["source"]["narration_sha256"]:
            raise ValueError("The laboratory belongs to a different story revision.")
        return validate_lab(payload, story)
    payload = default_lab_state(story, annotations)
    atomic_json(LAB_PATH, payload)
    return payload


def _spoken_text(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", text)).strip()


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in re.finditer(r"\n[ \t]*\n+", text):
        span = _trimmed_span(text, start, boundary.start())
        if span:
            spans.append(span)
        start = boundary.end()
    span = _trimmed_span(text, start, len(text))
    if span:
        spans.append(span)
    return spans


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    for boundary in re.finditer(r"(?<=[.!?…])\s+", text[start:end]):
        absolute_start = start + boundary.start()
        span = _trimmed_span(text, cursor, absolute_start)
        if span:
            spans.append(span)
        cursor = start + boundary.end()
    span = _trimmed_span(text, cursor, end)
    if span:
        spans.append(span)
    return spans


def _bounded_spans(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    if len(_spoken_text(text[start:end])) <= limit:
        return [(start, end)]
    words = list(re.finditer(r"\S+", text[start:end]))
    if len(words) < 2:
        return [(start, end)]
    result: list[tuple[int, int]] = []
    chunk_start = start + words[0].start()
    chunk_end = start + words[0].end()
    for word in words[1:]:
        next_end = start + word.end()
        if len(_spoken_text(text[chunk_start:next_end])) > limit:
            result.append((chunk_start, chunk_end))
            chunk_start = start + word.start()
        chunk_end = next_end
    result.append((chunk_start, chunk_end))
    return result


def plan_lab_chunks(text: str, target: int) -> list[dict]:
    """Return editable-text offsets for the exact chunks previewed by the lab."""
    soft_max = target + 100
    units: list[dict] = []
    for paragraph_start, paragraph_end in _paragraph_spans(text):
        sentences = _sentence_spans(text, paragraph_start, paragraph_end)
        bounded = [
            span
            for sentence_start, sentence_end in sentences
            for span in _bounded_spans(text, sentence_start, sentence_end, soft_max)
        ]
        for index, (unit_start, unit_end) in enumerate(bounded):
            units.append(
                {
                    "start": unit_start,
                    "end": unit_end,
                    "characters": len(_spoken_text(text[unit_start:unit_end])),
                    "paragraph_end": index == len(bounded) - 1,
                }
            )
    if not units:
        return []

    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for unit in units:
        projected = current_chars + (1 if current else 0) + unit["characters"]
        if current and projected > soft_max:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += (1 if current_chars else 0) + unit["characters"]
        if current_chars >= target and unit["paragraph_end"]:
            groups.append(current)
            current = []
            current_chars = 0
    if current:
        groups.append(current)

    chunks: list[dict] = []
    for index, group in enumerate(groups):
        start = 0 if index == 0 else group[0]["start"]
        end = groups[index + 1][0]["start"] if index + 1 < len(groups) else len(text)
        raw = text[start:end]
        spoken = _spoken_text(raw)
        chunks.append(
            {
                "id": f"c{index + 1:03d}",
                "index": index + 1,
                "start": start,
                "end": end,
                "characters": len(spoken),
                "raw_characters": len(raw),
                "boundary_after": "paragraph" if group[-1]["paragraph_end"] else "sentence",
                "tags": TAG_RE.findall(raw),
                "preview": spoken[:180] + ("…" if len(spoken) > 180 else ""),
            }
        )
    return chunks


def validate_annotations(payload: dict, story: dict) -> dict:
    if payload.get("story_sha256") != story["source"]["narration_sha256"]:
        raise ValueError("The annotation file belongs to a different story revision.")
    target = int(payload.get("target_chars", 380))
    if not 220 <= target <= 700:
        raise ValueError("Target chunk length must be between 220 and 700 characters.")
    global_controls = payload.get("global") or {}
    global_emotion = str(global_controls.get("emotion", ""))
    expressiveness = str(global_controls.get("expressiveness", "expressive_low"))
    if global_emotion and global_emotion not in EMOTIONS:
        raise ValueError("Unknown global emotion.")
    if expressiveness not in EXPRESSIVENESS:
        raise ValueError("Unknown expressiveness value.")

    expected = {utterance["id"] for utterance in story["utterances"]}
    incoming = set((payload.get("utterances") or {}).keys())
    if incoming != expected:
        raise ValueError("Utterance annotations must match the current story map.")
    utterances: dict[str, dict[str, str]] = {}
    for utterance_id in sorted(expected):
        emotion = str(payload["utterances"][utterance_id].get("emotion", "inherit"))
        if emotion not in {"inherit", *EMOTIONS}:
            raise ValueError(f"Unknown emotion for {utterance_id}.")
        utterances[utterance_id] = {"emotion": emotion}

    return {
        "version": 3,
        "story_sha256": story["source"]["narration_sha256"],
        "include_title": bool(payload.get("include_title", True)),
        "target_chars": target,
        "global": {"emotion": global_emotion, "expressiveness": expressiveness},
        "utterances": utterances,
    }


def sentence_units(paragraph: dict, target: int) -> list[dict]:
    canonical = paragraph["text"]
    soft_max = target + 100
    if len(canonical) <= soft_max:
        return [{"paragraph_id": paragraph["id"], "utterance_id": None, "canonical_text": canonical, "text": tts_text(canonical), "paragraph_end": True}]
    sentences = [piece.strip() for piece in re.split(r"(?<=[.!?…])\s+", canonical) if piece.strip()]
    if len(sentences) == 1:
        return [{"paragraph_id": paragraph["id"], "utterance_id": None, "canonical_text": canonical, "text": tts_text(canonical), "paragraph_end": True}]
    units: list[dict] = []
    for index, sentence in enumerate(sentences):
        units.append(
            {
                "paragraph_id": paragraph["id"],
                "utterance_id": None,
                "canonical_text": sentence,
                "text": tts_text(sentence),
                "paragraph_end": index == len(sentences) - 1,
            }
        )
    return units


def paragraph_units(paragraph: dict, target: int, utterances: list[dict]) -> list[dict]:
    """Split only at authored speech spans; preserve the normalized paragraph."""
    if not utterances:
        return sentence_units(paragraph, target)
    canonical = paragraph["text"]
    pieces: list[dict] = []
    cursor = 0
    for utterance in sorted(utterances, key=lambda item: item["start"]):
        if cursor < utterance["start"]:
            text = canonical[cursor:utterance["start"]]
            if text:
                pieces.append({"utterance_id": None, "canonical_text": text, "exact_join": True})
        text = canonical[utterance["start"]:utterance["end"]]
        if text:
            pieces.append({"utterance_id": utterance["id"], "canonical_text": text, "exact_join": True})
        cursor = utterance["end"]
    if cursor < len(canonical):
        text = canonical[cursor:]
        if text:
            pieces.append({"utterance_id": None, "canonical_text": text, "exact_join": True})
    for index, piece in enumerate(pieces):
        piece.update(
            {
                "paragraph_id": paragraph["id"],
                "text": tts_text(piece["canonical_text"]),
                "paragraph_end": index == len(pieces) - 1,
            }
        )
    if "".join(piece["canonical_text"] for piece in pieces) != canonical:
        raise ValueError(f"Speech-span split changed {paragraph['id']}.")
    return pieces


def delivery_tags(emotion: str, expressiveness: str) -> str:
    tags: list[str] = []
    if emotion:
        tags.append(f"<|emotion:{emotion}|>")
    if expressiveness:
        tags.append(f"<|prosody:{expressiveness}|>")
    return "".join(tags)


def plan_chunks(story: dict, annotations: dict) -> list[dict]:
    by_id = {paragraph["id"]: paragraph for paragraph in story["paragraphs"]}
    beat_by_paragraph = {
        paragraph_id: beat
        for beat in story["beats"]
        for paragraph_id in beat["paragraph_ids"]
    }
    utterances_by_paragraph: dict[str, list[dict]] = {}
    utterance_by_id = {utterance["id"]: utterance for utterance in story["utterances"]}
    for utterance in story["utterances"]:
        utterances_by_paragraph.setdefault(utterance["paragraph_id"], []).append(utterance)
    target = annotations["target_chars"]
    soft_max = target + 100
    chunks: list[dict] = []
    current: list[dict] = []
    current_chars = 0
    current_beat: dict | None = None
    current_utterance: dict | None = None
    current_emotion = ""

    def flush() -> None:
        nonlocal current, current_chars, current_beat, current_utterance, current_emotion
        if not current or current_beat is None:
            return
        text_parts: list[str] = []
        canonical_parts: list[str] = []
        paragraph_ids: list[str] = []
        previous_id = ""
        for unit in current:
            if unit["paragraph_id"] != previous_id:
                if text_parts:
                    # A planned Higgs chunk is one continuous generation.  Keep
                    # authored paragraphs in canonical_text and the manifest,
                    # but do not let the preview backend silently turn them
                    # back into independent generation calls.
                    text_parts.append(" ")
                    canonical_parts.append("\n\n")
                paragraph_ids.append(unit["paragraph_id"])
            elif text_parts:
                separator = "" if unit.get("exact_join") else " "
                text_parts.append(separator)
                canonical_parts.append(separator)
            text_parts.append(unit["text"])
            canonical_parts.append(unit["canonical_text"])
            previous_id = unit["paragraph_id"]
        text = "".join(text_parts)
        canonical_text = "".join(canonical_parts)
        expressiveness = annotations["global"]["expressiveness"]
        tag = delivery_tags(current_emotion, expressiveness)
        prompted_text = tag + text
        lead_in_emotion: str | None = None
        if (
            paragraph_ids[:2] == ["p001", "p002"]
            and current_emotion in OPENING_LEAD_IN_EMOTIONS
        ):
            lead = tts_text(by_id["p001"]["text"])
            remainder = text[len(lead):].lstrip()
            prompted_text = (
                delivery_tags("", expressiveness)
                + lead
                + " "
                + delivery_tags(current_emotion, expressiveness)
                + remainder
            )
            lead_in_emotion = current_emotion
        chunks.append(
            {
                "id": f"c{len(chunks) + 1:03d}",
                "beat_id": current_beat["id"],
                "beat_title": current_beat["title"],
                "utterance_id": current_utterance["id"] if current_utterance else None,
                "paragraph_ids": paragraph_ids,
                "emotion": current_emotion or None,
                "expressiveness": expressiveness or None,
                "neutral_lead_in_before_emotion": lead_in_emotion,
                "text": text,
                "canonical_text": canonical_text,
                "prompted_text": prompted_text,
                "characters": len(text),
            }
        )
        current = []
        current_chars = 0
        current_beat = None
        current_utterance = None
        current_emotion = ""

    for paragraph in story["paragraphs"]:
        beat = beat_by_paragraph[paragraph["id"]]
        units = paragraph_units(
            paragraph, target, utterances_by_paragraph.get(paragraph["id"], [])
        )
        for unit in units:
            utterance = utterance_by_id.get(unit["utterance_id"])
            selected = (
                annotations["utterances"].get(utterance["id"], {}).get("emotion")
                if utterance else None
            )
            active_utterance = utterance if selected and selected != "inherit" else None
            emotion = selected if active_utterance else annotations["global"]["emotion"]
            profile = (
                beat["id"],
                active_utterance["id"] if active_utterance else None,
                emotion,
            )
            active_profile = (
                current_beat["id"] if current_beat else None,
                current_utterance["id"] if current_utterance else None,
                current_emotion,
            )
            if current and profile != active_profile:
                flush()
            if not current:
                current_beat = beat
                current_utterance = active_utterance
                current_emotion = emotion
            separator = 0 if current and unit.get("exact_join") and unit["paragraph_id"] == current[-1]["paragraph_id"] else 1
            projected = current_chars + separator + len(unit["text"])
            if current and projected > soft_max:
                flush()
                current_beat = beat
                current_utterance = active_utterance
                current_emotion = emotion
            current.append(unit)
            current_chars += (separator if current_chars else 0) + len(unit["text"])
            if current_chars >= target and unit["paragraph_end"]:
                flush()
                current_beat = beat
                current_utterance = active_utterance
                current_emotion = emotion
    flush()

    if annotations["include_title"]:
        intro_emotion = annotations["global"]["emotion"] or None
        intro_tags = delivery_tags(intro_emotion or "", annotations["global"]["expressiveness"])
        byline = "От" if story.get("language") == "bg" else "By"
        author_text = f" {byline} {story['author']}." if story.get("author") else ""
        author_canonical = f"\n\n{byline} {story['author']}" if story.get("author") else ""
        intro_text = f"{story['title']}.{author_text}"
        intro_canonical = f"{story['title']}{author_canonical}"
        chunks.insert(
            0,
            {
                "id": "c000",
                "beat_id": "intro",
                "beat_title": "Заглавие",
                "utterance_id": None,
                "paragraph_ids": [],
                "emotion": intro_emotion,
                "expressiveness": annotations["global"]["expressiveness"] or None,
                "text": intro_text,
                "canonical_text": intro_canonical,
                "prompted_text": intro_tags + intro_text,
                "characters": len(intro_text),
            },
        )
    return chunks


def higgs_status() -> dict:
    try:
        with urllib.request.urlopen(f"{HIGGS_ENDPOINT}/api/status", timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {
            "reachable": True,
            "endpoint": HIGGS_ENDPOINT,
            "build": data.get("build") or data.get("version") or "unreported",
            "capabilities": {
                "stable_voice_reference": True,
                "inline_higgs_tokens": True,
                "seed": True,
                "temperature": True,
                "top_k": True,
                "neighbor_audio_conditioning": bool(data.get("neighbor_audio_conditioning", False)),
            },
            **data,
        }
    except (OSError, ValueError, urllib.error.URLError):
        return {
            "reachable": False,
            "ready": False,
            "model_loaded": False,
            "endpoint": HIGGS_ENDPOINT,
            "build": "unreachable",
            "capabilities": {},
        }


def preview_prompt(story: dict, annotations: dict, utterance_id: str, variant: str) -> tuple[str, str]:
    by_id = {paragraph["id"]: paragraph for paragraph in story["paragraphs"]}
    base_emotion = annotations["global"]["emotion"]
    expressive = annotations["global"]["expressiveness"]
    if utterance_id == "narrator":
        lead = tts_text(by_id["p001"]["text"])
        continuation = tts_text(by_id["p002"]["text"])
        if base_emotion in OPENING_LEAD_IN_EMOTIONS:
            text = (
                delivery_tags("", expressive)
                + lead
                + " "
                + delivery_tags(base_emotion, expressive)
                + continuation
            )
        else:
            text = delivery_tags(base_emotion, expressive) + lead + " " + continuation
        return text, "Общият глас"

    utterance = next((item for item in story["utterances"] if item["id"] == utterance_id), None)
    if utterance is None:
        raise ValueError("Unknown utterance.")
    if variant not in {"base", "override"}:
        raise ValueError("Preview variant must be base or override.")
    override = annotations["utterances"][utterance_id]["emotion"]
    parts: list[str] = []
    previous_profile: tuple[str, str] | None = None
    context_utterances: dict[str, list[dict]] = {}
    for item in story["utterances"]:
        if item["paragraph_id"] in utterance["preview_paragraph_ids"]:
            context_utterances.setdefault(item["paragraph_id"], []).append(item)
    for paragraph_id in utterance["preview_paragraph_ids"]:
        paragraph_parts: list[str] = []
        for unit in paragraph_units(by_id[paragraph_id], 700, context_utterances.get(paragraph_id, [])):
            emotion = base_emotion
            if variant == "override" and unit["utterance_id"] == utterance_id and override != "inherit":
                emotion = override
            profile = (emotion, expressive)
            text = unit["text"]
            if profile != previous_profile:
                text = delivery_tags(*profile) + text
            paragraph_parts.append(text)
            previous_profile = profile
        parts.append("".join(paragraph_parts))
    label = "Общият глас" if variant == "base" else f"{utterance['speaker']} · {override}"
    return " ".join(part for part in parts if part), label


def post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Higgs preview failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Higgs endpoint is unreachable: {url}") from exc


def generate_preview(story: dict, annotations: dict, utterance_id: str, variant: str) -> dict:
    prompt, label = preview_prompt(story, annotations, utterance_id, variant)
    voice_mode = default_voice_mode()
    if not voice_mode:
        raise ValueError("Configure at least one canonical voice reference first.")
    voice_label = VOICE_REFERENCES[voice_mode]["label"]
    request_payload = {
        "text": prompt,
        "voice_mode": voice_mode,
        "controls": {"emotion": "", "style": "", "speed": "", "pitch": "", "expressiveness": ""},
        "temperature": 0.8,
        "seed": 42,
        "chunk_chars": 700,
        "max_tokens": 3000,
        "top_k": 50,
        "rolling_context": False,
    }
    fingerprint = hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    safe_name = re.sub(r"[^a-z0-9-]", "-", f"{utterance_id}-{variant}")
    audio_path = PREVIEW_DIR / f"{safe_name}-{fingerprint}.wav"
    metadata_path = audio_path.with_suffix(".json")
    if audio_path.is_file() and metadata_path.is_file():
        metadata = load_json(metadata_path)
        return {**metadata, "audio_url": f"/preview-audio/{audio_path.name}", "cached": True}

    with PREVIEW_LOCK:
        if audio_path.is_file() and metadata_path.is_file():
            metadata = load_json(metadata_path)
            return {**metadata, "audio_url": f"/preview-audio/{audio_path.name}", "cached": True}
        result = post_json(f"{HIGGS_ENDPOINT}/api/generate", request_payload, timeout=900)
        backend_audio = str(result.get("audio_url") or "")
        if not backend_audio.startswith("/outputs/"):
            raise RuntimeError("Higgs preview returned no usable audio artifact.")
        with urllib.request.urlopen(f"{HIGGS_ENDPOINT}{backend_audio}", timeout=30) as response:
            audio = response.read()
        if len(audio) < 44 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise RuntimeError("Higgs preview returned an invalid WAV file.")
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        temporary = audio_path.with_suffix(".wav.tmp")
        temporary.write_bytes(audio)
        temporary.replace(audio_path)
        metadata = {
            "label": label,
            "utterance_id": utterance_id,
            "variant": variant,
            "duration_seconds": result.get("duration_seconds"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "job_id": result.get("job_id"),
            "model": result.get("model"),
            "anchor": voice_label,
            "seed": 42,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        atomic_json(metadata_path, metadata)
        return {**metadata, "audio_url": f"/preview-audio/{audio_path.name}", "cached": False}


def lab_state_payload() -> dict:
    story = load_json(STORY_PATH)
    annotations = validate_annotations(load_json(ANNOTATION_PATH), story)
    lab = load_lab_state(story, annotations)
    chunks = plan_lab_chunks(lab["text"], lab["target_chars"])
    return {
        "lab": lab,
        "chunks": chunks,
        "options": {
            "emotions": EMOTIONS,
            "styles": [value for value in STYLES if value],
            "speeds": [value for value in SPEEDS if value],
            "pitches": [value for value in PITCHES if value],
            "expressiveness": [value for value in EXPRESSIVENESS if value],
        },
        "voices": available_voices(),
        "engine": higgs_status(),
        "story": {
            "title": story["title"],
            "author": story["author"],
            "canonical_characters": story["stats"]["characters"],
        },
    }


def save_lab(payload: dict) -> tuple[dict, list[dict]]:
    story = load_json(STORY_PATH)
    lab = validate_lab(payload, story)
    chunks = plan_lab_chunks(lab["text"], lab["target_chars"])
    if not chunks:
        raise ValueError("No speakable chunks were found.")
    with LAB_LOCK:
        atomic_json(LAB_PATH, lab)
    return lab, chunks


def lab_generation_request(lab: dict, chunk: dict) -> tuple[str, dict]:
    raw_chunk = lab["text"][chunk["start"]:chunk["end"]]
    prompt = re.sub(r"\s+", " ", raw_chunk).strip()
    if not _spoken_text(prompt):
        raise ValueError("The selected chunk contains no speakable text.")
    return prompt, {
        "text": prompt,
        "voice_mode": lab["voice_mode"],
        "controls": lab["controls"],
        "temperature": lab["temperature"],
        "seed": lab["seed"],
        # The laboratory chunk is already a deliberate unit. Flattened
        # paragraph whitespace plus this ceiling keeps it one Higgs call.
        "chunk_chars": 5000,
        "max_tokens": lab["max_tokens"],
        "top_k": lab["top_k"],
        "rolling_context": False,
    }


def confirmed_backend_voice(
    result: dict,
    expected_voice_mode: str,
    expected_reference: dict | None = None,
) -> dict:
    confirmation = {
        "voice_mode": result.get("voice_mode"),
        "stable_anchor": result.get("stable_anchor"),
        "anchor_name": result.get("anchor_name"),
        "voice_cloned": result.get("voice_cloned"),
        "reference_audio_sha256": result.get("reference_audio_sha256"),
        "reference_text_sha256": result.get("reference_text_sha256"),
    }
    if (
        confirmation["voice_mode"] != expected_voice_mode
        or confirmation["stable_anchor"] is not True
        or confirmation["voice_cloned"] is not True
    ):
        raise RuntimeError(
            "Higgs did not confirm the selected stable voice reference; preview rejected."
        )
    expected_reference = expected_reference or {}
    reported = (
        confirmation["reference_audio_sha256"],
        confirmation["reference_text_sha256"],
    )
    expected = (
        expected_reference.get("audio_sha256"),
        expected_reference.get("transcript_sha256"),
    )
    if any(reported) and reported != expected:
        raise RuntimeError("Higgs reported a different canonical reference fingerprint.")
    confirmation["reference_fingerprint_attestation"] = (
        "verified" if all(reported) else "unavailable_from_endpoint"
    )
    return confirmation


def generate_lab_preview(lab: dict, chunk_id: str) -> dict:
    chunks = plan_lab_chunks(lab["text"], lab["target_chars"])
    chunk = next((item for item in chunks if item["id"] == chunk_id), None)
    if chunk is None:
        raise ValueError("The selected chunk no longer exists. Place the cursor again.")
    prompt, request_payload = lab_generation_request(lab, chunk)
    fingerprint = hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    request_id = uuid.uuid4().hex[:12]
    audio_path = PREVIEW_DIR / f"lab-{chunk_id}-{fingerprint}.wav"
    metadata_path = audio_path.with_suffix(".json")

    def cached_preview() -> dict:
        metadata = load_json(metadata_path)
        if "speaker_qa" not in metadata:
            qa = speaker_qa(audio_path, lab["voice_mode"])
            metadata["speaker_qa"] = qa
            atomic_json(metadata_path, metadata)
            log_lab_generation(
                "speaker_qa_completed",
                request_id,
                chunk_id=chunk_id,
                fingerprint=fingerprint,
                cached=True,
                voice_mode=lab["voice_mode"],
                **qa,
            )
        log_lab_generation(
            "cache_hit",
            request_id,
            chunk_id=chunk_id,
            fingerprint=fingerprint,
            job_id=metadata.get("job_id"),
            voice_mode=metadata.get("voice_mode"),
            speaker_qa=metadata.get("speaker_qa"),
        )
        return {**metadata, "audio_url": f"/preview-audio/{audio_path.name}", "cached": True}

    if audio_path.is_file() and metadata_path.is_file():
        return cached_preview()

    with PREVIEW_LOCK:
        if audio_path.is_file() and metadata_path.is_file():
            return cached_preview()
        voice = VOICE_REFERENCES[lab["voice_mode"]]
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        reference_audio_sha256 = file_sha256(voice["audio"])
        reference_text_sha256 = file_sha256(voice["transcript"])
        delivery = delivery_diagnostics(prompt, lab["controls"])
        log_lab_generation(
            "request_started",
            request_id,
            chunk_id=chunk_id,
            chunk_index=chunk["index"],
            chunk_characters=chunk["characters"],
            fingerprint=fingerprint,
            prompt_sha256=prompt_sha256,
            voice_mode=lab["voice_mode"],
            voice_label=voice["label"],
            reference_audio_sha256=reference_audio_sha256,
            reference_text_sha256=reference_text_sha256,
            controls=lab["controls"],
            delivery=delivery,
            seed=lab["seed"],
            temperature=lab["temperature"],
            top_k=lab["top_k"],
            max_frames=lab["max_tokens"],
        )
        started = time.perf_counter()
        try:
            result = post_json(f"{HIGGS_ENDPOINT}/api/generate", request_payload, timeout=900)
            backend_confirmation = confirmed_backend_voice(result, lab["voice_mode"])
            backend_audio = str(result.get("audio_url") or "")
            if not backend_audio.startswith("/outputs/"):
                raise RuntimeError("Higgs preview returned no usable audio artifact.")
            with urllib.request.urlopen(f"{HIGGS_ENDPOINT}{backend_audio}", timeout=30) as response:
                audio = response.read()
            if len(audio) < 44 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                raise RuntimeError("Higgs preview returned an invalid WAV file.")
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            temporary = audio_path.with_suffix(".wav.tmp")
            temporary.write_bytes(audio)
            temporary.replace(audio_path)
            qa = speaker_qa(audio_path, lab["voice_mode"])
            metadata = {
                "label": f"{chunk_id} · {voice['label']}",
                "chunk_id": chunk_id,
                "chunk_index": chunk["index"],
                "chunk_characters": chunk["characters"],
                "duration_seconds": result.get("duration_seconds"),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "job_id": result.get("job_id"),
                "model": result.get("model"),
                "voice_mode": lab["voice_mode"],
                "voice_label": voice["label"],
                "seed": lab["seed"],
                "prompt_sha256": prompt_sha256,
                "fingerprint": fingerprint,
                "reference_audio_sha256": reference_audio_sha256,
                "reference_text_sha256": reference_text_sha256,
                "controls": lab["controls"],
                "delivery": delivery,
                "backend_confirmation": backend_confirmation,
                "speaker_qa": qa,
            }
            atomic_json(metadata_path, metadata)
            log_lab_generation(
                "request_completed",
                request_id,
                chunk_id=chunk_id,
                fingerprint=fingerprint,
                job_id=result.get("job_id"),
                elapsed_seconds=round(time.perf_counter() - started, 2),
                duration_seconds=result.get("duration_seconds"),
                output_sha256=hashlib.sha256(audio).hexdigest(),
                backend_confirmation=backend_confirmation,
                speaker_qa=qa,
            )
            return {**metadata, "audio_url": f"/preview-audio/{audio_path.name}", "cached": False}
        except Exception as exc:
            log_lab_generation(
                "request_failed",
                request_id,
                chunk_id=chunk_id,
                fingerprint=fingerprint,
                elapsed_seconds=round(time.perf_counter() - started, 2),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise


def _safe_project_copy(project: dict) -> dict:
    payload = json.loads(json.dumps(project, ensure_ascii=False))
    for region in payload["regions"]:
        for take in region.get("takes", []):
            audio_file = take.get("audio_file")
            if audio_file:
                take["audio_url"] = "/" + audio_file.lstrip("/")
    for render in payload.get("renders", []):
        if render.get("audio_file"):
            render["audio_url"] = "/" + render["audio_file"].lstrip("/")
    return payload


def load_stored_project_offline() -> dict:
    """Read the authoritative project without touching the Higgs endpoint."""
    with LAB_LOCK:
        project = DIRECTOR_STORE.load()
        migrated, changed = director.migrate_project(project)
        return DIRECTOR_STORE.write_system(migrated, "offline_schema_migration") if changed else migrated


def _qa_jobs_payload() -> list[dict]:
    with QA_JOBS_LOCK:
        return [deepcopy(job) for job in QA_JOBS.values()][-20:]


def _append_qa_evaluation(region_id: str, take_id: str, evaluation: dict) -> None:
    with LAB_LOCK:
        project = load_stored_project_offline()
        region = next((item for item in project["regions"] if item["id"] == region_id), None)
        take = director.take_for(region, take_id) if region else None
        if take is None:
            raise ValueError("QA target disappeared.")
        take.setdefault("qa_evaluations", []).append(evaluation)
        DIRECTOR_STORE.write_system(project, f"qa_{evaluation['gate']}_{evaluation['state']}")


def _qa_worker(job_id: str, region_id: str, take_id: str, gates: list[str]) -> None:
    with QA_JOBS_LOCK:
        QA_JOBS[job_id].update(state="running", started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        with QA_LOCK:
            project = load_stored_project_offline()
            region = next(item for item in project["regions"] if item["id"] == region_id)
            take = director.take_for(region, take_id)
            if take is None:
                raise ValueError("Unknown QA take.")
            audio_path = APP_DIR / take["audio_file"]
            if not audio_path.is_file() or file_sha256(audio_path) != take.get("audio_sha256"):
                raise ValueError("Immutable take audio is missing or its hash changed.")
            snapshot = take.get("qa_policy_snapshot") or {}
            expected = spoken_higgs_text((take.get("transaction") or {}).get("higgs_request", {}).get("text", ""))
            for gate in gates:
                policy = (snapshot.get("gates") or {}).get(gate, {"enabled": True})
                created = datetime.now().astimezone().isoformat(timespec="seconds")
                if not policy.get("enabled", True):
                    result = {"state": "skipped", "reason": "disabled_in_take_policy_snapshot"}
                elif gate == "speaker_similarity":
                    duration = float(take.get("duration_seconds") or 0.0)
                    if duration and duration < float(policy.get("minimum_duration_seconds", 4.0)):
                        result = {"state": "abstain", "reason": "audio_too_short", "duration_seconds": duration}
                    else:
                        raw = speaker_qa(audio_path, (take.get("transaction") or {}).get("higgs_request", {}).get("voice_mode", ""))
                        result = {
                            "state": ("fail" if raw.get("speaker_anomaly") else "pass") if raw.get("available") else "error",
                            **raw,
                        }
                elif gate == "asr_content":
                    result = asr_qa(audio_path, expected)
                else:
                    raise ValueError(f"Unknown QA gate: {gate}")
                evaluation = {
                    "id": f"qa-{uuid.uuid4().hex[:12]}",
                    "gate": gate,
                    "state": result.pop("state"),
                    "created_at": created,
                    "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "policy": deepcopy(policy),
                    "audio_sha256": take.get("audio_sha256"),
                    "result": result,
                }
                _append_qa_evaluation(region_id, take_id, evaluation)
        with QA_JOBS_LOCK:
            QA_JOBS[job_id].update(state="completed", finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    except Exception as exc:
        with QA_JOBS_LOCK:
            QA_JOBS[job_id].update(
                state="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )


def start_qa_job(region_id: str, take_id: str, gates: list[str] | None = None) -> dict:
    gates = gates or ["speaker_similarity", "asr_content"]
    job_id = f"qa-job-{uuid.uuid4().hex[:12]}"
    job = {
        "id": job_id,
        "kind": "qa",
        "state": "queued",
        "region_id": region_id,
        "take_id": take_id,
        "gates": gates,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with QA_JOBS_LOCK:
        QA_JOBS[job_id] = job
    threading.Thread(
        target=_qa_worker,
        args=(job_id, region_id, take_id, gates),
        daemon=True,
        name=job_id,
    ).start()
    return deepcopy(job)


def request_director_qa(payload: dict) -> dict:
    project = load_stored_project_offline()
    targets = []
    requested_region = payload.get("region_id")
    requested_take = payload.get("take_id")
    for region in project["regions"]:
        if requested_region and region["id"] != requested_region:
            continue
        take = director.take_for(region, requested_take or region.get("active_take_id"))
        if take is not None:
            targets.append((region["id"], take["id"]))
    if not targets:
        raise ValueError("No take is available for QA.")
    jobs = [start_qa_job(region_id, take_id) for region_id, take_id in targets]
    return {"jobs": jobs}


def _legacy_take_import(project: dict, endpoint_status: dict) -> dict:
    """Keep old previews audible without pretending their provenance is complete."""
    voices = {voice["id"]: voice for voice in available_voices()}
    for region in project["regions"]:
        if region.get("takes"):
            continue
        candidates = sorted(
            PREVIEW_DIR.glob(f"lab-{region['id']}-*.json"),
            key=lambda path: path.stat().st_mtime,
        )
        for metadata_path in candidates:
            audio_path = metadata_path.with_suffix(".wav")
            if not audio_path.is_file():
                continue
            metadata = load_json(metadata_path)
            voice = voices.get(metadata.get("voice_mode"))
            if voice is None:
                continue
            current = director.current_transaction(
                project, region, voice, HIGGS_ENDPOINT, endpoint_status
            )
            prompt = " ".join(region["source_text"].split())
            prompt_matches = metadata.get("prompt_sha256") == director.sha256_text(prompt)
            current_request = current["higgs_request"]
            known_request_matches = (
                prompt_matches
                and metadata.get("voice_mode") == current_request["voice_mode"]
                and metadata.get("controls", {}) == current_request["controls"]
                and metadata.get("seed") == current_request["seed"]
                and metadata.get("top_k") == current_request["top_k"]
                and metadata.get("max_frames") == current_request["max_tokens"]
            )
            request = {
                "text": prompt if prompt_matches else None,
                "text_sha256": metadata.get("prompt_sha256"),
                "voice_mode": metadata.get("voice_mode"),
                "controls": metadata.get("controls", {}),
                "temperature": metadata.get("temperature"),
                "seed": metadata.get("seed"),
                "top_k": metadata.get("top_k"),
                "max_tokens": metadata.get("max_frames"),
                "rolling_context": False,
            }
            transaction = {
                **current,
                "operation": "legacy_import",
                "higgs_request": request,
                "model": metadata.get("model"),
                "build": "legacy-unreported",
                "generation_fingerprint": current["generation_fingerprint"] if known_request_matches else "legacy-stale:unknown",
                "freshness_fingerprint": current["freshness_fingerprint"] if known_request_matches else "legacy-unknown",
                "legacy_incomplete": not known_request_matches,
            }
            take_id = director.next_take_id(region)
            region["takes"].append(
                {
                    "id": take_id,
                    "created_at": datetime.fromtimestamp(
                        audio_path.stat().st_mtime
                    ).astimezone().isoformat(timespec="seconds"),
                    "audio_file": f"preview-audio/{audio_path.name}",
                    "audio_sha256": file_sha256(audio_path),
                    "duration_seconds": metadata.get("duration_seconds"),
                    "transaction": transaction,
                    "endpoint_response": {
                        "job_id": metadata.get("job_id"),
                        "backend_confirmation": metadata.get("backend_confirmation"),
                    },
                    "speaker_qa": metadata.get("speaker_qa", {"available": False}),
                    "human_judgement": {
                        "approved": False,
                        "approved_at": None,
                        "notes": "Импортиран preview от старата лаборатория.",
                    },
                    "edits": {
                        "trim_start_ms": 0,
                        "trim_end_ms": 0,
                        "inserted_pauses": [],
                    },
                }
            )
        if region["takes"]:
            region["active_take_id"] = region["takes"][-1]["id"]
    return project


def load_director_project() -> tuple[dict, dict, list[dict], dict]:
    with LAB_LOCK:
        story = load_json(STORY_PATH)
        annotations = validate_annotations(load_json(ANNOTATION_PATH), story)
        lab = load_lab_state(story, annotations)
        created = not DIRECTOR_PATH.is_file()
        migration_source = None
        store_warning = None

        if created:
            chunks = plan_lab_chunks(lab["text"], lab["target_chars"])
            project = director.new_project(story, lab, chunks)
        else:
            loaded = DIRECTOR_STORE.load()
            store_warning = DIRECTOR_STORE.last_warning
            if int(loaded.get("version", 0)) < director.PROJECT_VERSION:
                migration_source = deepcopy(loaded)
            project, migrated = director.migrate_project(loaded)
            if project.get("story_sha256") != story["source"]["narration_sha256"]:
                raise ValueError("Directors Room belongs to a different story revision.")
            # From schema v2 onward the project manuscript is the directing source
            # of truth. LAB_PATH remains a compatibility mirror, not an authority
            # that can erase a persistent Undo on the next GET.
            if project.get("manuscript"):
                lab = dict(lab)
                lab["text"] = project["manuscript"]
            chunks = (
                director.stable_region_chunks(project, lab["text"])
                if project.get("regions")
                else plan_lab_chunks(lab["text"], lab["target_chars"])
            )
            project = director.reconcile_project(project, lab, chunks)

        endpoint_status = higgs_status()
        if created:
            project = _legacy_take_import(project, endpoint_status)
            project = DIRECTOR_STORE.initialize(project)
        else:
            current_transactions = _current_transactions(project, endpoint_status)
            project, fingerprints_upgraded = director.upgrade_take_fingerprints(
                project, current_transactions
            )
            changed = project != loaded
            if migration_source is not None:
                project = DIRECTOR_STORE.initialize(
                    project, migration_source=migration_source
                )
            elif changed or fingerprints_upgraded or migrated:
                project = DIRECTOR_STORE.write_system(project, "reconcile")
            if store_warning:
                DIRECTOR_STORE.last_warning = store_warning
        return project, lab, chunks, endpoint_status


def _current_transactions(project: dict, endpoint_status: dict) -> dict[str, dict]:
    voices = {voice["id"]: voice for voice in available_voices()}
    transactions = {}
    for region in project["regions"]:
        settings = director.effective_settings(project, region)
        voice = voices.get(settings["voice_mode"])
        if voice is None:
            raise ValueError(f"Voice reference unavailable for {region['id']}.")
        transactions[region["id"]] = director.current_transaction(
            project, region, voice, HIGGS_ENDPOINT, endpoint_status
        )
    return transactions


def baseline_generation_config_fingerprint(project: dict) -> str:
    """Hash only inputs that can change a baseline Higgs transaction."""
    material = {
        "story_sha256": project.get("story_sha256"),
        "manuscript_sha256": director.sha256_text(project.get("manuscript", "")),
        "generation_defaults": project.get("generation_defaults"),
        "tracks": [
            {
                "id": track.get("id"),
                "voice_mode": track.get("voice_mode"),
                "delivery_defaults": track.get("delivery_defaults"),
            }
            for track in project.get("tracks", [])
        ],
        "regions": [
            {
                "id": region.get("id"),
                "source_text_sha256": region.get("source_text_sha256"),
                "track_id": region.get("track_id"),
                "overrides": region.get("overrides"),
            }
            for region in project.get("regions", [])
        ],
    }
    return director.sha256_json(material)


def activate_baseline_take_if_empty(
    project: dict,
    region: dict,
    take_id: str,
    expected_config_fingerprint: str,
) -> tuple[bool, str | None]:
    """Apply the baseline orchestration's only automatic editorial decision."""
    if region.get("active_take_id"):
        return False, None
    if (
        expected_config_fingerprint
        and expected_config_fingerprint != baseline_generation_config_fingerprint(project)
    ):
        return False, "generation_config_changed"
    region["active_take_id"] = take_id
    return True, None


def _timing_observations(project: dict, endpoint_status: dict) -> list[dict]:
    observations = []
    current_model = endpoint_status.get("model")
    for region in project.get("regions", []):
        for take in region.get("takes", []):
            transaction = take.get("transaction") or {}
            request = transaction.get("higgs_request") or {}
            try:
                duration = float(take.get("duration_seconds") or 0)
                elapsed = float(take.get("elapsed_seconds") or 0)
            except (TypeError, ValueError):
                continue
            text = _spoken_text(str(request.get("text") or ""))
            if not text or duration <= 0 or elapsed <= 0:
                continue
            if transaction.get("endpoint") not in {None, HIGGS_ENDPOINT}:
                continue
            if current_model and transaction.get("model") not in {None, current_model}:
                continue
            observations.append(
                {
                    "characters_per_audio_second": len(text) / duration,
                    "realtime_factor": elapsed / duration,
                }
            )
    return observations


def baseline_plan(project: dict, endpoint_status: dict) -> dict:
    """Plan missing-only synthesis and expose a deliberately honest ETA range."""
    targets = [region for region in project.get("regions", []) if not region.get("active_take_id")]
    target_characters = sum(len(_spoken_text(region.get("source_text", ""))) for region in targets)
    observations = _timing_observations(project, endpoint_status)
    sample_count = len(observations)
    if sample_count:
        characters_per_audio_second = statistics.median(
            item["characters_per_audio_second"] for item in observations
        )
        realtime_factor = statistics.median(item["realtime_factor"] for item in observations)
        expected_audio_seconds = target_characters / max(1.0, characters_per_audio_second)
        estimated_seconds = expected_audio_seconds * realtime_factor
        if sample_count >= 8:
            confidence, lower_factor, upper_factor = "measured", 0.88, 1.18
        elif sample_count >= 3:
            confidence, lower_factor, upper_factor = "preliminary", 0.82, 1.25
        else:
            confidence, lower_factor, upper_factor = "rough", 0.88, 1.30
        basis = "matching local Higgs takes"
    else:
        characters_per_audio_second = 14.0
        realtime_factor = 1.25
        expected_audio_seconds = target_characters / characters_per_audio_second
        estimated_seconds = expected_audio_seconds * realtime_factor
        confidence, lower_factor, upper_factor = "rough", 0.70, 1.60
        basis = "conservative fallback; no matching timing samples"
    return {
        "target_region_ids": [region["id"] for region in targets],
        "target_regions": len(targets),
        "existing_active_regions": len(project.get("regions", [])) - len(targets),
        "total_regions": len(project.get("regions", [])),
        "target_characters": target_characters,
        "expected_audio_seconds": round(expected_audio_seconds),
        "estimated_generation_seconds": round(estimated_seconds),
        "estimated_lower_seconds": round(estimated_seconds * lower_factor),
        "estimated_upper_seconds": round(estimated_seconds * upper_factor),
        "confidence": confidence,
        "timing_sample_count": sample_count,
        "basis": basis,
        "endpoint": HIGGS_ENDPOINT,
        "model": endpoint_status.get("model"),
        "model_loaded": bool(endpoint_status.get("model_loaded")),
    }


def director_state_payload() -> dict:
    project, lab, chunks, endpoint_status = load_director_project()
    transactions = _current_transactions(project, endpoint_status)
    annotated = director.annotate_freshness(project, transactions)
    workflow = director.workflow_state(annotated)
    active = sum(1 for region in annotated["regions"] if region["status"]["has_active_take"])
    approved = sum(1 for region in annotated["regions"] if region["status"]["approved"])
    fresh = sum(1 for region in annotated["regions"] if region["status"]["fresh"])
    return {
        "project": _safe_project_copy(annotated),
        "lab": lab,
        "chunks": chunks,
        "voices": available_voices(),
        "options": {
            "emotions": EMOTIONS,
            "styles": [value for value in STYLES if value],
            "speeds": [value for value in SPEEDS if value],
            "pitches": [value for value in PITCHES if value],
            "expressiveness": [value for value in EXPRESSIVENESS if value],
        },
        "engine": endpoint_status,
        "generation_activity": director_generation_activity(),
        "baseline": {
            "plan": baseline_plan(annotated, endpoint_status),
            "activity": baseline_activity(),
        },
        "qa_jobs": _qa_jobs_payload(),
        "qa_runtime": {
            "speaker_model": "microsoft/wavlm-base-plus-sv",
            "speaker_loaded": SPEAKER_QA_RUNTIME is not None,
            "asr_model": ASR_MODEL,
            "gates_are_post_generation": True,
        },
        "history": DIRECTOR_STORE.status(),
        "workflow": workflow,
        "state_warning": DIRECTOR_STORE.last_warning,
        "summary": {
            "regions": len(annotated["regions"]),
            "active": active,
            "approved": approved,
            "fresh": fresh,
            "stale": active - fresh,
            "generation_required": workflow["generation_required"],
            "qa_required": workflow["qa_required"],
            "render_outdated": workflow["render_outdated"],
        },
    }


def update_director_project(payload: dict) -> dict:
    with LAB_LOCK:
        return _update_director_project_locked(payload)


def _update_director_project_locked(payload: dict) -> dict:
    project, lab, _, _ = load_director_project()
    before = deepcopy(project)
    voices = {voice["id"] for voice in available_voices()}
    lab_changed = False
    if "manuscript" in payload or "target_chars" in payload:
        lab_payload = dict(lab)
        if "manuscript" in payload:
            lab_payload["text"] = str(payload["manuscript"])
        if "target_chars" in payload:
            lab_payload["target_chars"] = int(payload["target_chars"])
        story = load_json(STORY_PATH)
        lab = validate_lab(lab_payload, story)
        project = director.reconcile_manuscript(
            project,
            lab["text"],
            active_region_id=str(payload.get("active_region_id") or "") or None,
        )
        lab_changed = True

    generation = payload.get("generation_defaults")
    if generation is not None:
        candidate = dict(project["generation_defaults"])
        candidate.update(generation)
        candidate["temperature"] = float(candidate["temperature"])
        candidate["top_k"] = int(candidate["top_k"])
        candidate["seed"] = int(candidate["seed"])
        candidate["max_tokens"] = int(candidate["max_tokens"])
        if not 0.1 <= candidate["temperature"] <= 2.0:
            raise ValueError("Temperature must be between 0.1 and 2.0.")
        if not 1 <= candidate["top_k"] <= 200:
            raise ValueError("Top K must be between 1 and 200.")
        if not 0 <= candidate["seed"] <= 2_147_483_647:
            raise ValueError("Seed must be between 0 and 2147483647.")
        if not 256 <= candidate["max_tokens"] <= 4096:
            raise ValueError("Max frames must be between 256 and 4096.")
        project["generation_defaults"] = candidate

    qa_policy_patch = payload.get("qa_policy")
    if qa_policy_patch is not None:
        for gate in ("speaker_similarity", "asr_content"):
            if gate in qa_policy_patch:
                project.setdefault("qa_policy", {}).setdefault(gate, {})["enabled"] = bool(
                    (qa_policy_patch[gate] or {}).get("enabled", False)
                )

    track_patch = payload.get("track")
    if track_patch:
        track = next((item for item in project["tracks"] if item["id"] == track_patch.get("id")), None)
        if track is None:
            raise ValueError("Unknown voice track.")
        if "voice_mode" in track_patch:
            if track_patch["voice_mode"] not in voices:
                raise ValueError("The selected voice reference is unavailable.")
            track["voice_mode"] = track_patch["voice_mode"]
        if "output_gain_db" in track_patch:
            gain = float(track_patch["output_gain_db"])
            if not -60.0 <= gain <= 12.0:
                raise ValueError("Track gain must be between -60 and +12 dB.")
            track["output_gain_db"] = round(gain, 3)
        controls = track_patch.get("delivery_defaults")
        if controls is not None:
            valid = {
                "emotion": {"", *EMOTIONS},
                "style": set(STYLES),
                "speed": set(SPEEDS),
                "pitch": set(PITCHES),
                "expressiveness": set(EXPRESSIVENESS),
            }
            for key, value in controls.items():
                if key not in valid or value not in valid[key]:
                    raise ValueError(f"Unknown Higgs {key} token: {value}")
                track["delivery_defaults"][key] = value

    region_patch = payload.get("region")
    if region_patch:
        region = next((item for item in project["regions"] if item["id"] == region_patch.get("id")), None)
        if region is None:
            raise ValueError("Unknown region.")
        if "track_id" in region_patch:
            if region_patch["track_id"] not in {track["id"] for track in project["tracks"]}:
                raise ValueError("Unknown voice track.")
            region["track_id"] = region_patch["track_id"]
        if "pause_after_ms" in region_patch:
            pause = int(region_patch["pause_after_ms"])
            if not 0 <= pause <= 10_000:
                raise ValueError("Pause must be between 0 and 10000 ms.")
            region["pause_after_ms"] = pause
        continuity_context = region_patch.get("continuity_context") or region_patch.get("context")
        if continuity_context:
            for key in ("previous_regions", "next_regions"):
                if key in continuity_context:
                    value = int(continuity_context[key])
                    if not 0 <= value <= 3:
                        raise ValueError("Context range must be between 0 and 3 regions.")
                    region["continuity_context"][key] = value
        overrides = region_patch.get("overrides")
        if overrides:
            if "voice_mode" in overrides:
                value = overrides["voice_mode"]
                if value is not None and value not in voices:
                    raise ValueError("The selected voice reference is unavailable.")
                region["overrides"]["voice_mode"] = value
            for key, value in (overrides.get("controls") or {}).items():
                valid = {
                    "emotion": {"", *EMOTIONS},
                    "style": set(STYLES),
                    "speed": set(SPEEDS),
                    "pitch": set(PITCHES),
                    "expressiveness": set(EXPRESSIVENESS),
                }
                normalized = value or ""
                if key not in valid or normalized not in valid[key]:
                    raise ValueError(f"Unknown Higgs {key} token: {value}")
                region["overrides"]["controls"][key] = value or None
            for key, value in (overrides.get("generation") or {}).items():
                if key not in {"temperature", "top_k", "seed", "max_tokens"}:
                    raise ValueError(f"Unknown Higgs generation parameter: {key}")
                normalized = value if value not in {"", None} else None
                if normalized is not None:
                    if key == "temperature":
                        normalized = float(normalized)
                        if not 0.1 <= normalized <= 2.0:
                            raise ValueError("Temperature must be between 0.1 and 2.0.")
                    else:
                        normalized = int(normalized)
                        limits = {
                            "top_k": (1, 200),
                            "seed": (0, 2_147_483_647),
                            "max_tokens": (256, 4096),
                        }
                        minimum, maximum = limits[key]
                        if not minimum <= normalized <= maximum:
                            raise ValueError(f"{key} must be between {minimum} and {maximum}.")
                region["overrides"]["generation"][key] = normalized

    project["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if "manuscript" in payload:
        requested_action = str(payload.get("manuscript_action", "manuscript_edit"))
        if requested_action not in {
            "manuscript_edit",
            "inline_direction_add",
            "inline_direction_remove",
        }:
            raise ValueError("Unknown manuscript edit action.")
        action = requested_action
    elif "generation_defaults" in payload:
        action = "generation_defaults"
    elif "qa_policy" in payload:
        action = "qa_policy"
    elif payload.get("track"):
        action = (
            "track_output_gain"
            if "output_gain_db" in payload["track"]
            else "track_defaults"
        )
    elif set((payload.get("region") or {}).keys()) <= {"id", "pause_after_ms"}:
        action = "boundary_pause"
    else:
        action = "region_direction"
    with LAB_LOCK:
        project, _ = DIRECTOR_STORE.commit(before, project, action)
        if lab_changed:
            atomic_json(LAB_PATH, lab)
    return director_state_payload()


def generate_director_take(payload: dict) -> dict:
    with DIRECTOR_GENERATION_LOCK:
        return _generate_director_take_locked(payload)


def _generate_director_take_locked(payload: dict) -> dict:
    project, _, _, endpoint_status = load_director_project()
    if not endpoint_status.get("reachable"):
        raise RuntimeError(f"Higgs endpoint is unreachable: {HIGGS_ENDPOINT}")
    region = next((item for item in project["regions"] if item["id"] == payload.get("region_id")), None)
    if region is None:
        raise ValueError("Unknown region.")
    operation = str(payload.get("operation", "new_performance"))
    base_take = director.take_for(region, payload.get("base_take_id"))
    transactions = _current_transactions(project, endpoint_status)
    transaction = director.transaction_for_operation(transactions[region["id"]], operation, base_take)
    if operation == "baseline":
        transaction["orchestration"] = {
            "kind": "baseline_first_read",
            "job_id": str(payload.get("baseline_job_id") or ""),
            "activate_if_empty": bool(payload.get("activate_if_empty")),
        }
    if operation == "retry_exact":
        if transaction.get("endpoint") != HIGGS_ENDPOINT:
            raise ValueError("Retry exact requires the original Higgs endpoint.")
        if (
            transaction.get("model") != endpoint_status.get("model")
            or transaction.get("build") != endpoint_status.get("build")
        ):
            raise ValueError("Retry exact requires the original Higgs model/build.")
    request_payload = transaction["higgs_request"]
    request_id = uuid.uuid4().hex[:12]
    set_director_generation_activity(
        state="running",
        request_id=request_id,
        region_id=region["id"],
        operation=operation,
        stage="dispatched",
        cancellable=False,
        message="Higgs request dispatched. Safe cancellation is unavailable after dispatch.",
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    log_lab_generation(
        "director_take_started",
        request_id,
        region_id=region["id"],
        operation=operation,
        seed=request_payload.get("seed"),
        request_sha256=director.sha256_json(request_payload),
        continuity_context_sha256=director.sha256_json(transaction["continuity_context"]),
        voice_mode=request_payload.get("voice_mode"),
        endpoint=HIGGS_ENDPOINT,
        model=endpoint_status.get("model"),
        build=endpoint_status.get("build"),
    )
    started = time.perf_counter()
    try:
        result = post_json(f"{HIGGS_ENDPOINT}/api/generate", request_payload, timeout=900)
        set_director_generation_activity(
            state="running",
            request_id=request_id,
            region_id=region["id"],
            operation=operation,
            stage="downloading_audio",
            cancellable=False,
            message="Higgs finished synthesis; downloading and validating immutable audio.",
        )
        backend_confirmation = confirmed_backend_voice(
            result,
            request_payload["voice_mode"],
            transaction.get("reference"),
        )
        backend_audio = str(result.get("audio_url") or "")
        if not backend_audio.startswith("/outputs/"):
            raise RuntimeError("Higgs returned no usable audio artifact.")
        with urllib.request.urlopen(f"{HIGGS_ENDPOINT}{backend_audio}", timeout=30) as response:
            audio = response.read()
        if len(audio) < 44 or not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
            raise RuntimeError("Higgs returned an invalid WAV file.")
    except Exception as exc:
        set_director_generation_activity(
            state="failed",
            request_id=request_id,
            region_id=region["id"],
            operation=operation,
            stage="failed",
            cancellable=False,
            message=str(exc),
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        log_lab_generation(
            "director_take_failed",
            request_id,
            region_id=region["id"],
            operation=operation,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    TAKES_DIR.mkdir(parents=True, exist_ok=True)
    temporary = TAKES_DIR / f".{region['id']}-{request_id}.wav"
    audio_path = None
    auto_activated = False
    activation_blocked_reason = None
    try:
        temporary.write_bytes(audio)
        transaction["completed_endpoint_observation"] = {
            "endpoint": HIGGS_ENDPOINT,
            "model": result.get("model"),
            "build": endpoint_status.get("build", "unreported"),
            "backend": endpoint_status.get("backend"),
            "backend_confirmation": backend_confirmation,
        }
        with LAB_LOCK:
            latest_project, _, _, _ = load_director_project()
            latest_region = next(
                (item for item in latest_project["regions"] if item["id"] == region["id"]),
                None,
            )
            if latest_region is None:
                raise ValueError("The region disappeared while Higgs was generating.")
            take_id = director.next_take_id(latest_region)
            audio_path = TAKES_DIR / f"{region['id']}-{take_id}-{request_id}.wav"
            if audio_path.exists():
                raise RuntimeError(f"Immutable take path already exists: {audio_path.name}")
            temporary.replace(audio_path)
            selected_voice = next(
                voice for voice in available_voices() if voice["id"] == request_payload["voice_mode"]
            )
            policy_snapshot = qa_policy_snapshot(latest_project, hashlib.sha256(audio).hexdigest(), selected_voice)
            qa_evaluations = initial_qa_evaluations(policy_snapshot)
            take = {
                "id": take_id,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "audio_file": f"takes/{audio_path.name}",
                "audio_sha256": hashlib.sha256(audio).hexdigest(),
                "duration_seconds": result.get("duration_seconds"),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "transaction": transaction,
                "endpoint_response": {
                    "job_id": result.get("job_id"),
                    "model": result.get("model"),
                    "backend_confirmation": backend_confirmation,
                },
                "speaker_qa": {},
                "qa_policy_snapshot": policy_snapshot,
                "qa_evaluations": qa_evaluations,
                "approval_events": [],
                "human_judgement": {
                    "approved": False,
                    "approved_at": None,
                    "notes": "",
                },
                "edits": director.normalize_edits({}),
            }
            latest_region["takes"].append(take)
            if payload.get("activate_if_empty") and not latest_region.get("active_take_id"):
                auto_activated, activation_blocked_reason = activate_baseline_take_if_empty(
                    latest_project,
                    latest_region,
                    take_id,
                    str(payload.get("baseline_config_fingerprint") or ""),
                )
            latest_project["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            project = DIRECTOR_STORE.write_system(latest_project, "generate_take")
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        # A promoted file may already be referenced by authoritative project
        # truth if persistence failed during post-transition audit/index work.
        # Keep this possible orphan/durable asset; reconciliation is safer than
        # deleting immutable audio after an ambiguous commit boundary.
        set_director_generation_activity(
            state="failed", request_id=request_id, region_id=region["id"], operation=operation,
            stage="commit_failed", cancellable=False, message=str(exc),
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        log_lab_generation(
            "director_take_commit_failed", request_id, region_id=region["id"], operation=operation,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            error_type=type(exc).__name__, error=str(exc),
        )
        raise
    log_lab_generation(
        "director_take_completed",
        request_id,
        region_id=region["id"],
        take_id=take_id,
        elapsed_seconds=round(time.perf_counter() - started, 2),
        duration_seconds=take["duration_seconds"],
        audio_sha256=take["audio_sha256"],
        auto_activated=auto_activated,
        activation_blocked_reason=activation_blocked_reason,
        qa_policy_snapshot_sha256=director.sha256_json(policy_snapshot),
    )
    set_director_generation_activity(
        state="completed",
        request_id=request_id,
        region_id=region["id"],
        take_id=take_id,
        operation=operation,
        stage="completed",
        cancellable=False,
        message=(
            f"{region['id']} {take_id} stored and activated in its empty baseline slot."
            if auto_activated
            else f"{region['id']} {take_id} stored as an immutable take; activation unchanged."
        ),
        finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    start_qa_job(region["id"], take_id)
    return {
        "take": {**take, "audio_url": f"/takes/{audio_path.name}"},
        "auto_activated": auto_activated,
        "activation_blocked_reason": activation_blocked_reason,
        "state": director_state_payload(),
    }


def _baseline_worker(job_id: str, target_region_ids: list[str], config_fingerprint: str) -> None:
    set_baseline_activity(
        state="running",
        stage="generating",
        message="Baseline first read is generating missing regions in source order.",
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    log_lab_generation(
        "baseline_started",
        job_id,
        target_regions=len(target_region_ids),
        generation_config_fingerprint=config_fingerprint,
    )
    try:
        for region_id in target_region_ids:
            activity = baseline_activity()
            if activity.get("job_id") != job_id:
                raise RuntimeError("Baseline job ownership changed unexpectedly.")
            if activity.get("cancel_requested"):
                set_baseline_activity(
                    state="cancelled",
                    stage="cancelled",
                    current_region_id=None,
                    message="Stopped between regions. Completed takes were kept; resume targets only missing regions.",
                    finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
                log_lab_generation("baseline_cancelled", job_id, completed_regions=activity.get("completed_regions", 0))
                return

            project, _, _, _ = load_director_project()
            if baseline_generation_config_fingerprint(project) != config_fingerprint:
                raise RuntimeError(
                    "Source, voice, delivery, sampling, or track assignment changed. "
                    "The batch stopped before another Higgs request; start again to resume missing regions."
                )
            region = next((item for item in project["regions"] if item["id"] == region_id), None)
            if region is None:
                raise RuntimeError(f"Baseline target {region_id} disappeared.")
            if region.get("active_take_id"):
                set_baseline_activity(
                    skipped_regions=int(activity.get("skipped_regions", 0)) + 1,
                    processed_regions=int(activity.get("processed_regions", 0)) + 1,
                    current_region_id=None,
                    message=f"{region_id} already has an active take; preserving it.",
                )
                continue

            set_baseline_activity(
                current_region_id=region_id,
                stage="generating",
                message=f"Generating {region_id}. Cancel will stop before the next region.",
            )
            result = generate_director_take(
                {
                    "region_id": region_id,
                    "operation": "baseline",
                    "activate_if_empty": True,
                    "baseline_job_id": job_id,
                    "baseline_config_fingerprint": config_fingerprint,
                }
            )
            if result.get("activation_blocked_reason") == "generation_config_changed":
                raise RuntimeError(
                    f"{region_id} finished after generation settings changed. Its immutable take was kept "
                    "but not activated; start again to resume safely."
                )
            current = baseline_activity()
            generated = 1 if result.get("auto_activated") else 0
            skipped = 0 if generated else 1
            set_baseline_activity(
                completed_regions=int(current.get("completed_regions", 0)) + generated,
                skipped_regions=int(current.get("skipped_regions", 0)) + skipped,
                processed_regions=int(current.get("processed_regions", 0)) + 1,
                current_region_id=None,
                last_take_id=result["take"]["id"],
                message=(
                    f"{region_id} baseline take is active; QA is running asynchronously."
                    if generated
                    else f"{region_id} received another active choice; its baseline take was kept inactive."
                ),
            )

        set_baseline_activity(stage="rendering", message="All regions are active. Rendering the current EDL…")
        rendered = render_director_story({})
        final = baseline_activity()
        set_baseline_activity(
            state="completed",
            stage="completed",
            current_region_id=None,
            render_id=rendered["id"],
            render_url=rendered["audio_url"],
            render_duration_seconds=rendered.get("duration_seconds"),
            message="First read is ready. It was rendered without autoplay; all takes remain unapproved.",
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        log_lab_generation(
            "baseline_completed",
            job_id,
            completed_regions=final.get("completed_regions", 0),
            skipped_regions=final.get("skipped_regions", 0),
            render_id=rendered["id"],
        )
    except Exception as exc:
        set_baseline_activity(
            state="failed",
            stage="failed",
            current_region_id=None,
            message=str(exc),
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        log_lab_generation(
            "baseline_failed",
            job_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )


def start_baseline_first_read(payload: dict) -> dict:
    with BASELINE_ACTIVITY_LOCK:
        if BASELINE_ACTIVITY.get("state") in {"queued", "running", "cancelling"}:
            raise ValueError("A baseline first-read job is already running.")
        project, _, _, endpoint_status = load_director_project()
        if not endpoint_status.get("reachable"):
            raise RuntimeError(f"Higgs endpoint is unreachable: {HIGGS_ENDPOINT}")
        voices = {voice["id"]: voice for voice in available_voices()}
        voice_mode = str(payload.get("voice_mode") or "")
        if voice_mode not in voices:
            raise ValueError("Choose an available canonical voice reference.")

        plan = baseline_plan(project, endpoint_status)
        if not plan["target_region_ids"]:
            raise ValueError("Every region already has an active take; render the current EDL instead.")
        before = deepcopy(project)
        for track in project.get("tracks", []):
            track["voice_mode"] = voice_mode
        project, _ = DIRECTOR_STORE.commit(before, project, "baseline_voice_choice")
        config_fingerprint = baseline_generation_config_fingerprint(project)
        plan = baseline_plan(project, endpoint_status)
        job_id = f"baseline-{uuid.uuid4().hex[:12]}"
        set_baseline_activity(
            replace=True,
            state="queued",
            stage="queued",
            job_id=job_id,
            voice_mode=voice_mode,
            voice_label=voices[voice_mode]["label"],
            target_regions=plan["target_regions"],
            existing_active_regions=plan["existing_active_regions"],
            target_region_ids=plan["target_region_ids"],
            target_characters=plan["target_characters"],
            timing=plan,
            completed_regions=0,
            skipped_regions=0,
            processed_regions=0,
            current_region_id=None,
            cancel_requested=False,
            cancellable=True,
            generation_config_fingerprint=config_fingerprint,
            message="Baseline first read is queued.",
            queued_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        threading.Thread(
            target=_baseline_worker,
            args=(job_id, list(plan["target_region_ids"]), config_fingerprint),
            name=f"directors-room-{job_id}",
            daemon=True,
        ).start()
        return baseline_activity()


def cancel_baseline_first_read() -> dict:
    with BASELINE_ACTIVITY_LOCK:
        if BASELINE_ACTIVITY.get("state") not in {"queued", "running", "cancelling"}:
            raise ValueError("No baseline first-read job is running.")
        BASELINE_ACTIVITY.update(
            {
                "state": "cancelling",
                "cancel_requested": True,
                "message": "Stop requested. The current Higgs request will finish; no next region will start.",
            }
        )
        return deepcopy(BASELINE_ACTIVITY)


def update_take_decision(payload: dict) -> dict:
    with LAB_LOCK:
        return _update_take_decision_locked(payload)


def _update_take_decision_locked(payload: dict) -> dict:
    project, _, _, _ = load_director_project()
    before = deepcopy(project)
    region = next((item for item in project["regions"] if item["id"] == payload.get("region_id")), None)
    if region is None:
        raise ValueError("Unknown region.")
    take = director.take_for(region, payload.get("take_id"))
    if take is None:
        raise ValueError("Unknown take.")
    action = payload.get("action")
    if action == "activate":
        region["active_take_id"] = take["id"]
    elif action == "approve":
        approved = bool(payload.get("approved", True))
        qa_status = director.qa_status_for_take(project, take)
        override_reason = str(payload.get("override_reason") or "").strip()
        if approved and qa_status.get("required") and not override_reason:
            raise ValueError(
                "QA has not passed. Use an explicit approval override and record the reason."
            )
        take["human_judgement"]["approved"] = approved
        take["human_judgement"]["approved_at"] = (
            datetime.now().astimezone().isoformat(timespec="seconds") if approved else None
        )
        event = {
            "id": f"approval-{uuid.uuid4().hex[:12]}",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "approved": approved,
            "override_reason": override_reason or None,
            "qa_status": qa_status,
        }
        take.setdefault("approval_events", []).append(event)
    elif action == "note":
        take["human_judgement"]["notes"] = str(payload.get("notes", ""))[:1000]
    elif action == "edit":
        merged = {**director.normalize_edits(take.get("edits")), **(payload.get("edits") or {})}
        take["edits"] = director.normalize_edits(merged)
    elif action == "copy_edits":
        source = director.take_for(region, payload.get("from_take_id"))
        if source is None:
            raise ValueError("Copy edits needs an existing source take in this region.")
        take["edits"] = director.normalize_edits(source.get("edits"))
    else:
        raise ValueError("Unknown take decision.")
    project["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with LAB_LOCK:
        project, _ = DIRECTOR_STORE.commit(before, project, f"take_{action}")
    return director_state_payload()


def update_director_history(payload: dict) -> dict:
    action = str(payload.get("action", ""))
    if action not in {"undo", "redo"}:
        raise ValueError("History action must be undo or redo.")
    with LAB_LOCK:
        project, lab, _, _ = load_director_project()
        if action == "undo":
            project, changed = DIRECTOR_STORE.undo(project)
        else:
            project, changed = DIRECTOR_STORE.redo(project)
        if changed and project.get("manuscript") != lab.get("text"):
            lab = dict(lab)
            lab["text"] = project["manuscript"]
            atomic_json(LAB_PATH, lab)
    result = director_state_payload()
    result["history_changed"] = changed
    result["history_action"] = action
    return result


def immutable_take_audio(take: dict) -> Path:
    source = APP_DIR / str(take.get("audio_file") or "")
    if not source.is_file() or file_sha256(source) != take.get("audio_sha256"):
        raise ValueError(f"Immutable audio is missing or its hash changed for {take.get('id', 'unknown take')}.")
    return source


def waveform_payload(region_id: str, take_id: str, buckets: int = 900) -> dict:
    project = load_stored_project_offline()
    region = next((item for item in project["regions"] if item["id"] == region_id), None)
    take = director.take_for(region, take_id) if region else None
    if take is None:
        raise ValueError("Unknown waveform take.")
    audio_path = immutable_take_audio(take)
    buckets = max(120, min(2400, int(buckets)))
    cache_key = f"{take['audio_sha256']}-{buckets}"
    cache_path = WAVEFORM_DIR / f"{cache_key}.json"
    if cache_path.is_file():
        return load_json(cache_path)
    params, frames = director.read_pcm(audio_path)
    if params.sampwidth != 2:
        raise ValueError("Waveform preview currently requires PCM signed 16-bit WAV.")
    from array import array

    samples = array("h")
    samples.frombytes(frames)
    mono = [
        max(abs(samples[index + channel]) for channel in range(params.nchannels))
        for index in range(0, len(samples), params.nchannels)
    ]
    stride = max(1, (len(mono) + buckets - 1) // buckets)
    peaks = []
    for start in range(0, len(mono), stride):
        block = mono[start:start + stride]
        peak = round(max(block, default=0) / 32768.0, 4)
        peaks.append(peak)
    result = {
        "version": 1,
        "audio_sha256": take["audio_sha256"],
        "duration_ms": round(len(mono) * 1000 / params.framerate),
        "sample_rate": params.framerate,
        "samples_per_bucket": stride,
        "peaks": peaks,
    }
    atomic_json(cache_path, result)
    return result


def edited_take_preview(region_id: str, take_id: str) -> dict:
    project = load_stored_project_offline()
    region = next((item for item in project["regions"] if item["id"] == region_id), None)
    take = director.take_for(region, take_id) if region else None
    if take is None:
        raise ValueError("Unknown edited-preview take.")
    source = immutable_take_audio(take)
    track = next(item for item in project["tracks"] if item["id"] == region["track_id"])
    material = {
        "audio_sha256": take.get("audio_sha256"),
        "edits": director.render_edit_material(take.get("edits")),
        "track_gain_mdb": round(float(track.get("output_gain_db", 0.0)) * 1000),
    }
    fingerprint = director.sha256_json(material)
    output = WAVEFORM_DIR / f"edited-{fingerprint}.wav"
    if not output.is_file():
        params, frames = director.edited_pcm(
            source,
            take.get("edits"),
            float(track.get("output_gain_db", 0.0)),
        )
        director.write_wav(output, params, frames)
    return {"fingerprint": fingerprint, "audio_url": f"/waveforms/{output.name}"}


def render_director_story(payload: dict) -> dict:
    with RENDER_LOCK:
        project = load_stored_project_offline()
        edl, missing = director.current_edl(project)
        if missing:
            raise ValueError(
                f"Render needs active takes for all regions. Missing: {', '.join(missing[:8])}"
                + ("…" if len(missing) > 8 else "")
            )
        if not edl:
            raise ValueError("There are no active takes to render.")
        fingerprint = director.current_render_fingerprint(project)
        render_id = f"render-{fingerprint[:16]}"
        output = RENDER_DIR / f"{fingerprint}.wav"
        metadata_path = RENDER_DIR / f"{fingerprint}.json"
        tracks = {track["id"]: track for track in project["tracks"]}
        active_regions = []
        for region in project["regions"]:
            take = director.take_for(region, region.get("active_take_id"))
            if take is None:
                continue
            source = immutable_take_audio(take)
            track = tracks[region["track_id"]]
            active_regions.append((region, take, track, source))
        cached = False
        if output.is_file() and metadata_path.is_file():
            render = load_json(metadata_path)
            cached = render.get("audio_sha256") == file_sha256(output)
        if not cached:
            items = [
                (source, take.get("edits", {}), track.get("output_gain_db", 0.0))
                for region, take, track, source in active_regions
            ]
            pauses = [region["pause_after_ms"] for region, _, _, _ in active_regions[:-1]]
            audio_meta = director.concatenate_wavs(items, pauses, output)
            render = {
                "id": render_id,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "audio_file": f"renders/{output.name}",
                "edl": edl,
                "edl_sha256": director.sha256_json(edl),
                "render_fingerprint": fingerprint,
                "render_contract": "directors-room-render/v2",
                "renderer_build": "pcm-edl-v2",
                "partial": False,
                "missing_regions": [],
                **audio_meta,
            }
            atomic_json(metadata_path, render)
        with LAB_LOCK:
            latest = load_stored_project_offline()
            existing = next((item for item in latest["renders"] if item["id"] == render_id), None)
            if existing is None:
                latest["renders"].append(render)
                DIRECTOR_STORE.write_system(latest, "render_created")
        return {**render, "cached": cached, "audio_url": f"/renders/{output.name}"}


def export_preflight(payload: dict) -> dict:
    project = load_stored_project_offline()
    render_id = payload.get("render_id") or director.workflow_state(project).get("last_render_id")
    render = next((item for item in project.get("renders", []) if item["id"] == render_id), None)
    errors = []
    warnings = []
    current_fingerprint = director.current_render_fingerprint(project)
    for region in project["regions"]:
        take = director.take_for(region, region.get("active_take_id"))
        if take is None:
            errors.append({
                "code": "missing_active_take", "region_id": region["id"],
                "message": f"{region['id']} has no active take.",
            })
            continue
        try:
            immutable_take_audio(take)
        except ValueError as exc:
            errors.append({
                "code": "source_audio_invalid", "region_id": region["id"], "take_id": take["id"],
                "message": str(exc),
            })
    if render is None:
        errors.append({"code": "missing_render", "message": "Render the complete current EDL first."})
    else:
        source = APP_DIR / render["audio_file"]
        if render.get("partial") or render.get("render_fingerprint") != current_fingerprint:
            errors.append({"code": "stale_render", "message": "The selected render is not the complete current EDL."})
        elif not source.is_file() or file_sha256(source) != render.get("audio_sha256"):
            errors.append({"code": "render_hash_mismatch", "message": "The cached render is missing or corrupt."})
    voices = {voice["id"]: voice for voice in available_voices()}
    transactions = {}
    for region in project["regions"]:
        settings = director.effective_settings(project, region)
        active = director.take_for(region, region.get("active_take_id"))
        transaction = (active or {}).get("transaction") or {}
        status = {
            "model": transaction.get("model"),
            "build": transaction.get("build", "unreported"),
            "backend": transaction.get("backend"),
        }
        transactions[region["id"]] = director.current_transaction(
            project, region, voices[settings["voice_mode"]], HIGGS_ENDPOINT, status
        )
    annotated = director.annotate_freshness(project, transactions)
    for region in annotated["regions"]:
        take = director.take_for(region, region.get("active_take_id"))
        if take is None:
            continue
        if not take.get("approved"):
            warnings.append({"code": "unapproved_take", "region_id": region["id"], "take_id": take["id"]})
        if not take.get("fresh"):
            warnings.append({"code": "generation_stale", "region_id": region["id"], "take_id": take["id"]})
        qa_state = (take.get("qa_status") or {}).get("overall")
        if qa_state != "pass":
            warnings.append({"code": f"qa_{qa_state or 'missing'}", "region_id": region["id"], "take_id": take["id"]})
        approval_events = take.get("approval_events") or []
        latest_approval = approval_events[-1] if approval_events else {}
        if latest_approval.get("approved") and latest_approval.get("override_reason"):
            warnings.append({
                "code": "approval_override", "region_id": region["id"], "take_id": take["id"],
                "approval_event_id": latest_approval.get("id"),
            })
    material = {
        "render_id": render_id,
        "render_fingerprint": current_fingerprint,
        "errors": errors,
        "warnings": warnings,
    }
    return {**material, "preflight_fingerprint": director.sha256_json(material)}


def reconcile_export_revisions(project: dict) -> tuple[dict, list[str]]:
    """Recover complete on-disk revisions left past an ambiguous commit boundary."""
    export_root = EXPORT_DIR / project["project_id"]
    if not export_root.is_dir():
        return project, []
    known = {item.get("id") for item in project.get("export_revisions", [])}
    recovered = []
    for target in sorted(export_root.iterdir()):
        if not target.is_dir() or not re.fullmatch(r"r\d+", target.name) or target.name in known:
            continue
        try:
            manifest_path = target / "manifest.json"
            manifest = load_json(manifest_path)
            if (
                manifest.get("contract") != "directors-room-export/v1"
                or (manifest.get("project") or {}).get("id") != project["project_id"]
                or manifest.get("revision_id") != target.name
            ):
                continue
            artifacts = manifest.get("artifacts") or {}
            for kind, filename in (("wav", "master.wav"), ("mp3", "delivery.mp3")):
                artifact = artifacts.get(kind) or {}
                path = target / filename
                if artifact.get("file") != filename or not path.is_file() or file_sha256(path) != artifact.get("sha256"):
                    raise ValueError(f"Invalid recovered {kind} artifact.")
            revision = {
                "id": target.name,
                "created_at": manifest.get("created_at"),
                "label": manifest.get("label"),
                "notes": manifest.get("notes"),
                "render_id": manifest.get("render_id"),
                "render_fingerprint": manifest.get("render_fingerprint"),
                "export_fingerprint": manifest.get("export_fingerprint"),
                "preflight": manifest.get("preflight"),
                "artifacts": {
                    **artifacts,
                    "manifest": {"file": "manifest.json", "sha256": file_sha256(manifest_path)},
                },
                "recovered_from_disk": True,
            }
            project.setdefault("export_revisions", []).append(revision)
            known.add(target.name); recovered.append(target.name)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if recovered:
        project["export_revisions"].sort(
            key=lambda item: int(item["id"][1:]) if re.fullmatch(r"r\d+", item.get("id", "")) else -1
        )
        project["latest_export_revision_id"] = project["export_revisions"][-1]["id"]
    return project, recovered


def export_director_render(payload: dict) -> dict:
    with EXPORT_LOCK:
        preflight = export_preflight(payload)
        if payload.get("preflight_fingerprint") != preflight["preflight_fingerprint"]:
            raise ValueError("Export preflight is missing or stale; run it again.")
        if preflight["errors"]:
            raise ValueError(preflight["errors"][0]["message"])
        reason = str(payload.get("override_reason") or "").strip()
        if preflight["warnings"] and not reason:
            raise ValueError("Export warnings require a recorded override reason.")
        with LAB_LOCK:
            project = load_stored_project_offline()
            project, recovered = reconcile_export_revisions(project)
            if recovered:
                project = DIRECTOR_STORE.write_system(project, "export_revisions_recovered")
                atomic_json(
                    EXPORT_DIR / project["project_id"] / "latest.json",
                    {"revision_id": project["latest_export_revision_id"], "recovered": True},
                )
        render = next(item for item in project["renders"] if item["id"] == preflight["render_id"])
        if render.get("render_fingerprint") != director.current_render_fingerprint(project):
            raise ValueError("Project changed after preflight; run export preflight again.")
        label = str(payload.get("label") or "").strip()[:120]
        notes = str(payload.get("notes") or "").strip()[:2000]
        export_fingerprint = director.sha256_json(
            {
                "contract": "directors-room-export/v1",
                "render_fingerprint": render["render_fingerprint"],
                "profile": "mp3-mono-96k",
                "label": label,
                "notes": notes,
            }
        )
        if not payload.get("distinct_duplicate"):
            existing = next(
                (item for item in project.get("export_revisions", []) if item.get("export_fingerprint") == export_fingerprint),
                None,
            )
            if existing:
                return {**existing, "reused": True, "files": export_urls(project["project_id"], existing["id"])}
        recorded_numbers = [
            int(item["id"][1:])
            for item in project.get("export_revisions", [])
            if re.fullmatch(r"r\d+", item.get("id", ""))
        ]
        export_root = EXPORT_DIR / project["project_id"]
        disk_numbers = [
            int(path.name[1:])
            for path in export_root.iterdir()
            if path.is_dir() and re.fullmatch(r"r\d+", path.name)
        ] if export_root.is_dir() else []
        maximum = max(recorded_numbers + disk_numbers + [0])
        revision_id = f"r{maximum + 1:03d}"
        target = export_root / revision_id
        if target.exists():
            raise RuntimeError(f"Immutable export revision already exists: {revision_id}")
        export_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{revision_id}-", dir=export_root))
        try:
            wav_target = staging / "master.wav"
            mp3_target = staging / "delivery.mp3"
            shutil.copy2(APP_DIR / render["audio_file"], wav_target)
            command = [
                "/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav_target), "-map_metadata", "-1", "-map_chapters", "-1",
                "-ac", "1", "-ar", "24000", "-codec:a", "libmp3lame", "-b:a", "96k", str(mp3_target),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=1800)
            if completed.returncode != 0 or not mp3_target.is_file():
                raise RuntimeError(f"MP3 encoding failed: {completed.stderr.strip()[-500:]}")
            manifest_edl = []
            regions = {region["id"]: region for region in project["regions"]}
            for item in render["edl"]:
                region = regions[item["region_id"]]
                take = director.take_for(region, item["take_id"])
                qa_status = director.qa_status_for_take(project, take)
                approval_events = (take or {}).get("approval_events", [])
                judgement = (take or {}).get("human_judgement", {})
                manifest_edl.append(
                    {
                        "ordinal": item.get("ordinal"),
                        "region_id": item["region_id"],
                        "track_id": item.get("track_id"),
                        "take_id": item["take_id"],
                        "audio_sha256": item["audio_sha256"],
                        "edits": item["edits"],
                        "track_output_gain_mdb": item.get("track_output_gain_mdb", 0),
                        "pause_after_ms": item["pause_after_ms"],
                        "qa": {
                            "overall": qa_status.get("overall"),
                            "required": qa_status.get("required"),
                            "gates": qa_status.get("gates"),
                            "policy_snapshot_sha256": director.sha256_json(
                                (take or {}).get("qa_policy_snapshot") or {}
                            ),
                        },
                        "approval": {
                            "approved": bool(judgement.get("approved")),
                            "approved_at": judgement.get("approved_at"),
                            "latest_event_id": approval_events[-1].get("id") if approval_events else None,
                            "override_reason": approval_events[-1].get("override_reason") if approval_events else None,
                            "qa_status_at_approval": approval_events[-1].get("qa_status") if approval_events else None,
                        },
                    }
                )
            manifest = {
                "version": 1,
                "contract": "directors-room-export/v1",
                "project": {
                    "id": project["project_id"], "title": project["title"], "author": project["author"],
                    "story_sha256": project.get("story_sha256"), "manuscript_sha256": project.get("manuscript_sha256"),
                },
                "revision_id": revision_id,
                "label": label or None,
                "notes": notes or None,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "render_id": render["id"],
                "render_fingerprint": render["render_fingerprint"],
                "render_contract": render.get("render_contract"),
                "renderer_build": render.get("renderer_build"),
                "export_fingerprint": export_fingerprint,
                "preflight": {
                    "fingerprint": preflight["preflight_fingerprint"],
                    "warning_codes": [item["code"] for item in preflight["warnings"]],
                    "override_reason": reason or None,
                },
                "edl": manifest_edl,
                "artifacts": {
                    "wav": {"file": "master.wav", "sha256": file_sha256(wav_target)},
                    "mp3": {"file": "delivery.mp3", "sha256": file_sha256(mp3_target)},
                },
            }
            atomic_json(staging / "manifest.json", manifest)
            manifest_sha = file_sha256(staging / "manifest.json")
            staging.replace(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        revision = {
            "id": revision_id,
            "created_at": manifest["created_at"],
            "label": label or None,
            "notes": notes or None,
            "render_id": render["id"],
            "render_fingerprint": render["render_fingerprint"],
            "export_fingerprint": export_fingerprint,
            "preflight": manifest["preflight"],
            "artifacts": {**manifest["artifacts"], "manifest": {"file": "manifest.json", "sha256": manifest_sha}},
        }
        with LAB_LOCK:
            latest = load_stored_project_offline()
            latest.setdefault("export_revisions", []).append(revision)
            latest["latest_export_revision_id"] = revision_id
            DIRECTOR_STORE.write_system(latest, "export_created")
            atomic_json(export_root / "latest.json", {"revision_id": revision_id, "export_fingerprint": export_fingerprint})
        return {**revision, "reused": False, "files": export_urls(project["project_id"], revision_id)}


def export_urls(project_id: str, revision_id: str) -> dict:
    base = f"/exports/{project_id}/{revision_id}"
    return {"wav": f"{base}/master.wav", "mp3": f"{base}/delivery.mp3", "manifest": f"{base}/manifest.json"}


def export_lab(lab: dict) -> dict:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    chunks = plan_lab_chunks(lab["text"], lab["target_chars"])
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    story_slug = director.project_slug(load_json(STORY_PATH)["title"])
    text_path = EXPORT_DIR / f"{story_slug}.lab.higgs.txt"
    manifest_path = EXPORT_DIR / f"{story_slug}.lab.chunks.json"
    settings_path = EXPORT_DIR / f"{story_slug}.lab.settings.json"
    text_path.write_text(lab["text"].rstrip() + "\n", encoding="utf-8")
    atomic_json(
        manifest_path,
        {
            "version": 1,
            "generated_at": generated_at,
            "voice_mode": lab["voice_mode"],
            "controls": lab["controls"],
            "target_chars": lab["target_chars"],
            "chunks": [
                {
                    **chunk,
                    "text": lab["text"][chunk["start"]:chunk["end"]],
                }
                for chunk in chunks
            ],
        },
    )
    atomic_json(settings_path, {key: value for key, value in lab.items() if key != "text"})
    return {
        "generated_at": generated_at,
        "chunks": len(chunks),
        "files": {
            "text": f"/exports/{text_path.name}",
            "chunks": f"/exports/{manifest_path.name}",
            "settings": f"/exports/{settings_path.name}",
        },
    }


def state_payload() -> dict:
    story = load_json(STORY_PATH)
    annotations = validate_annotations(load_json(ANNOTATION_PATH), story)
    chunks = plan_chunks(story, annotations)
    return {
        "story": story,
        "annotations": annotations,
        "options": {"emotions": EMOTIONS, "expressiveness": EXPRESSIVENESS},
        "engine": higgs_status(),
        "plan": {
            "chunks": len(chunks),
            "largest_chunk": max((chunk["characters"] for chunk in chunks), default=0),
        },
    }


def export_files(story: dict, annotations: dict) -> dict:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    chunks = plan_chunks(story, annotations)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {
        "version": 3,
        "generated_at": generated_at,
        "story": {
            "title": story["title"],
            "author": story["author"],
            "language": story["language"],
            "narration_sha256": story["source"]["narration_sha256"],
        },
        "generation_defaults": {
            "model": "bosonai/higgs-tts-3-4b",
            "anchor": VOICE_REFERENCES.get(default_voice_mode(), {}).get("label", "unconfigured"),
            "temperature": 0.8,
            "seed": 42,
            "target_chars": annotations["target_chars"],
        },
        "chunks": chunks,
    }
    story_slug = director.project_slug(story["title"])
    files = {
        "canonical": EXPORT_DIR / f"{story_slug}.canonical.txt",
        "tts": EXPORT_DIR / f"{story_slug}.tts.txt",
        "annotated": EXPORT_DIR / f"{story_slug}.higgs.txt",
        "chunks": EXPORT_DIR / f"{story_slug}.chunks.json",
        "annotations": EXPORT_DIR / f"{story_slug}.annotations.json",
    }
    canonical_parts = []
    if annotations["include_title"]:
        byline = "От" if story.get("language") == "bg" else "By"
        canonical_parts.append(
            f"{story['title']}\n\n{byline} {story['author']}" if story.get("author") else story["title"]
        )
    canonical_parts.append("\n\n".join(paragraph["text"] for paragraph in story["paragraphs"]))
    files["canonical"].write_text("\n\n".join(canonical_parts) + "\n", encoding="utf-8")
    tts_parts = []
    if annotations["include_title"]:
        tts_parts.append(
            f"{story['title']}\n\n{byline} {story['author']}" if story.get("author") else story["title"]
        )
    tts_parts.append("\n\n".join(tts_text(paragraph["text"]) for paragraph in story["paragraphs"]))
    files["tts"].write_text("\n\n".join(tts_parts) + "\n", encoding="utf-8")
    files["annotated"].write_text("\n\n".join(chunk["prompted_text"] for chunk in chunks) + "\n", encoding="utf-8")
    atomic_json(files["chunks"], manifest)
    atomic_json(files["annotations"], annotations)
    return {
        "generated_at": generated_at,
        "chunks": len(chunks),
        "files": {name: f"/exports/{path.name}" for name, path in files.items()},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HiggsDirectorsRoom/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def send_file(self, path: Path, content_type: str) -> None:
        """Serve immutable/local audio with single-range support for browser scrubbing."""
        size = path.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        requested = self.headers.get("range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not match:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last:
                length = min(int(last), size)
                start, end = size - length, size - 1
            if start < 0 or start >= size or end < start:
                self.send_response(416)
                self.send_header("content-range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(length))
        self.send_header("accept-ranges", "bytes")
        self.send_header("cache-control", "no-store")
        if status == 206:
            self.send_header("content-range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                block = source.read(min(128 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def read_json(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        if length > 1_000_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/director/state":
            try:
                self.send_json(200, director_state_payload())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path == "/api/director/waveform":
            try:
                query = parse_qs(parsed.query)
                self.send_json(
                    200,
                    waveform_payload(
                        query.get("region_id", [""])[0],
                        query.get("take_id", [""])[0],
                        int(query.get("buckets", ["900"])[0]),
                    ),
                )
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path == "/api/director/edited-preview":
            try:
                query = parse_qs(parsed.query)
                self.send_json(
                    200,
                    edited_take_preview(
                        query.get("region_id", [""])[0],
                        query.get("take_id", [""])[0],
                    ),
                )
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path == "/api/lab/state":
            try:
                self.send_json(200, lab_state_payload())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path.startswith("/reference-audio/"):
            voice_id = Path(path).stem
            voice = VOICE_REFERENCES.get(voice_id)
            if voice is None or not voice["audio"].is_file():
                self.send_json(404, {"error": "Voice reference not found."})
                return
            content_type = mimetypes.guess_type(voice["audio"].name)[0] or "audio/wav"
            self.send_file(voice["audio"], content_type)
            return
        if path == "/api/state":
            try:
                self.send_json(200, state_payload())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if path.startswith(("/exports/", "/preview-audio/", "/takes/", "/renders/", "/waveforms/", "/static/")):
            roots = {
                "/exports/": EXPORT_DIR,
                "/preview-audio/": PREVIEW_DIR,
                "/takes/": TAKES_DIR,
                "/renders/": RENDER_DIR,
                "/waveforms/": WAVEFORM_DIR,
                "/static/": APP_DIR / "static",
            }
            prefix = next(value for value in roots if path.startswith(value))
            root = roots[prefix]
            candidate = (root / path[len(prefix):]).resolve()
            if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
                self.send_json(404, {"error": "Artifact not found."})
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or candidate.suffix == ".json":
                content_type += "; charset=utf-8"
            self.send_file(candidate, content_type)
            return
        if path in {"/", "/index.html"}:
            self.send_bytes(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        self.send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {
            "/api/save", "/api/export", "/api/preview",
            "/api/lab/save", "/api/lab/export", "/api/lab/preview",
            "/api/director/save", "/api/director/generate",
            "/api/director/take", "/api/director/history",
            "/api/director/render", "/api/director/export",
            "/api/director/export/preflight", "/api/director/qa",
            "/api/director/baseline/start", "/api/director/baseline/cancel",
        }:
            self.send_json(404, {"error": "Not found."})
            return
        try:
            payload = self.read_json()
            story = load_json(STORY_PATH)
            if path == "/api/director/save":
                self.send_json(200, update_director_project(payload))
                return
            if path == "/api/director/generate":
                self.send_json(200, generate_director_take(payload))
                return
            if path == "/api/director/baseline/start":
                self.send_json(202, start_baseline_first_read(payload))
                return
            if path == "/api/director/baseline/cancel":
                self.send_json(202, cancel_baseline_first_read())
                return
            if path == "/api/director/take":
                self.send_json(200, update_take_decision(payload))
                return
            if path == "/api/director/history":
                self.send_json(200, update_director_history(payload))
                return
            if path == "/api/director/render":
                self.send_json(200, render_director_story(payload))
                return
            if path == "/api/director/export/preflight":
                self.send_json(200, export_preflight(payload))
                return
            if path == "/api/director/export":
                self.send_json(200, export_director_render(payload))
                return
            if path == "/api/director/qa":
                self.send_json(202, request_director_qa(payload))
                return
            if path == "/api/lab/save":
                lab, chunks = save_lab(payload)
                self.send_json(
                    200,
                    {
                        "saved": True,
                        "lab": lab,
                        "chunks": chunks,
                        "largest_chunk": max(chunk["characters"] for chunk in chunks),
                    },
                )
                return
            if path == "/api/lab/preview":
                lab, _ = save_lab(payload.get("lab") or {})
                chunk_id = str(payload.get("chunk_id", ""))
                self.send_json(200, generate_lab_preview(lab, chunk_id))
                return
            if path == "/api/lab/export":
                lab, _ = save_lab(payload)
                self.send_json(200, export_lab(lab))
                return
            if path == "/api/preview":
                annotations = validate_annotations(load_json(ANNOTATION_PATH), story)
                utterance_id = str(payload.get("utterance_id", ""))
                variant = str(payload.get("variant", "base"))
                self.send_json(200, generate_preview(story, annotations, utterance_id, variant))
                return
            annotations = validate_annotations(payload, story)
            atomic_json(ANNOTATION_PATH, annotations)
            if path == "/api/save":
                chunks = plan_chunks(story, annotations)
                self.send_json(200, {"saved": True, "chunks": len(chunks), "largest_chunk": max(chunk["characters"] for chunk in chunks)})
                return
            if path == "/api/export":
                self.send_json(200, export_files(story, annotations))
                return
            self.send_json(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


def main() -> None:
    global HIGGS_ENDPOINT
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--higgs-endpoint",
        default=HIGGS_ENDPOINT,
        help="Concrete Higgs HTTP endpoint (or set HIGGS_ENDPOINT).",
    )
    args = parser.parse_args()
    HIGGS_ENDPOINT = args.higgs_endpoint.rstrip("/")
    if not STORY_PATH.exists() or not ANNOTATION_PATH.exists():
        raise SystemExit("Run bootstrap_project.py first.")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"TTS Directors Room: http://{args.host}:{args.port}", flush=True)
    print(f"Higgs endpoint: {HIGGS_ENDPOINT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
