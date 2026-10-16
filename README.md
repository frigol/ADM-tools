# ADM to AmbiX

Converts an ADM/BW64 scene into an ambisonic file, encoding every object
**from its position** rather than transcoding a rendered layout. No double
panning.

Also splits an already-rendered ADM file into standalone per-region files
(e.g. one per song on an album), and repairs a known ADM export fault
from the [EAR Production Suite](https://github.com/ebu/ear-production-suite).

## Double-clickable apps (no Terminal, fully self-contained)

Two `.app` bundles in [`Compiled App/`](Compiled%20App/) — Python, numpy,
lxml and the `ear` library are all frozen inside each one (via
PyInstaller), so they are genuinely standalone: copy either one anywhere
(Desktop, Applications, a USB stick, another Mac), rename it, open with a
double click. No dependency on this folder or on Python being installed
at all.

- **ADM to AmbiX.app** — the conversion GUI (`adm2ambix.py`): order,
  normalisation, per-item inclusion, repair.
- **ADM Split by Region.app** — the region-split GUI (`adm_split_gui.py`,
  see "Splitting by region" below): reads regions straight from a
  REAPER `.rpp` project file, or from a CSV.

Caveats: ~115 MB each (the scientific Python stack doesn't compress
small); built for Apple Silicon (arm64) — won't run on an Intel Mac or
any other OS without rebuilding there; unsigned, so macOS Gatekeeper
will refuse a plain double-click on a copy that came from the internet
or AirDrop (quarantine flag) — right-click → Open once, or System
Settings → Privacy & Security → "Open Anyway", to approve it the first
time on each machine.

## Running from source instead

```bash
pip3 install ear
python3 adm2ambix.py
```

`ear` brings numpy and lxml with it. `tkinter` is part of the standard
Python distribution on macOS. No other dependency.

To rebuild the `.app` bundles after a code change:
```bash
pip3 install pyinstaller
pyinstaller --windowed --name "ADM to AmbiX" --collect-all ear \
  --hidden-import tkinter --collect-all pkg_resources --noconfirm adm2ambix.py
pyinstaller --windowed --name "ADM Split by Region" --collect-all ear \
  --hidden-import tkinter --collect-all pkg_resources --noconfirm adm_split_gui.py
```

## Usage

Interface: add files (even many, for a whole album), choose the
destination folder, the order and the options, run. Files are processed
in sequence, each in blocks, and reassembled into a single output file.

**Repair from the same window**: if one or more queued files were
exported by the EAR Production Suite with the faulty structure described
below, select them in the list and press "Repair selected (EPS)". This
writes a `_repaired.wav` file next to the original — the original is
left untouched — and queues it automatically, ready for conversion. It
remains an explicit step, separate from conversion, not automatic: an
already-correct file passes through unchanged, so selecting one by
mistake has no consequences.

Command line:

```
python3 adm2ambix.py --cli --inspect *.wav
python3 adm2ambix.py --cli in.wav -n 5 --max-re -o /output/path
python3 adm2ambix.py --repair exported_file.wav -o /output/path
```

## What it does

| ADM type | Treatment |
|---|---|
| Object | encoded from position, with width/height/depth and diffuse |
| DirectSpeakers | encoded from nominal directions |
| HOA | summed directly, with normalisation matching |

Block temporal interpolation uses the reference library's
`InterpretObjectMetadata`, so behaviour over time matches the EAR
renderer's.

## Design choices, and why

**Linear combination of the extent.** `ear`'s `PolarExtentPanner`
computes `sqrt(sum(pv²))` because speaker gains are non-negative.
Spherical harmonic coefficients are *signed*: squaring them would
destroy phase. This tool uses the underlying `SpreadingPanner`, which is
linear, and combines points with a weighted average.

The consequence is physically correct: as the source widens, higher-order
components attenuate progressively while W stays constant. At width 0 the
coefficients match the point source exactly; at width 360 only W remains.

Verified at third order:

```
width   0: energy per order [1.000 1.000 1.000 1.000]
width  30: [1.000 0.967 0.904 0.815]
width  90: [1.000 0.819 0.523 0.215]
width 360: [1.000 0.000 0.000 0.000]
```

**Diffuse via per-harmonic-channel decorrelators.** The standard designs
decorrelation filters for the speakers of a layout; no layout exists in
the ambisonic domain. This uses one random-phase all-pass filter per ACN
channel, with the library's own seeds. It's a design choice, not a
BS.2127 requirement: the output will diverge from the EAR render on this
parameter. Can be disabled.

**Path alignment.** The decorrelators delay the object path;
DirectSpeakers and HOA don't have this delay. Summing them directly
would produce a misalignment of about 8 ms at 96 kHz. The library's own
`BlockAligner` is used, exactly as the reference renderer does. The
output has exactly the same length as the input.

**What isn't implemented.** `zoneExclusion` and `channelLock` are
defined relative to a speaker layout and have no ambisonic equivalent:
they are ignored. `objectDivergence` is not handled. Screen-related
metadata (`screenRef`, `screenEdgeLock`) is not handled.

## Sample rate

No constraint: the library only validates `sampleRate >= 1`, and 48000
is just the constructor's default value. Verified writing and reading at
96 kHz. 48 kHz is a broadcast-delivery convention, not a limit of the
format.

## Conventions

AmbiX output: ACN channel ordering, SN3D normalisation (N3D optional).
Counter-clockwise azimuth from the front, as in ADM.

Verified: a DirectSpeakers item at +30° produces Y/W = 0.500 and
X/W = 0.866, both positive — i.e. sin(30°) and cos(30°).

**Worth checking yourself**, since it depends on your own chain: that
the orientation matches what your IEM decoders expect. Encode a source
at 90° and check it comes out on the left, not the right.

## Repairing ADM files exported by the EAR Production Suite

The [EAR Production Suite](https://github.com/ebu/ear-production-suite)
can export BW64/ADM files with broken references. Two distinct patterns
have been found and are repaired by `adm_repair.py`:

- **Locally-defined channel formats** (e.g. an Object): the
  `audioTrackFormat`/`audioStreamFormat` elements are missing entirely;
  the `chna` references the channel format's ID with `_00` stuck on the
  end. The missing elements are generated with conforming IDs (`AT_`/`AS_`,
  following the same naming pattern as the common definitions), and the
  `chna` row is rewritten to match.
- **Standard layouts / common definitions** (e.g. 5.1): the `chna`
  references an ID that is neither local nor a valid common definition.
  It is corrected with the real ID, retrieved from the `ear` library's
  own catalogue (~300 entries), with no guessing at the naming pattern.
- In both cases, the EAR Production Suite also writes a redundant direct
  reference (`audioChannelFormatIDRef`) inside the `<audioTrackUID>`
  element itself — the simplified BS.2076-2 style. Once the classic
  reference chain is rebuilt, that redundant reference is removed, so
  the file carries a single, unambiguous addressing scheme. Without
  this, REAPER's own importer rejects the file as "mutually-exclusive
  AudioTrackUid references", even though the reference library accepts it.

If a reference fits neither case, nothing is written and the anomaly is
reported instead.

**A step separate from conversion, not automatic**: an already-correct
file passes through unchanged (verified), so it's safe to always run it,
but it remains a distinct action by choice — useful when converting ADM
files that don't need repair. Available both as a button in the GUI
("Repair selected (EPS)") and from the command line:

```
python3 adm2ambix.py --repair exported_file.wav -o /output/path
```

Verified on a real exported file: audio identical, byte for byte, to the
original after repair; the reference library reads it correctly (7 items
found, versus 0 before, due to the reference error); REAPER's own ADM
importer accepts the repaired file.

## Splitting by region (an album's songs out of one ADM export)

Fully after-the-fact: it only ever touches an ADM file you've already
rendered, however and whenever you rendered it (whatever render format
you used that day — this never asks you to fix your render settings to
one value). Workflow:

1. Record and edit normally; mark each song/movement as a region.
2. Render the full session to ADM BW64, as you'd render anything else.
3. Open **ADM Split by Region.app**, point it at the rendered file and
   at the project's **`.rpp` file** — regions are read straight out of
   it, no REAPER needed open, no manual export step. Uncheck any region
   you don't want, choose a destination, click Split.

Each output file is a standalone ADM document: object timing is rebased
to zero at the region's start, and objects that don't overlap the region
at all are dropped entirely (audio channel included), not just silenced.

**Broken references are repaired automatically.** Before splitting, the
app runs the same repair step described above on a throwaway temporary
copy of the source file (not saved, deleted once the split is done) and
splits from that instead — no separate "Repair selected (EPS)" step
needed first, and no effect on an already-correct file (the repair is a
no-op, just a copy, when nothing needs fixing). If the file has a
reference problem the repair step can't resolve, the error says so
explicitly instead of a raw traceback.

Regions are **not** stored inside the ADM/BWF file itself — that's not
part of the ADM standard, and the reader this tool uses doesn't parse
WAV cue/region chunks even if REAPER wrote any — so the project file (or
a CSV, see below) is what supplies them, not the rendered audio file.

Reading straight from `.rpp` (`rpp_regions.py`) is based on the project
file's actual on-disk `MARKER` line format, reverse-engineered and
verified against several real projects (not guessed): a region is a
*pair* of `MARKER` lines sharing one id — a long form (carries the name
and start) and, later, a short form (just the end position); a point
marker is a long-form line with no matching short form. The field that
looks like an "is a region" flag turned out to be unreliable across real
files (seen as `0` on some point markers, `8` on others) — the parser
keys only on line length and pairing, not on that field's value.

**Alternative, if you'd rather not hand over the `.rpp`:** REAPER's
Region/Marker Manager's own CSV export still works — right-click the
region list → export the formatted list (`Start`/`End` as
`H:MM:SS.ffffff`, set the ruler to Time (seconds) first if asked) — and
the app's "CSV..." button reads that the same way. Either source feeds
the same region list in the app.

Command line (same functionality): `python3 adm2ambix.py --split
in.wav --regions-csv regions.csv -o /out`, or `--start`/`--end` for a
single region without a CSV.

## Still to test on your material

- behaviour with ~50 objects and long files (timing, memory)
- comparison between this AmbiX output and the EAR render on the same layout
- tuning of the `diffuse` parameter, the least standardised part

## License

MIT — see [LICENSE](LICENSE). Independent tool; not affiliated with the
EBU, BBC, or IRT (whose EAR Production Suite is mentioned above only
because this tool reads/repairs its ADM export format).
