# TTS Directors Room codebase map

Target product decisions live in [`product-ledger.md`](product-ledger.md). This
map describes the current V1 implementation.

## System shape

The browser is the directing surface. The local Python service owns project state,
Higgs transactions, immutable takes, rendering, private reference routing, and the
audit trail. The Higgs endpoint is a separate resident process and remains a gray
box behind its concrete HTTP contract.

## Interfaces I should own

- Project document: manuscript, ordered regions, track defaults, pauses, active
  takes, approval, and freshness.
- Higgs transaction: exact text, immutable canonical-reference fingerprints,
  inline delivery instructions, supported generation parameters, model/build,
  and output hashes.
- Generation semantics: retry exact, new performance, and variation.
- Edit decision list: one active take per region plus explicit inter-region pauses.
- Render/export semantics: render assembles the EDL; export packages an existing
  render and provenance manifest.

## Gray boxes I can delegate

- Higgs model loading and synthesis behind `HIGGS_ENDPOINT`.
- Speaker similarity/ASR runtime work, provided results and failure states remain
  visible and take-snapshotted.
- WAV concatenation and silence insertion, provided the EDL and output hash are
  recorded.
- Waveform peak extraction/rendering implementation; immutable audio and its hash
  remain truth.

## Behavior contracts

- A generation never overwrites an existing take.
- Approval and freshness are independent.
- Changing text, inherited voice, or applied Higgs delivery makes a take stale,
  but never deletes or deactivates it automatically.
- Activating a take changes the EDL, not its approval judgement.
- Neighbor take hashes/audio are audition metadata only. They never affect
  generation freshness and are never sent to Higgs; every generation conditions
  exclusively on the selected immutable canonical voice reference.
- Unsupported controls are not presented as reliable synthesis parameters.
- The backend stays resident; Directors Room does not unload it after generation.
- Project data and voice/reference material are not tracked by Git.

## Current implementation boundaries

- Project state is schema-versioned and atomically snapshotted. Persistent
  command history supplies reload-safe Undo/Redo.
- Region IDs are durable across ordinary manuscript and inline-token edits.
- Takes are append-only files with explicit activation, approval, QA snapshots,
  provenance, and take-owned edit decisions.
- Rendering is deterministic and cache-keyed; exporting packages an existing
  render into an immutable numbered revision.
- The legacy lab/state routes remain as a local migration adapter. The current
  browser UI uses only the `/api/director/*` contract.
