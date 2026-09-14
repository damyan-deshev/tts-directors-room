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
first-read workflow. The screenshots use a fictional demo manuscript, and all
private writing and reference voice material stays outside the repository.

I started this project because I was tired of a very specific nuisance: a take
could sound perfect, yet regenerating the next paragraph, adjusting a pause, or
comparing two seeds still meant juggling text, audio files, and generation
settings by hand.

TTS Directors Room is the local editor I built for that work with
[Higgs TTS 3](https://github.com/boson-ai/higgs-audio). I can edit the
manuscript, direct one region at a time, generate several performances, and
choose which take belongs in the finished reading. Speaker strips, manuscript
regions, waveforms, and Higgs controls share the same screen because I want the
text and its performance to stay together throughout the edit.

Every take carries the transaction that created it: the text and canonical
reference voice, the delivery tokens and sampling parameters, the seed, the
endpoint's model and build report, and the output hash. When I approve a take, I
am making one narrow claim: I listened to that performance and accepted it.

The first launch opens in English; Bulgarian is one click away, and the editor
remembers that choice locally.

---

## The audio workflow

```text
Generate -> Audition -> Activate -> Render -> Export
```

- Use **Generate** to create an immutable take, while the current take stays
  active until you choose a replacement.
- **Audition** plays takes raw, with their edits, or between the neighboring
  seams, which makes both A/B comparison and continuity checks possible.
- Activating a take places it in the edit decision list. **Approve** separately
  records that you heard and accepted the performance, even if its source or
  context later changes.
- During **Render**, the editor applies trims, fades, gain, automation, and
  pauses to the active EDL through deterministic audio processing, leaving
  Higgs idle.
- **Export** writes an immutable WAV, MP3, and manifest revision from the
  existing render, preserving the decisions behind the finished file.

`Retry exact` repeats the recorded request with its original seed, whereas
`New performance` keeps the direction and chooses a fresh seed; `Variation`
also changes the supported sampling parameters and records those changes with
the resulting take.

During generation, Higgs receives the selected canonical voice reference. The
editor reserves neighboring generated takes for audition and excludes them from
conditioning, because feeding one synthetic performance into the next
gradually compounds changes in voice and prosody across a long work.

## What works today

- The timeline is source-locked, which means every speaker strip lines up with
  its waveform while the regions continue to follow the manuscript's narrative
  order.
- Select a passage in the editable manuscript and you can add Higgs emotion,
  style, or prosody tokens exactly where the performance needs them.
- Each take has a real waveform with a playhead and zoom. Its own trim points,
  fades, clip gain, volume envelope, and internal pauses remain editable around
  the original generated audio.
- Pauses between regions are explicit, and speaker tracks have independent
  output gain. Raw A/B, edited playback, and contextual audition all remain
  available in the same workspace.
- Optional speaker-similarity and ASR checks sit beside the approval controls,
  where their evidence informs a listening decision and the listener retains
  responsibility for approval.
- Undo and Redo survive a restart, EDL renders are cached, and each export
  becomes an immutable revision.
- I usually begin with **First read**, which can generate the whole manuscript
  with a chosen voice and shows a rough time estimate before it starts. Hearing
  one complete performance tells me where detailed direction is worthwhile.
- Switching to another Higgs endpoint leaves the project intact, and a model
  that is already running remains loaded after generation.

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
       -> configured HIGGS_ENDPOINT
```

Open a take later and you can see exactly what created it: the generation
request, reference fingerprints, endpoint, reported model and build, seed, and
output hash. The same record holds its QA results, approval and notes, and every
non-destructive edit made afterward. Missing build information is stored
literally as `unreported`.

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

Browser responses contain a display label, an allowlisted audition URL, and
SHA-256 fingerprints for each reference. File paths and transcripts stay in the
local voice registry, where the browser cannot expose them.

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
