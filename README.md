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

The screenshots show the app as it works today: arranging speakers and text,
choosing and editing takes, and doing an optional first read. They use a
fictional demo manuscript, and all private writing and reference voice material
stays outside the repository.

I built TTS Directors Room because I was matching manuscript passages to WAV
files and tracking which Higgs settings produced each take by hand. It's a local
performance editor for
[Higgs TTS 3](https://github.com/boson-ai/higgs-audio) that keeps the manuscript,
speaker strips, generated takes, waveforms, and direction controls in one place,
so I can work through the text one region at a time, try several performances,
and choose the one I want in the finished reading.

I've set it up so every take keeps what I used to generate it: the text,
reference voice, delivery tokens, sampling settings, seed, the endpoint's model
and build report, and the output hash. I use approval to mean that I've listened
to a take and want to keep it.

The first launch opens in English; Bulgarian is one click away, and the editor
remembers that choice locally.

---

## The audio workflow

```text
Generate -> Audition -> Activate -> Render -> Export
```

- **Generate** gives me a new immutable take, while the current one stays active
  until I choose a replacement.
- **Audition** lets me compare raw takes, hear their edits, or play a take with
  its neighboring seams when I need to check continuity.
- When I activate a take, it enters the edit decision list; **Approve** records
  that I've heard it and want to keep it, even if its source or context later
  changes.
- During **Render**, the editor applies trims, fades, gain, automation, and
  pauses to the active EDL through deterministic audio processing, leaving
  Higgs idle.
- **Export** writes an immutable WAV, MP3, and manifest revision from the
  existing render, preserving the decisions behind the finished file.

`Retry exact` repeats the recorded request with its original seed, whereas
`New performance` keeps the direction and chooses a fresh seed; `Variation`
also changes the supported sampling parameters and records those changes with
the resulting take.

When I generate a take, I send Higgs the selected canonical voice reference. I
keep neighboring generated takes around for audition and out of the conditioning
input, because feeding one synthetic performance into the next gradually
changes the voice and prosody across a long work.

## What works today

- I keep the timeline source-locked, so every speaker strip lines up with its
  waveform and the regions continue to follow the manuscript's narrative order.
- I can select a passage in the manuscript and add Higgs emotion, style, or
  prosody tokens where I want the delivery to change.
- Each take gives me a real waveform with a playhead and zoom, plus trim points,
  fades, clip gain, volume automation, and internal pauses that I can edit around
  the original audio.
- Pauses between regions are explicit, and speaker tracks have independent
  output gain. Raw A/B, edited playback, and contextual audition all remain
  available in the same workspace.
- I've put the optional speaker-similarity and ASR checks beside the approval
  controls, where I can use their evidence and still make the listening decision
  myself.
- Undo and Redo survive a restart, EDL renders are cached, and each export
  becomes an immutable revision.
- I usually begin with **First read**, which can generate the whole manuscript
  with a chosen voice and shows a rough time estimate before it starts. Hearing
  one complete performance tells me where detailed direction is worthwhile.
- I can switch to another Higgs endpoint without rebuilding the project, and I
  leave an already running model loaded after generation.

For V1, I've left AI-assisted speaker attribution, rechunking, free movement of
regions, crossfades, and plugin chains for later work. I keep the current
product contract, including the reasoning behind those boundaries, in
[`docs/product-ledger.md`](docs/product-ledger.md).

## Quick start

The application requires Python 3.11 or newer, `ffmpeg` for rendering and
export, and a running Higgs endpoint. The main UI and API server run on the
Python standard library, with the similarity and ASR runtimes loading lazily
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

I keep reference voices in `voices.local.json`, which stays local and out of
Git:

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
       -> configured HIGGS_ENDPOINT
```

When I open a take later, I can see the request, reference fingerprints,
endpoint, reported model and build, seed, and output hash that produced it. The
same record holds its QA results, approval and notes, and every non-destructive
edit made afterward. If the endpoint omits its build information, I store it as
`unreported`.

I keep the following local material outside Git:

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

I give the browser a display label, an allowlisted audition URL, and SHA-256
fingerprints for each reference. The file paths and transcripts stay in the
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

I've made the repository public so people can inspect the work. I retain all
rights to the source-available code, and [`LICENSE`](LICENSE) contains the terms
that govern copying and commercial use.

The Higgs model and its code are distributed separately under their respective
terms. Relevant attribution and links are collected in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
