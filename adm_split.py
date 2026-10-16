"""
Extracts a time portion ("region") from an already-exported ADM/BW64
file, as a standalone song or act.

Why this isn't just an audio cut: for every object, block timings
(rtime/duration) are relative to the object's own start, which is in
turn relative to the start of the file (verified in the `ear` library's
own code: block_start = object_start + rtime). Extracting a region
therefore requires keeping only the blocks that touch it, shortening
the ones that straddle a boundary, and rebasing all timings onto the
new zero. The calculation logic (clip_object_timing) was verified by
hand on three cases - partial overlap, object entirely outside the
region, cut exactly on a boundary - before being used here.

Objects that don't overlap the region at all are removed entirely,
audio channel included, not left as silent channels: built for people
working with many multitrack takes in the same project.

Implementation: rather than hand-editing the source file's XML text
(risky - a field forgotten in a half-copied block is a silent error,
not a crash), the file is REBUILT from scratch with ADMBuilder, from
the object model the `ear` library itself provides on read. Individual
blockFormats are cloned with `attr.evolve`, changing only rtime/
duration: every other field (position, width, gain, diffuse...) is
guaranteed identical to the original because it's never hand-copied.
"""

import re
from fractions import Fraction

import attr
import numpy as np
import lxml.etree

from ear.fileio import openBw64, openBw64Adm
from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk
from ear.fileio.adm.builder import ADMBuilder
from ear.fileio.adm.generate_ids import generate_ids
from ear.fileio.adm.xml import adm_to_xml
from ear.fileio.adm.chna import populate_chna_chunk
from ear.core.metadata_input import ObjectRenderingItem, DirectSpeakersRenderingItem
from ear.core.select_items import select_rendering_items


# --------------------------------------------------------------------------
# Reading the region/marker CSV exported from REAPER (Region/Marker Manager)
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")


def _parse_time(s):
    """'0:00:12.000000' or '12.000000' -> seconds (float).

    The first format is REAPER's own (Region/Marker Manager, with the
    ruler set to Time (seconds)); the second is what export_and_split.lua
    writes directly (plain seconds, via the API, without going through
    the Manager). Both CSVs use the same "#,Name,Start,End,Length"
    header, so both are accepted here rather than constraining the Lua
    script to a format it would then have to reformat. None if empty
    (point marker).
    """
    s = s.strip()
    if not s:
        return None
    m = _TIME_RE.match(s)
    if m:
        h, mm, ss = m.groups()
        return int(h) * 3600 + int(mm) * 60 + float(ss)
    try:
        return float(s)
    except ValueError:
        raise ValueError(
            "Unrecognised time format: %r. Expected H:MM:SS.ffffff "
            "(REAPER, Region/Marker Manager with the ruler in Time "
            "(seconds)) or plain seconds (e.g. 12.000000)." % s)


def read_regions_csv(path):
    """Reads the CSV exported from the Region/Marker Manager.

    Returns a list of {id, name, start, end} dicts in SECONDS - point
    markers (no End) are dropped: they don't define an interval to
    extract.
    """
    with open(path, encoding="utf-8-sig") as f:
        lines = [l.rstrip("\n").rstrip("\r") for l in f if l.strip()]
    if not lines:
        raise ValueError("Empty file")

    header = [h.strip() for h in lines[0].split(",")]
    try:
        idx_id = header.index("#")
        idx_name = header.index("Name")
        idx_start = header.index("Start")
        idx_end = header.index("End")
    except ValueError:
        raise ValueError(
            "Unexpected CSV header: %r. Expected something like "
            "'#,Name,Start,End,Length'." % header)

    regions = []
    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= max(idx_id, idx_name, idx_start, idx_end):
            continue
        row_id = cols[idx_id].strip()
        name = cols[idx_name].strip()
        start = _parse_time(cols[idx_start])
        end = _parse_time(cols[idx_end])
        if start is None or end is None:
            continue  # point marker, not a region
        regions.append({"id": row_id, "name": name or row_id,
                        "start": start, "end": end})
    return regions


# --------------------------------------------------------------------------
# Timing cut/rebase
# --------------------------------------------------------------------------

def clip_object_timing(obj_start, obj_duration, blocks, t0, t1):
    """blocks: list of (rtime, duration) Fraction, relative to obj_start.

    Returns (new_obj_duration, [(new_rtime, new_duration), ...]) with
    timings already rebased onto the new zero (the region's start), or
    None if the object doesn't overlap [t0, t1) at all.
    """
    obj_end = obj_start + obj_duration
    if obj_end <= t0 or obj_start >= t1:
        return None

    new_obj_duration = min(obj_end, t1) - max(obj_start, t0)

    new_blocks = []
    for rtime, duration in blocks:
        abs_start = obj_start + rtime
        abs_end = abs_start + duration
        if abs_end <= t0 or abs_start >= t1:
            continue
        clipped_start = max(abs_start, t0)
        clipped_end = min(abs_end, t1)
        new_rtime = clipped_start - max(obj_start, t0)
        new_duration = clipped_end - clipped_start
        new_blocks.append((new_rtime, new_duration))

    return (new_obj_duration, new_blocks)


# --------------------------------------------------------------------------
# Main splitter
# --------------------------------------------------------------------------

def split_region(input_path, output_path, region_start_s, region_end_s,
                 log=None):
    """Extracts [region_start_s, region_end_s) from input_path into output_path."""

    def _log(msg):
        if log:
            log(msg)

    t0 = Fraction(region_start_s).limit_denominator(10_000_000)
    t1 = Fraction(region_end_s).limit_denominator(10_000_000)
    if t1 <= t0:
        raise ValueError("Region end must be after start")

    with openBw64Adm(input_path) as reader:
        sample_rate = reader.sampleRate
        bitdepth = reader.bitdepth
        n_samples_total = len(reader._bw64)
        file_duration = Fraction(n_samples_total, sample_rate)

        items = select_rendering_items(reader.adm)

        builder = ADMBuilder()
        builder.create_programme(audioProgrammeName="split")
        builder.create_content(audioContentName="c")

        # source audio channel (0-based index) -> new index (0-based)
        # in the order we add them to the builder
        source_channels = []
        n_kept = 0
        n_dropped = 0

        for item in items:
            block = item.metadata_source.get_next_block()
            if block is None:
                n_dropped += 1
                continue
            bf = block.block_format

            obj_start = (block.extra_data.object_start
                        if block.extra_data.object_start is not None
                        else Fraction(0))
            obj_dur = (block.extra_data.object_duration
                      if block.extra_data.object_duration is not None
                      else file_duration - obj_start)

            # collect ALL blocks of this channel format (not just the
            # first): keep pulling from metadata_source to walk them all
            all_blocks = [bf]
            while True:
                nxt = item.metadata_source.get_next_block()
                if nxt is None:
                    break
                all_blocks.append(nxt.block_format)

            times = [(b.rtime if b.rtime is not None else Fraction(0),
                     b.duration if b.duration is not None else obj_dur)
                    for b in all_blocks]

            result = clip_object_timing(obj_start, obj_dur, times, t0, t1)
            if result is None:
                n_dropped += 1
                continue

            new_obj_duration, new_times = result
            new_blocks = [
                attr.evolve(all_blocks[i], rtime=new_times[i][0],
                           duration=new_times[i][1])
                for i in range(len(new_times))
            ]

            channel_index = item.track_spec.track_index  # 0-based
            new_channel_index = n_kept
            source_channels.append(channel_index)

            name = getattr(item, "name", None) or "obj%d" % (n_kept + 1)

            if isinstance(item, ObjectRenderingItem):
                builder.create_item_objects(
                    track_index=new_channel_index, name=name,
                    block_formats=new_blocks)
            elif isinstance(item, DirectSpeakersRenderingItem):
                builder.create_item_direct_speakers(
                    track_index=new_channel_index, name=name,
                    block_formats=new_blocks)
            else:
                _log("  ! type not handled by this splitter (%s), "
                     "item dropped" % type(item).__name__)
                source_channels.pop()
                n_dropped += 1
                continue

            adm = builder.adm
            adm.audioObjects[-1].start = Fraction(0)
            adm.audioObjects[-1].duration = new_obj_duration
            n_kept += 1

        _log("Channels kept: %d, dropped: %d" % (n_kept, n_dropped))

        if n_kept == 0:
            raise ValueError(
                "No item overlaps this region (%.3f-%.3f s): the output "
                "file would be empty. Check the region's timing against "
                "the source file's duration." % (float(t0), float(t1)))

        adm = builder.adm
        generate_ids(adm)

        n_out_samples = int(round((t1 - t0) * sample_rate))
        start_sample = int(round(t0 * sample_rate))

        chna = ChnaChunk()
        populate_chna_chunk(chna, adm)
        fmt = FormatInfoChunk(formatTag=1, channelCount=len(source_channels),
                              sampleRate=sample_rate, bitsPerSample=bitdepth)
        xml = lxml.etree.tostring(adm_to_xml(adm), pretty_print=True)

        with openBw64(output_path, "w", formatInfo=fmt, axml=xml,
                     chna=chna) as out:
            block_size = 65536
            done = 0
            reader._bw64.seek(start_sample)
            while done < n_out_samples:
                n = min(block_size, n_out_samples - done)
                block = reader._bw64.read(n)
                out.write(block[:, source_channels])
                done += n

    _log("Written: %s" % output_path)
    return {"kept": n_kept, "dropped": n_dropped}


def verify(output_path, log=None):
    """Reopens the file with the full ADM reader, for confirmation."""
    with openBw64Adm(output_path) as f:
        items = select_rendering_items(f.adm)
    if log:
        log("Verification: %d items, %d audio channels." %
            (len(items), f.channels))
    return len(items)
