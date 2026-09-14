# TTS Directors Room

<p align="center">
  <img src="docs/screenshots/arrange-and-direction.png" width="100%" alt="TTS Directors Room arrange view with aligned speaker tracks, manuscript editor, and Higgs controls">
</p>

<p align="center">
  <img src="docs/screenshots/takes-and-edits.png" width="100%" alt="Immutable take comparison and deterministic audio edits">
</p>

<p align="center">
  <img src="docs/screenshots/first-read.png" width="80%" alt="Optional first-read workflow with voice audition and time estimate">
</p>

These are three working views of the application, covering speaker and
manuscript arrangement, take selection and audio editing, and the optional
first-read workflow. The screenshots use a fictional demo manuscript, so no
private writing or reference voice appears in this repository.

> An audiobook needs direction; pressing Generate insistently and hoping the
> seventeenth seed will understand the hint is rarely enough.

TTS Directors Room is a local non-linear performance editor for
[Higgs TTS 3](https://github.com/boson-ai/higgs-audio), built around a manuscript
that remains the source of truth while every region acquires its own immutable
takes, active performance, human judgment, and non-destructive audio edits.

The interface borrows the useful spatial language of a DAW while remaining
specific to spoken-word generation with Higgs. For every take, it records the
actual generation transaction, including the text, canonical reference voice,
delivery tokens, sampling parameters, seed, endpoint and model observations,
build information, and output hash.

English is the default interface language on first launch, while Bulgarian can
be selected with one click and the preference is then stored locally.

---

## The audio workflow

```text
Generate -> Audition -> Activate -> Render -> Export
```

- **Generate** creates a new immutable take and leaves the current active take
  unchanged, so a new performance never silently enters the edit.
- **Audition** provides raw A/B comparison as well as edited and contextual
  playback, allowing each take to be judged both on its own and at its seams.
- **Activate** places the chosen performance in the edit decision list, while
  **Approve** records the separate human judgment that the take has been heard
  and accepted; approval therefore remains independent of freshness.
- **Render** assembles the active EDL with trims, fades, gain, automation, and
  pauses, using deterministic audio processing without calling Higgs again.
- **Export** turns an existing render into an immutable WAV, MP3, and manifest
  revision that can be traced back to its complete set of decisions.

The take controls preserve three genuinely different intentions. `Retry exact`
repeats the recorded request with its original seed, `New performance` retains
the direction while choosing a new seed, and `Variation` records a small change
to supported sampling parameters.

During generation, Higgs receives only the selected canonical voice reference.
Neighboring generated takes remain available as audition context, but never
become conditioning audio, because feeding one synthetic performance into the
next gradually compounds changes in voice and prosody across a long work.

## What works today

- A source-locked timeline aligns every speaker channel strip vertically with
  its waveform lane, while preserving the narrative order of the manuscript.
- The manuscript remains editable and supports inline Higgs emotion, style,
  and prosody tokens at the precise spans where their direction is needed.
- Every take has a real waveform, playhead, zoom, trim points, fade-in and
  fade-out, clip gain, volume automation, and internal pauses.
- Region boundaries carry explicit pauses, and each speaker track has its own
  output gain without introducing invisible changes to the underlying take.
- Raw immutable A/B comparison, edited audition, and contextual audition are
  available from the same workspace.
- Speaker similarity and ASR checks appear as visible, optional approval gates,
  keeping automated evidence close to the human listening decision.
- Persistent Undo and Redo, cached EDL renders, and immutable export revisions
  preserve both experimentation and reproducibility.
- A quiet, optional **First read** workflow lets a voice be chosen and the
  entire manuscript generated with a rough time estimate before detailed
  direction begins.
- The Higgs endpoint is independently configurable, and the UI server leaves a
  running model loaded after generation.

AI-assisted speaker attribution, rechunking, free movement of regions,
crossfades, and plugin chains remain future concerns rather than hidden V1
promises. The current product contract and the reasoning behind its boundaries
live in [`docs/product-ledger.md`](docs/product-ledger.md).

## Quick start

The application requires Python 3.11 or newer, `ffmpeg` for rendering and
export, and a running Higgs endpoint. Its main UI and API server use only the
Python standard library, while the similarity and ASR runtimes are loaded lazily
when their respective gates are requested.

```bash
git clone https://github.com/damyan-deshev/tts-directors-room.git
cd tts-directors-room

python3 -m venv .venv
source .venv/bin/activate

cp voices.example.json voices.local.json
python bootstrap_project.py /path/to/manuscript.txt \
  --title "The Lighthouse" \
  --author "A. Writer" \
  --language en

python server.py \
  --host 127.0.0.1 \
  --port 8767 \
  --higgs-endpoint http://127.0.0.1:8765
```

Once the server is running, open <http://127.0.0.1:8767/>.

`bootstrap_project.py` accepts UTF-8 `.txt`, `.md`, and `.html` files. Blank
lines separate paragraphs in text and Markdown manuscripts, while HTML imports
the contents of `<p>` elements; to protect existing work, the importer refuses
to replace a local project unless `--force` is supplied explicitly.

### Reference voices

Reference voices are registered in `voices.local.json`, which remains local and
is excluded from Git:

```json
{
  "canonical_narrator": {
    "label": "Studio reference A",
    "description": "Immutable narrator reference.",
    "audio": "/absolute/path/to/reference.wav",
    "transcript": "/absolute/path/to/reference.txt"
  }
}
```

The registry may also live outside the checkout:

```bash
HIGGS_VOICES_CONFIG=/private/path/voices.json \
HIGGS_ENDPOINT=http://another-host:8765 \
python server.py --port 8767
```

For local use on a Mac, `run.command` launches the application with its own
`.venv` or with a Higgs environment selected through `HIGGS_LOCAL_DIR`. When the
default local endpoint is unavailable, the launcher can start Higgs and will
leave the model running afterward, avoiding repeated load and unload cycles.

## What is stored

```text
browser
  -> editable manuscript and directing decisions
  -> local Python API
       -> versioned project + persistent command history
       -> immutable takes + QA evaluations
       -> deterministic render/export pipeline
       -> concrete HIGGS_ENDPOINT
```

Each take retains its exact request, canonical-reference fingerprints,
endpoint, model and build observations, seed, output hash, QA results, approval
and notes, and non-destructive edits. Whenever an endpoint omits its build
information, the value is recorded as `unreported` rather than inferred.

The following directories and files contain local material and are ignored by
Git:

```text
data/             manuscript and project state
voices/           reference material
preview-audio/    generated previews
takes/            immutable performances
renders/          cached EDL renders
waveforms/        peaks and edited previews
exports/          delivery revisions
logs/             audit and runtime logs
config.local.json
voices.local.json
```

Reference file paths and transcripts are withheld from browser responses. The
interface receives the display label, an allowlisted audition URL, and SHA-256
fingerprints, which is enough for direction and provenance without exposing the
local voice registry.

## Verification

```bash
python -m unittest -v \
  test_bootstrap_project.py \
  test_lab.py \
  test_directors_room.py

node --check static/i18n.js
node --check static/app.js
node static/i18n.test.js
python -m py_compile \
  bootstrap_project.py \
  server.py \
  directors_room.py \
  project_store.py
```

## Name, licensing, and Higgs

TTS Directors Room is an independent project whose support for “Higgs TTS 3”
and “Higgs Audio” is described by name for clarity. The project has no
affiliation with, sponsorship from, or endorsement by Boson AI.

The repository is public so the work can be inspected, although its code
remains source-available with all rights reserved; publication alone grants no
permission for copying or commercial use. The full terms are in
[`LICENSE`](LICENSE).

The Higgs model and its code are distributed separately under their respective
terms and are not included here. Relevant attribution and links are collected
in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
