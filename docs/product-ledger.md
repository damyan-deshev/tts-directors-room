# TTS Directors Room — product decision ledger

Updated: 2026-09-15

This ledger is the authoritative product contract for V1. Implementation slices
may refine technical details, but they must not silently change these decisions.
If a slice discovers a contradiction, it stops at the smallest reversible point
and records the proposed ledger amendment before proceeding.

## Product definition

TTS Directors Room is a Higgs-native non-linear performance editor. It directs,
auditions, selects, non-destructively edits, renders, and exports spoken
performances. It is not a generic TTS abstraction and it is not a general-purpose
DAW.

The V1 proving project is private. Story text, private voice material, generated
audio, and local endpoint details remain outside Git.

## Non-negotiable invariants

1. The manuscript defines a locked narrative sequence. Regions cannot be moved
   left or right, overlapped, or made simultaneous in V1.
2. A generated take is immutable. Ordinary generation never overwrites, deletes,
   or automatically activates a take. The explicit baseline-first orchestration
   may activate its new take only when that region is still empty.
3. Every Higgs request conditions only on the selected immutable canonical voice
   reference. Generated neighboring takes are never conditioning audio.
4. The Higgs endpoint is configurable independently of the application. The
   application never unloads or cycles a resident model after generation.
5. `Generate`, `Activate`, `Render`, and `Export` are different operations:
   generation creates a performance; activation changes the EDL; render assembles
   the EDL; export packages an existing result.
6. Higgs direction and deterministic audio processing are separate domains.
   Audio processing never pretends to be a model instruction, and a model
   instruction never pretends to be immediately audible without regeneration.
7. No material processing or monitoring effect is invisible in the UI.

## Decisions

### D-001 — Higgs is the native engine

Store the exact Higgs generation transaction for each take: source text,
canonical reference fingerprints, track defaults, inline delivery instructions,
sampling parameters, seed, endpoint/model/build observation, and output hash.
There is no universal `engine parameters` translation layer in V1.

### D-002 — Endpoint and model lifetime

The Higgs base URL is runtime configuration, not baked into project data. Local
Mac Higgs is the V1 test target; changing the URL may point Directors Room at a
different resident Higgs service. Directors Room may start the configured local
service when absent, but never unloads it after a request.

### D-003 — Canonical reference boundary

Every generation uses the original canonical reference audio selected by the
speaker track. Previous and next generated takes may be auditioned in the browser
but never enter Higgs conditioning. This prevents cumulative speaker/prosody
drift.

### D-004 — Locked narrative timeline

The timeline visualizes duration and sequence but does not permit reordering,
overlap, or arbitrary horizontal placement. A boundary pause may delay the next
region without changing manuscript order.

### D-005 — Speaker tracks are real product structure

A track owns:

- name and color;
- canonical voice reference;
- inherited Higgs delivery defaults;
- track output gain;
- ordered assigned regions;
- session monitoring state.

The Arrange view uses DAW-style vertically aligned track rows. Every speaker,
including the narrator, has one persistent horizontal channel strip: its name,
reference/default controls, output gain, Mute/Solo, and status live in a fixed
control column on the left; the corresponding waveform lane occupies the same
vertical row to the right on a shared time axis. Regions assigned to other
speakers appear as silence/gaps in that lane rather than collapsing the rows.

V1 supports manual reassignment of an existing region to a track. The layout is
multi-lane, but the content contract remains a single locked narrative EDL:
regions cannot overlap or play simultaneously. The detached voice-card list in
the current prototype is transitional and is not the target layout.

### D-006 — Future speaker inference and rechunking are V2

A future small language model may propose speaker spans and tracks chapter by
chapter. That version may combine intelligent and manual chunking. V1 neither
implements nor prematurely encodes that workflow.

### D-007 — Stable region identity

Region IDs remain durable across ordinary manuscript edits. The editor shows a
persistent visual cue for the region under the caret or selection. Inline Higgs
delivery controls operate on the selected text span and are stored explicitly.
Changing source or applied Higgs direction marks relevant takes generation-stale;
it never destroys them.

### D-008 — Take workflow

`Retry exact`, `New performance`, and `Variation` create new immutable takes.
The new take is selected for audition but does not replace the active EDL take.
The explicit workflow is:

`Generate → Audition → Activate`

`Retry exact` preserves the exact request and seed. `New performance` preserves
direction with a new seed. `Variation` applies a small recorded change to real
Higgs generation parameters.

### D-009 — Independent take states

- **Selected**: currently inspected or auditioned in the UI.
- **Active**: used by the EDL.
- **Approved**: accepted by human judgement under the QA policy recorded for the
  take.
- **Generation fresh**: generated from the current Higgs inputs.

An approved take may be generation-stale. Switching the active take changes the
EDL but does not alter human approval.

The UI exposes three independent actionable states:

- `Generate required`: actual Higgs inputs changed;
- `Run QA`: required QA is missing, pending, or obsolete;
- `Render out of date`: the active EDL or deterministic audio processing changed.

Neighbor take changes and seam edits do not make a take generation-stale.

### D-010 — Higgs direction versus audio automation

Higgs emotion, style, expressiveness, and other supported delivery tokens are
discrete instructions attached to regions or source spans. They require a new
take to become audible. Only controls demonstrated by the concrete Higgs
pipeline are exposed as reliable generation controls.

Trim, fades, gain, envelopes, and pauses are deterministic, non-destructive audio
operations with immediate audition. They mark render dirty but never request
Higgs regeneration.

### D-011 — Audio edit ownership

- Track output gain belongs to the track.
- Trim, fade-in, fade-out, clip gain, internal volume envelope, and internal
  pause markers belong to a specific take.
- A boundary pause belongs between two ordered regions.

Take edits do not transfer automatically when the active take changes. The UI
offers an explicit `Copy audio edits from…` operation.

### D-012 — No seam-review state

There is no seam freshness, seam approval, or mandatory seam-review gate. A seam
audition is an optional listening tool over deterministic audio. If it sounds
wrong, the operator may edit audio or generate a new complete take.

### D-013 — Monitoring is not rendering

Mute and Solo are visible, session-only monitoring controls. They reset when the
project is reopened and never affect Render, Export, EDL fingerprints, or output
manifests. While active, the UI strongly lights the controls, dims inaudible
tracks, shows a permanent audition-mode banner with `Clear audition mode`, and
states that Render includes all tracks.

If final track exclusion is ever required, it must be a separate explicit
operation. It is not part of V1.

### D-014 — Audition modes

A/B compares raw immutable Higgs take audio at its original output level. It
bypasses trims, fades, envelopes, pauses, track gain, loudness matching, and
normalization. This exposes level and compression defects rather than masking
them.

`In context` auditions the active take through its deterministic edits, track
gain, boundary pauses, and neighboring EDL regions.

### D-015 — Non-destructive V1 audio toolbox

V1 includes:

- a real waveform, playhead, and zoom;
- trim handles;
- fade-in and fade-out handles;
- clip gain;
- a true multi-point volume envelope;
- visible internal pause markers;
- inter-region boundary pause control;
- immediate browser audition of the edited signal;
- explicit copying of edits from another take.

### D-016 — QA gates and human override

Speaker similarity and ASR content QA are project-level toggles and are enabled
by default. A new take records a snapshot of the exact QA policy, models,
thresholds, source/reference fingerprints, and results.

An enabled QA suite runs asynchronously after generation. The take is immediately
auditionable but ordinary approval remains disabled while QA is pending. Passing
QA enables normal approval. Failure or abstention exposes only an explicit
`Approve with override`, requiring a recorded reason. Directors Room does not
automatically regenerate after QA failure.

Disabling a gate is visible on every affected take as `QA skipped`.

### D-017 — QA changes do not regenerate speech

Changing project QA toggles applies to future takes and never marks speech
generation stale. Existing takes preserve their original QA snapshot. The UI may
offer `Run missing QA` or an explicit recheck, stored as another QA evaluation of
the same immutable audio.

### D-018 — Approval and export preflight

Approval is human judgement after the applicable QA gate, not a technical render
lock. Render always works from the current complete EDL. Export shows a preflight
for unapproved active takes, generation-stale regions, missing/skipped/failed QA,
and missing audio. Warnings never create an absolute export prohibition;
`Export anyway` is explicit and recorded.

### D-019 — Autosave and history

Every editorial state change autosaves. Persistent `Undo` and `Redo`, including
`Cmd-Z` and `Shift-Cmd-Z`, survive reload and cover manuscript/direction edits,
activation, approval, pauses, take audio edits, track assignment, and track gain.
History operations never delete generated takes or exported revisions.

### D-020 — Render and export revisions

Render produces or reuses a cached preview keyed by the exact EDL/render
fingerprint. It does not call Higgs.

`Export master` creates an immutable numbered revision (`r001`, `r002`, …)
containing at minimum:

- master WAV;
- delivery MP3;
- a manifest containing active take IDs and hashes, edits, pauses, track gains,
  QA/approval state, renderer build, output hashes, and export override reason if
  applicable.

`latest` is only a pointer. An identical fingerprint resolves to the existing
revision unless the operator explicitly requests a distinct duplicate. Revisions
may have a human label and note.

### D-021 — Observability

Generation, QA, render, export, endpoint errors, and user overrides are visible
in the UI and recorded durably. No failed or skipped stage is silently reported
as success.

### D-022 — Optional baseline first read

Directors Room quietly suggests, but never requires, a baseline-first workflow:
choose and audition one canonical voice, inspect the number of missing regions
and an observed generation-time range, then generate the work from beginning to
end before making surgical corrections. The normal region-by-region workflow is
never hidden or disabled by onboarding.

The batch targets only regions without an active take. It creates the same exact,
immutable Higgs transaction as any other take, starts the configured QA gates,
and activates the result only if the region remains empty at commit time. It
never replaces an existing active choice and never marks its takes Approved.
Choosing the baseline voice explicitly changes track-level defaults while
preserving region-level overrides.

Successful regions survive cancellation or failure; resuming plans only the
remaining empty regions. Cancellation takes effect between regions because a
dispatched Higgs request is not safely cancellable. Generation-relevant source,
voice, direction, sampling, or track-assignment drift stops the job visibly
before another request. On completion, Render assembles the current complete EDL
without autoplay. The estimate is labeled as rough when it has little local
evidence and improves from recorded wall-time/audio-duration observations.

## V1 scope boundary

### Included

- locked narrative timeline and real speaker tracks;
- vertically aligned speaker channel strips and waveform lanes on one time axis;
- manual region-to-track assignment;
- Higgs-native immutable take transactions and configurable resident endpoint;
- editable manuscript and inline Higgs delivery instructions;
- take generation, raw A/B, context audition, activation, approval, and notes;
- waveform editing and the non-destructive toolbox in D-015;
- distinct generation, QA, and render state indicators;
- speaker similarity and ASR QA gates with visible override;
- session-only Mute/Solo monitoring;
- autosave and persistent Undo/Redo;
- cached renders and immutable revision exports;
- visible logs and failure states.

### Explicitly deferred

- AI speaker attribution;
- intelligent or manual rechunking workflow;
- free region movement, overlap, and simultaneous clips;
- crossfades;
- time-stretch and post-process pitch;
- EQ, compression, reverb, and plugin chains;
- destructive audio editing;
- cloud collaboration.

## V1 implementation constraints resolved

- Project state is schema-versioned, atomically snapshotted, recoverable, and
  persistently undoable while all private state remains untracked.
- Continuity metadata is diagnostic/audition-only and no longer participates in
  the generation fingerprint; old takes remain intact.
- Real hash-keyed waveforms, transport scrub/playhead, speaker lanes, track gain,
  and session-only monitoring replaced the placeholders.
- Speaker similarity and Bulgarian ASR are asynchronous, policy-snapshotted,
  append-only approval gates with reasoned human override.
- Private stories, audio, voice references, endpoint overrides, and logs remain
  untracked.
- V1 is verified against the local Mac endpoint while the endpoint contract stays
  address-independent.

## Open technical choices delegated to implementation

These do not require another product interview unless evidence contradicts a
ledger decision:

- waveform rendering and Web Audio implementation library;
- persistent command-log and snapshot format;
- ASR runtime adapter and background-job mechanism;
- exact renderer implementation and delivery MP3 encoding parameters;
- cache layout, retention, and migration mechanics;
- thresholds and short-utterance eligibility rules, provided they are recorded
  and visible rather than hidden.
