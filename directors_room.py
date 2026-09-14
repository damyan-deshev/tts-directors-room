"""Higgs-native project, take provenance, freshness, and WAV edit decisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import unicodedata
import wave
from array import array
from copy import deepcopy
from datetime import datetime
from pathlib import Path


PROJECT_VERSION = 3
RENDER_CONTRACT_VERSION = 2
EDIT_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _track(track_id: str, label: str, color: str, lab: dict) -> dict:
    return {
        "id": track_id,
        "label": label,
        "color": color,
        "voice_mode": lab["voice_mode"],
        "delivery_defaults": deepcopy(lab["controls"]),
        "output_gain_db": 0.0,
    }


def default_tracks(lab: dict) -> list[dict]:
    return [
        _track("narrator", "Narrator", "#557aa8", lab),
        _track("dimitar", "Voice 1", "#4e8a61", lab),
        _track("monk", "Voice 2", "#795ca8", lab),
        _track("other", "Other voice", "#8b8174", lab),
    ]


def project_slug(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or f"tts-project-{sha256_text(title)[:10]}"


def new_project(story: dict, lab: dict, chunks: list[dict]) -> dict:
    project = {
        "version": PROJECT_VERSION,
        "project_id": project_slug(story["title"]),
        "title": story["title"],
        "author": story["author"],
        "story_sha256": story["source"]["narration_sha256"],
        "manuscript": lab["text"],
        "manuscript_sha256": sha256_text(lab["text"]),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "tracks": default_tracks(lab),
        "generation_defaults": {
            "temperature": lab["temperature"],
            "top_k": lab["top_k"],
            "seed": lab["seed"],
            "max_tokens": lab["max_tokens"],
        },
        "qa_policy": {
            "speaker_similarity": {"enabled": True},
            "asr_content": {"enabled": True},
        },
        "state_meta": {
            "revision": 0,
            "last_action": "created",
            "last_command_id": None,
        },
        "regions": [],
        "renders": [],
        "export_revisions": [],
        "latest_export_revision_id": None,
    }
    return reconcile_project(project, lab, chunks)


def _new_region(chunk: dict, text: str) -> dict:
    source_text = text[chunk["start"] : chunk["end"]]
    return {
        "id": chunk["id"],
        "index": chunk["index"],
        "start": chunk["start"],
        "end": chunk["end"],
        "source_text": source_text,
        "source_text_sha256": sha256_text(source_text),
        "track_id": "narrator",
        "boundary_after": chunk["boundary_after"],
        "overrides": {
            "voice_mode": None,
            "controls": {
                "emotion": None,
                "style": None,
                "speed": None,
                "pitch": None,
                "expressiveness": None,
            },
            "generation": {
                "temperature": None,
                "top_k": None,
                "seed": None,
                "max_tokens": None,
            },
        },
        "continuity_context": {
            "previous_regions": 1,
            "next_regions": 1,
        },
        "pause_after_ms": 700 if chunk["boundary_after"] == "paragraph" else 350,
        "active_take_id": None,
        "takes": [],
    }


def migrate_project(project: dict) -> tuple[dict, bool]:
    """Upgrade stored project structure without deleting user decisions/assets."""
    original = deepcopy(project)
    version = int(project.get("version", 0))
    if version > PROJECT_VERSION:
        raise ValueError(
            f"Project schema {version} is newer than supported schema {PROJECT_VERSION}."
        )
    project = deepcopy(project)
    project.setdefault(
        "qa_policy",
        {
            "speaker_similarity": {"enabled": True},
            "asr_content": {"enabled": True},
        },
    )
    project.setdefault("renders", [])
    project.setdefault("export_revisions", [])
    project.setdefault("latest_export_revision_id", None)
    for track in project.get("tracks", []):
        track.setdefault("output_gain_db", 0.0)
    for region in project.get("regions", []):
        for take in region.get("takes", []):
            take["edits"] = normalize_edits(take.get("edits"))
            take.setdefault("qa_policy_snapshot", None)
            take.setdefault("qa_evaluations", [])
            take.setdefault("approval_events", [])
    project.setdefault(
        "state_meta",
        {"revision": 0, "last_action": "migrated", "last_command_id": None},
    )
    project["version"] = PROJECT_VERSION
    changed = project != original
    if changed:
        project["updated_at"] = now_iso()
    return project, changed


def reconcile_project(project: dict, lab: dict, chunks: list[dict]) -> dict:
    """Preserve decisions while reflecting the current editable manuscript plan."""
    original = deepcopy(project)
    project, _ = migrate_project(project)
    project["manuscript"] = lab["text"]
    project["manuscript_sha256"] = sha256_text(lab["text"])
    project.setdefault("renders", [])
    project.setdefault("generation_defaults", {})
    for key, value in {
        "temperature": lab["temperature"],
        "top_k": lab["top_k"],
        "seed": lab["seed"],
        "max_tokens": lab["max_tokens"],
    }.items():
        project["generation_defaults"].setdefault(key, value)

    tracks = project.setdefault("tracks", [])
    if not tracks:
        tracks.extend(default_tracks(lab))
    narrator = next((track for track in tracks if track["id"] == "narrator"), None)
    if narrator:
        narrator.setdefault("voice_mode", lab["voice_mode"])
        narrator.setdefault("delivery_defaults", deepcopy(lab["controls"]))
    for track in tracks:
        track.setdefault("output_gain_db", 0.0)

    existing = {region["id"]: region for region in project.get("regions", [])}
    regions = []
    for chunk in chunks:
        region = deepcopy(existing.get(chunk["id"]) or _new_region(chunk, lab["text"]))
        source_text = lab["text"][chunk["start"] : chunk["end"]]
        region.update(
            {
                "id": chunk["id"],
                "index": chunk["index"],
                "start": chunk["start"],
                "end": chunk["end"],
                "source_text": source_text,
                "source_text_sha256": sha256_text(source_text),
                "boundary_after": chunk["boundary_after"],
            }
        )
        region.setdefault("track_id", "narrator")
        region.setdefault("pause_after_ms", 700 if chunk["boundary_after"] == "paragraph" else 350)
        region.setdefault("active_take_id", None)
        region.setdefault("takes", [])
        for take in region["takes"]:
            take["edits"] = normalize_edits(take.get("edits"))
            take.setdefault("qa_policy_snapshot", None)
            take.setdefault("qa_evaluations", [])
            take.setdefault("approval_events", [])
        if "continuity_context" not in region:
            region["continuity_context"] = region.pop(
                "context", {"previous_regions": 1, "next_regions": 1}
            )
        region.setdefault("overrides", _new_region(chunk, lab["text"])["overrides"])
        regions.append(region)
    project["regions"] = regions
    comparable_original = deepcopy(original)
    comparable_project = deepcopy(project)
    comparable_original.pop("updated_at", None)
    comparable_project.pop("updated_at", None)
    if comparable_project != comparable_original:
        project["updated_at"] = now_iso()
    else:
        project["updated_at"] = original.get("updated_at", project.get("updated_at", now_iso()))
    return project


def stable_region_chunks(project: dict, text: str) -> list[dict]:
    """Return the stored region plan after validating exact manuscript coverage."""
    regions = project.get("regions", [])
    if not regions:
        return []
    chunks: list[dict] = []
    cursor = 0
    identifiers: set[str] = set()
    for index, region in enumerate(regions, 1):
        identifier = str(region.get("id", ""))
        start = int(region.get("start", -1))
        end = int(region.get("end", -1))
        if not identifier or identifier in identifiers:
            raise ValueError("Stored region IDs must be non-empty and unique.")
        if start != cursor or end <= start or end > len(text):
            raise ValueError("Stored regions no longer cover the manuscript contiguously.")
        identifiers.add(identifier)
        chunks.append(
            {
                "id": identifier,
                "index": index,
                "start": start,
                "end": end,
                "boundary_after": region.get("boundary_after", "paragraph"),
            }
        )
        cursor = end
    if cursor != len(text):
        raise ValueError("Stored regions do not cover the complete manuscript.")
    return chunks


def _single_edit_span(old_text: str, new_text: str) -> tuple[int, int, int, int]:
    """Describe one browser edit by its exact unchanged prefix and suffix."""
    prefix = 0
    maximum_prefix = min(len(old_text), len(new_text))
    while prefix < maximum_prefix and old_text[prefix] == new_text[prefix]:
        prefix += 1
    suffix = 0
    maximum_suffix = min(len(old_text) - prefix, len(new_text) - prefix)
    while (
        suffix < maximum_suffix
        and old_text[len(old_text) - suffix - 1] == new_text[len(new_text) - suffix - 1]
    ):
        suffix += 1
    return prefix, len(old_text) - suffix, prefix, len(new_text) - suffix


def _mapped_boundary(
    change: tuple[int, int, int, int],
    offset: int,
    *,
    insertion_owner: str,
) -> int:
    """Map one old boundary through a local edit without fuzzy text matching."""
    old_start, old_end, new_start, new_end = change
    if old_start == old_end and offset == old_start:
        return new_end if insertion_owner == "left" else new_start
    if offset <= old_start:
        return offset
    if offset >= old_end:
        return new_end + (offset - old_end)
    return new_end if insertion_owner == "left" else new_start


def reconcile_manuscript(
    project: dict,
    text: str,
    *,
    active_region_id: str | None = None,
) -> dict:
    """Apply a text edit while preserving every existing region ID and asset."""
    result = deepcopy(project)
    old_text = str(result.get("manuscript", ""))
    if text == old_text:
        return result
    stable_region_chunks(result, old_text)
    regions = result["regions"]
    change = _single_edit_span(old_text, text)
    boundaries = [0]
    for index in range(1, len(regions)):
        old_boundary = int(regions[index]["start"])
        left_id = regions[index - 1]["id"]
        owner = "left" if active_region_id == left_id else "right"
        boundaries.append(
            _mapped_boundary(change, old_boundary, insertion_owner=owner)
        )
    boundaries.append(len(text))

    if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
        raise ValueError(
            "The edit would erase or merge a stable region. Rechunking is not enabled in V1."
        )

    for index, region in enumerate(regions):
        start, end = boundaries[index], boundaries[index + 1]
        source_text = text[start:end]
        if not source_text.strip():
            raise ValueError(
                f"The edit would leave {region['id']} empty. Rechunking is not enabled in V1."
            )
        region.update(
            {
                "index": index + 1,
                "start": start,
                "end": end,
                "source_text": source_text,
                "source_text_sha256": sha256_text(source_text),
            }
        )
    result["manuscript"] = text
    result["manuscript_sha256"] = sha256_text(text)
    result["updated_at"] = now_iso()
    return result


def track_for(project: dict, region: dict) -> dict:
    return next(track for track in project["tracks"] if track["id"] == region["track_id"])


def take_for(region: dict, take_id: str | None) -> dict | None:
    if not take_id:
        return None
    return next((take for take in region.get("takes", []) if take["id"] == take_id), None)


def effective_settings(project: dict, region: dict) -> dict:
    track = track_for(project, region)
    overrides = region["overrides"]
    controls = {
        key: overrides["controls"].get(key)
        if overrides["controls"].get(key) is not None
        else track["delivery_defaults"].get(key, "")
        for key in ("emotion", "style", "speed", "pitch", "expressiveness")
    }
    generation = {
        key: overrides["generation"].get(key)
        if overrides["generation"].get(key) is not None
        else project["generation_defaults"][key]
        for key in ("temperature", "top_k", "seed", "max_tokens")
    }
    return {
        "track_id": track["id"],
        "voice_mode": overrides.get("voice_mode") or track["voice_mode"],
        "controls": controls,
        "generation": generation,
    }


def continuity_snapshot(project: dict, region: dict) -> dict:
    index = project["regions"].index(region)
    previous_count = max(0, min(int(region["continuity_context"].get("previous_regions", 1)), 3))
    next_count = max(0, min(int(region["continuity_context"].get("next_regions", 1)), 3))
    previous = []
    for candidate in project["regions"][max(0, index - previous_count) : index]:
        active = take_for(candidate, candidate.get("active_take_id"))
        previous.append(
            {
                "region_id": candidate["id"],
                "source_text_sha256": candidate["source_text_sha256"],
                "active_take_id": active["id"] if active else None,
                "active_audio_sha256": active.get("audio_sha256") if active else None,
            }
        )
    following = [
        {
            "region_id": candidate["id"],
            "source_text_sha256": candidate["source_text_sha256"],
        }
        for candidate in project["regions"][index + 1 : index + 1 + next_count]
    ]
    return {
        "previous": previous,
        "next": following,
        "purpose": "browser_audition_and_diagnostics_only",
        "applied_to_higgs": False,
    }


def current_transaction(
    project: dict,
    region: dict,
    voice: dict,
    endpoint: str,
    endpoint_status: dict,
) -> dict:
    settings = effective_settings(project, region)
    prompt = " ".join(region["source_text"].split())
    request = {
        "text": prompt,
        "voice_mode": settings["voice_mode"],
        "controls": settings["controls"],
        "temperature": settings["generation"]["temperature"],
        "seed": settings["generation"]["seed"],
        "chunk_chars": 5000,
        "max_tokens": settings["generation"]["max_tokens"],
        "top_k": settings["generation"]["top_k"],
        "rolling_context": False,
    }
    continuity_context = continuity_snapshot(project, region)
    generation_material = {
        "source_text_sha256": region["source_text_sha256"],
        "higgs_request": request,
        "reference_audio_sha256": voice["audio_sha256"],
        "reference_text_sha256": voice["transcript_sha256"],
        "endpoint_model": endpoint_status.get("model"),
        "endpoint_build": endpoint_status.get("build", "unreported"),
    }
    legacy_freshness_material = {
        "source_text_sha256": region["source_text_sha256"],
        "higgs_request": request,
        "continuity_context": continuity_context,
        "reference_audio_sha256": voice["audio_sha256"],
        "reference_text_sha256": voice["transcript_sha256"],
        "endpoint_model": endpoint_status.get("model"),
    }
    generation_fingerprint = sha256_json(generation_material)
    return {
        "higgs_request": request,
        "conditioning": {
            "source": "immutable_canonical_reference_only",
            "neighbor_takes_applied": False,
        },
        "continuity_context": continuity_context,
        "reference": {
            "voice_mode": settings["voice_mode"],
            "label": voice["label"],
            "audio_sha256": voice["audio_sha256"],
            "transcript_sha256": voice["transcript_sha256"],
        },
        "endpoint": endpoint,
        "model": endpoint_status.get("model"),
        "build": endpoint_status.get("build", "unreported"),
        "backend": endpoint_status.get("backend"),
        "generation_fingerprint": generation_fingerprint,
        # Compatibility alias for v1 take readers. New freshness comparisons use
        # generation_fingerprint, which deliberately excludes audition context.
        "freshness_fingerprint": generation_fingerprint,
        "legacy_freshness_fingerprint": sha256_json(legacy_freshness_material),
    }


def transaction_for_operation(current: dict, operation: str, base_take: dict | None = None) -> dict:
    if operation == "retry_exact":
        if base_take is None:
            raise ValueError("Retry exact requires a base take.")
        if not base_take.get("transaction", {}).get("higgs_request", {}).get("text"):
            raise ValueError("This imported legacy take has no complete Higgs request to retry.")
        transaction = deepcopy(base_take["transaction"])
        transaction["operation"] = "retry_exact"
        transaction["based_on_take_id"] = base_take["id"]
        return transaction
    if operation == "baseline":
        transaction = deepcopy(current)
        transaction["operation"] = "baseline"
        transaction["generation_fingerprint"] = current["generation_fingerprint"]
        transaction["freshness_fingerprint"] = current["generation_fingerprint"]
        return transaction
    if operation not in {"new_performance", "variation"}:
        raise ValueError("Unknown Higgs generation operation.")
    transaction = deepcopy(current)
    transaction["operation"] = operation
    request = transaction["higgs_request"]
    old_seed = int(request["seed"])
    request["seed"] = secrets.randbelow(2_147_483_648)
    if operation == "variation":
        old_temperature = float(request["temperature"])
        old_top_k = int(request["top_k"])
        temperature_delta = 0.05 if secrets.randbelow(2) else -0.05
        top_k_delta = 5 if secrets.randbelow(2) else -5
        request["temperature"] = round(max(0.1, min(2.0, old_temperature + temperature_delta)), 2)
        request["top_k"] = max(1, min(200, old_top_k + top_k_delta))
        transaction["variation"] = {
            "seed_from": old_seed,
            "seed_to": request["seed"],
            "temperature_from": old_temperature,
            "temperature_to": request["temperature"],
            "top_k_from": old_top_k,
            "top_k_to": request["top_k"],
            "base_generation_fingerprint": current["generation_fingerprint"],
        }
    # A new seed is a new performance, not a stale dependency. A deliberate
    # variation also remains fresh relative to the baseline it varied from;
    # the exact changed values remain in the immutable Higgs request.
    transaction["generation_fingerprint"] = current["generation_fingerprint"]
    transaction["freshness_fingerprint"] = current["generation_fingerprint"]
    return transaction


def upgrade_take_fingerprints(
    project: dict, current_transactions: dict[str, dict]
) -> tuple[dict, bool]:
    """Preserve v1 fresh/stale judgement while removing continuity dependency."""
    result = deepcopy(project)
    changed = False
    for region in result.get("regions", []):
        current = current_transactions[region["id"]]
        for take in region.get("takes", []):
            transaction = take.get("transaction") or {}
            if transaction.get("generation_fingerprint"):
                continue
            old = transaction.get("freshness_fingerprint")
            request = transaction.get("higgs_request") or {}
            current_request = current.get("higgs_request") or {}
            variation = transaction.get("variation") or {}
            legacy_request_matches = all(
                request.get(key) == current_request.get(key)
                for key in (
                    "text",
                    "voice_mode",
                    "controls",
                    "chunk_chars",
                    "max_tokens",
                    "rolling_context",
                )
            )
            legacy_request_matches = legacy_request_matches and (
                variation.get("temperature_from", request.get("temperature"))
                == current_request.get("temperature")
                and variation.get("top_k_from", request.get("top_k"))
                == current_request.get("top_k")
            )
            reference = transaction.get("reference") or {}
            current_reference = current.get("reference") or {}
            legacy_reference_matches = all(
                reference.get(key) == current_reference.get(key)
                for key in ("voice_mode", "audio_sha256", "transcript_sha256")
            )
            if (
                old
                and (
                    old == current.get("legacy_freshness_fingerprint")
                    or (legacy_request_matches and legacy_reference_matches)
                )
            ):
                fingerprint = current["generation_fingerprint"]
            else:
                fingerprint = f"legacy-stale:{old or 'unknown'}"
            transaction["generation_fingerprint"] = fingerprint
            take["transaction"] = transaction
            changed = True
    if changed:
        result["updated_at"] = now_iso()
    return result, changed


def _qa_gate_enabled(project: dict, gate: str) -> bool:
    value = (project.get("qa_policy") or {}).get(gate, {"enabled": True})
    return bool(value.get("enabled", True)) if isinstance(value, dict) else bool(value)


def qa_status_for_take(project: dict, take: dict | None) -> dict:
    if take is None:
        return {"required": False, "overall": "not_applicable", "gates": {}}
    evaluations = take.get("qa_evaluations") or []
    snapshot = take.get("qa_policy_snapshot") or {}
    if evaluations and snapshot:
        gates = {}
        for gate in ("speaker_similarity", "asr_content"):
            gate_policy = (snapshot.get("gates") or {}).get(gate, {})
            enabled = bool(gate_policy.get("enabled", False))
            latest = next(
                (item for item in reversed(evaluations) if item.get("gate") == gate),
                None,
            )
            gates[gate] = {
                "state": (latest or {}).get("state", "pending" if enabled else "skipped"),
                "evaluation_id": (latest or {}).get("id"),
                "enabled": enabled,
            }
        enabled_states = [item["state"] for item in gates.values() if item["enabled"]]
        required = any(state != "pass" for state in enabled_states)
        if not enabled_states:
            overall = "skipped"
        elif required:
            overall = next(
                (
                    state
                    for state in ("error", "fail", "abstain", "pending", "missing")
                    if state in enabled_states
                ),
                "required",
            )
        else:
            overall = "pass"
        return {"required": required, "overall": overall, "gates": gates}
    gates: dict[str, dict] = {}
    speaker_enabled = _qa_gate_enabled(project, "speaker_similarity")
    speaker = take.get("speaker_qa") or {}
    if not speaker_enabled:
        gates["speaker_similarity"] = {"state": "skipped"}
    elif speaker.get("available"):
        gates["speaker_similarity"] = {
            "state": "fail" if speaker.get("speaker_anomaly") else "pass"
        }
    elif speaker:
        gates["speaker_similarity"] = {"state": "error"}
    else:
        gates["speaker_similarity"] = {"state": "missing"}

    asr_enabled = _qa_gate_enabled(project, "asr_content")
    asr = take.get("asr_qa") or take.get("content_qa") or {}
    if not asr_enabled:
        gates["asr_content"] = {"state": "skipped"}
    elif asr.get("state"):
        gates["asr_content"] = {"state": asr["state"]}
    elif asr.get("available"):
        gates["asr_content"] = {
            "state": "pass" if asr.get("content_acceptable") else "fail"
        }
    elif asr:
        gates["asr_content"] = {"state": "error"}
    else:
        gates["asr_content"] = {"state": "missing"}

    enabled_states = [
        gate["state"]
        for name, gate in gates.items()
        if _qa_gate_enabled(project, name)
    ]
    required = any(state != "pass" for state in enabled_states)
    if not enabled_states:
        overall = "skipped"
    elif required:
        overall = next(
            (state for state in ("error", "fail", "abstain", "pending", "missing") if state in enabled_states),
            "required",
        )
    else:
        overall = "pass"
    return {"required": required, "overall": overall, "gates": gates}


def annotate_freshness(project: dict, current_transactions: dict[str, dict]) -> dict:
    result = deepcopy(project)
    for region in result["regions"]:
        current = current_transactions[region["id"]]
        for take in region["takes"]:
            take["fresh"] = (
                take.get("transaction", {}).get("generation_fingerprint")
                == current["generation_fingerprint"]
            )
            judgement = take.setdefault("human_judgement", {})
            judgement.setdefault("approved", False)
            take["approved"] = bool(judgement["approved"])
            take["qa_status"] = qa_status_for_take(result, take)
        active = take_for(region, region.get("active_take_id"))
        region["status"] = {
            "has_active_take": active is not None,
            "approved": bool(active and active.get("approved")),
            "fresh": bool(active and active.get("fresh")),
            "generation_required": active is None or not bool(active.get("fresh")),
            "qa_required": bool(active and active.get("qa_status", {}).get("required")),
        }
    return result


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value or 0)))


def _bounded_gain(value: object) -> float:
    return round(max(-60.0, min(12.0, float(value or 0.0))), 3)


def normalize_edits(edits: dict | None) -> dict:
    """Return the canonical, source-timeline edit contract for one immutable take."""
    edits = edits or {}
    envelope_by_ms: dict[int, dict] = {}
    for item in list(edits.get("volume_envelope") or edits.get("envelope") or [])[:128]:
        at_ms = _bounded_int(item.get("at_ms"), 0, 86_400_000)
        envelope_by_ms[at_ms] = {
            "id": str(item.get("id") or f"env-{at_ms}"),
            "at_ms": at_ms,
            "gain_db": _bounded_gain(item.get("gain_db")),
        }
    pauses = []
    for index, item in enumerate(list(edits.get("inserted_pauses") or [])[:50]):
        at_ms = _bounded_int(item.get("at_ms"), 0, 86_400_000)
        pauses.append(
            {
                "id": str(item.get("id") or f"pause-{at_ms}-{index + 1}"),
                "at_ms": at_ms,
                "duration_ms": _bounded_int(item.get("duration_ms"), 0, 10_000),
            }
        )
    return {
        "version": EDIT_VERSION,
        "trim_start_ms": _bounded_int(edits.get("trim_start_ms"), 0, 86_400_000),
        "trim_end_ms": _bounded_int(edits.get("trim_end_ms"), 0, 86_400_000),
        "fade_in_ms": _bounded_int(edits.get("fade_in_ms"), 0, 60_000),
        "fade_out_ms": _bounded_int(edits.get("fade_out_ms"), 0, 60_000),
        "clip_gain_db": _bounded_gain(edits.get("clip_gain_db")),
        "volume_envelope": [envelope_by_ms[key] for key in sorted(envelope_by_ms)],
        "inserted_pauses": sorted(pauses, key=lambda item: (item["at_ms"], item["id"])),
    }


def render_edit_material(edits: dict | None) -> dict:
    """Canonical sound-affecting material; UI marker IDs are deliberately absent."""
    normalized = normalize_edits(edits)
    return {
        "version": normalized["version"],
        "trim_start_ms": normalized["trim_start_ms"],
        "trim_end_ms": normalized["trim_end_ms"],
        "fade_in_ms": normalized["fade_in_ms"],
        "fade_out_ms": normalized["fade_out_ms"],
        "clip_gain_mdb": round(normalized["clip_gain_db"] * 1000),
        "volume_envelope": [
            {"at_ms": point["at_ms"], "gain_mdb": round(point["gain_db"] * 1000)}
            for point in normalized["volume_envelope"]
        ],
        "inserted_pauses": [
            {"at_ms": point["at_ms"], "duration_ms": point["duration_ms"]}
            for point in normalized["inserted_pauses"]
        ],
    }


def current_edl(project: dict) -> tuple[list[dict], list[str]]:
    edl = []
    missing = []
    tracks = {track["id"]: track for track in project.get("tracks", [])}
    for region in project.get("regions", []):
        take = take_for(region, region.get("active_take_id"))
        if take is None:
            missing.append(region["id"])
            continue
        track = tracks.get(region.get("track_id"), {})
        edl.append(
            {
                "ordinal": len(edl) + 1,
                "region_id": region["id"],
                "track_id": region.get("track_id", "narrator"),
                "take_id": take["id"],
                "audio_sha256": take.get("audio_sha256"),
                "edits": render_edit_material(take.get("edits")),
                "pause_after_ms": int(region.get("pause_after_ms", 0)),
                "track_output_gain_mdb": round(float(track.get("output_gain_db", 0.0)) * 1000),
            }
        )
    if edl and project.get("regions") and edl[-1]["region_id"] == project["regions"][-1]["id"]:
        edl[-1]["pause_after_ms"] = 0
    return edl, missing


def current_render_fingerprint(project: dict) -> str:
    edl, missing = current_edl(project)
    return sha256_json(
        {
            "render_contract": f"directors-room-render/v{RENDER_CONTRACT_VERSION}",
            "renderer_build": "pcm-edl-v2",
            "target": {"codec": "pcm_s16le", "sample_rate": 24_000, "channels": 1},
            "edl": edl,
            "missing_regions": missing,
        }
    )


def workflow_state(project: dict) -> dict:
    generation_required = sum(
        bool(region.get("status", {}).get("generation_required"))
        for region in project.get("regions", [])
    )
    qa_required = sum(
        bool(region.get("status", {}).get("qa_required"))
        for region in project.get("regions", [])
    )
    fingerprint = current_render_fingerprint(project)
    matching = next(
        (
            render
            for render in reversed(project.get("renders", []))
            if render.get("render_fingerprint") == fingerprint
        ),
        None,
    )
    render_outdated = matching is None
    return {
        "generation_required": generation_required,
        "qa_required": qa_required,
        "render_outdated": render_outdated,
        "render_fingerprint": fingerprint,
        "last_render_id": matching.get("id") if matching else None,
    }


def next_take_id(region: dict) -> str:
    highest = 0
    for take in region.get("takes", []):
        try:
            highest = max(highest, int(take["id"].split("_")[-1]))
        except (ValueError, KeyError):
            continue
    return f"take_{highest + 1:03d}"


def read_pcm(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        if params.comptype != "NONE":
            raise ValueError(f"Compressed WAV is not supported: {path.name}")
        return params, handle.readframes(handle.getnframes())


def frame_for_ms(sample_rate: int, milliseconds: int) -> int:
    return (int(sample_rate) * max(0, int(milliseconds)) + 500) // 1000


def _envelope_gain_db(points: list[dict], source_ms: float) -> float:
    if not points:
        return 0.0
    if source_ms <= points[0]["at_ms"]:
        return float(points[0]["gain_db"])
    if source_ms >= points[-1]["at_ms"]:
        return float(points[-1]["gain_db"])
    for left, right in zip(points, points[1:]):
        if left["at_ms"] <= source_ms <= right["at_ms"]:
            span = right["at_ms"] - left["at_ms"]
            amount = 0.0 if span == 0 else (source_ms - left["at_ms"]) / span
            return float(left["gain_db"]) + amount * (
                float(right["gain_db"]) - float(left["gain_db"])
            )
    return 0.0


def edited_pcm(
    path: Path,
    edits: dict | None = None,
    track_gain_db: float = 0.0,
) -> tuple[wave._wave_params, bytes]:
    params, frames = read_pcm(path)
    edits = normalize_edits(edits)
    frame_width = params.nchannels * params.sampwidth
    total = len(frames) // frame_width
    start = min(total, frame_for_ms(params.framerate, edits["trim_start_ms"]))
    trim_end = frame_for_ms(params.framerate, edits["trim_end_ms"])
    end = max(start, total - min(total, trim_end))
    if end <= start:
        raise ValueError(f"Edits fully trim immutable take {path.name}.")
    frames = frames[start * frame_width : end * frame_width]

    if params.sampwidth != 2:
        if any(
            [edits["clip_gain_db"], track_gain_db, edits["fade_in_ms"], edits["fade_out_ms"], edits["volume_envelope"]]
        ):
            raise ValueError("V1 deterministic gain/fade DSP requires PCM signed 16-bit WAV.")
    else:
        samples = array("h")
        samples.frombytes(frames)
        retained_frames = end - start
        fade_in_frames = frame_for_ms(params.framerate, edits["fade_in_ms"])
        fade_out_frames = frame_for_ms(params.framerate, edits["fade_out_ms"])
        base_gain_db = float(edits["clip_gain_db"]) + float(track_gain_db)
        points = edits["volume_envelope"]
        for frame_index in range(retained_frames):
            source_frame = start + frame_index
            source_ms = source_frame * 1000.0 / params.framerate
            gain_db = base_gain_db + _envelope_gain_db(points, source_ms)
            gain = math.pow(10.0, gain_db / 20.0)
            if fade_in_frames:
                gain *= min(1.0, frame_index / max(1, fade_in_frames))
            if fade_out_frames:
                gain *= min(1.0, (retained_frames - 1 - frame_index) / max(1, fade_out_frames))
            for channel in range(params.nchannels):
                sample_index = frame_index * params.nchannels + channel
                value = round(samples[sample_index] * gain)
                samples[sample_index] = max(-32768, min(32767, value))
        frames = samples.tobytes()

    pauses = edits["inserted_pauses"]
    if not pauses:
        return params, frames
    pieces: list[bytes] = []
    cursor = 0
    edited_total = len(frames) // frame_width
    for pause in pauses:
        source_at = frame_for_ms(params.framerate, pause["at_ms"])
        if source_at < start or source_at > end:
            continue
        at = source_at - start
        pieces.append(frames[cursor * frame_width : at * frame_width])
        silence_frames = frame_for_ms(params.framerate, pause["duration_ms"])
        pieces.append(b"\0" * silence_frames * frame_width)
        cursor = at
    pieces.append(frames[cursor * frame_width :])
    return params, b"".join(pieces)


def write_wav(path: Path, params: wave._wave_params, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(temporary), "wb") as handle:
        handle.setparams(params)
        handle.writeframes(frames)
    temporary.replace(path)


def concatenate_wavs(items: list[tuple], pauses_ms: list[int], output: Path) -> dict:
    if not items:
        raise ValueError("The edit decision list has no active takes.")
    output_params = None
    parts: list[bytes] = []
    for index, item in enumerate(items):
        path, edits = item[:2]
        track_gain_db = float(item[2]) if len(item) > 2 else 0.0
        params, frames = edited_pcm(path, edits, track_gain_db)
        signature = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
        if output_params is None:
            output_params = params
            expected = signature
        elif signature != expected:
            raise ValueError("Active takes do not share one PCM format.")
        parts.append(frames)
        if index < len(items) - 1:
            pause_ms = max(0, int(pauses_ms[index]))
            parts.append(b"\0" * frame_for_ms(params.framerate, pause_ms) * params.nchannels * params.sampwidth)
    combined = b"".join(parts)
    write_wav(output, output_params, combined)
    duration = len(combined) / (output_params.framerate * output_params.nchannels * output_params.sampwidth)
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate": output_params.framerate,
        "channels": output_params.nchannels,
        "sample_width": output_params.sampwidth,
        "audio_sha256": sha256_bytes(output.read_bytes()),
    }
