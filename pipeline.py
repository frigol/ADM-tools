"""ADM -> AmbiX conversion pipeline, block-based, with a file queue."""

import os
from itertools import chain

import numpy as np

from ear.fileio import openBw64Adm, openBw64
from ear.fileio.bw64.chunks import FormatInfoChunk
from ear.fileio.adm import timing_fixes
from ear.core.select_items import select_rendering_items
from ear.core.metadata_processing import preprocess_rendering_items
from ear.core.metadata_input import (
    ObjectRenderingItem,
    DirectSpeakersRenderingItem,
    HOARenderingItem,
)
from ear.core.hoa import MaxRECoefficients, from_acn
from ear.core.block_aligner import BlockAligner

from encoder import (
    HOAEncoder,
    ObjectHOARenderer,
    DirectSpeakersHOARenderer,
    HOAPassthrough,
    n_channels_for_order,
)


class Settings:
    def __init__(self, order=3, norm="SN3D", block_size=4096,
                 include_objects=True, include_direct_speakers=True,
                 include_hoa=True, use_diffuse=True, apply_max_re=False,
                 apply_distance_gain=False, bit_depth=None, gain_db=0.0):
        self.order = order
        self.norm = norm
        self.block_size = block_size
        self.include_objects = include_objects
        self.include_direct_speakers = include_direct_speakers
        self.include_hoa = include_hoa
        self.use_diffuse = use_diffuse
        self.apply_max_re = apply_max_re
        self.apply_distance_gain = apply_distance_gain
        self.bit_depth = bit_depth
        self.gain_db = gain_db


def inspect_file(path):
    """Reads the file and reports what it contains, without rendering."""
    info = {"path": path, "objects": 0, "direct_speakers": 0, "hoa": 0,
            "other": 0, "channels": 0, "sample_rate": 0, "bit_depth": 0,
            "duration_s": 0.0, "hoa_orders": set(), "errors": []}
    with openBw64Adm(path) as f:
        info["channels"] = f.channels
        info["sample_rate"] = f.sampleRate
        info["bit_depth"] = f.bitdepth
        try:
            info["n_samples"] = len(f._bw64)
            info["duration_s"] = info["n_samples"] / float(f.sampleRate)
        except (AttributeError, TypeError):
            info["n_samples"] = 0
        try:
            f.adm.validate()
        except Exception as exc:
            info["errors"].append(str(exc))
        items = _get_items(f.adm)
        for it in items:
            if isinstance(it, ObjectRenderingItem):
                info["objects"] += 1
            elif isinstance(it, DirectSpeakersRenderingItem):
                info["direct_speakers"] += 1
            elif isinstance(it, HOARenderingItem):
                info["hoa"] += 1
                block = it.metadata_source.get_next_block()
                if block is not None:
                    info["hoa_orders"].add(max(block.orders))
            else:
                info["other"] += 1
    return info


def _get_items(adm):
    items = select_rendering_items(adm)
    return preprocess_rendering_items(items)


def _max_re_gains(order, n_ch):
    """max-rE gains per ACN channel."""
    coeffs = MaxRECoefficients(order)
    n, _m = from_acn(np.arange(n_ch))
    return np.array([coeffs[int(ni)] for ni in n])


def convert_file(input_path, output_path, settings, progress=None, log=None):
    """Converts one file. progress(frac) and log(str) are optional callbacks."""

    def _log(msg):
        if log:
            log(msg)

    encoder = HOAEncoder(order=settings.order, norm=settings.norm)
    n_ch = encoder.n_ch

    with openBw64Adm(input_path) as infile:
        try:
            timing_fixes.check_blockFormat_timings(infile.adm, fix=True)
        except Exception as exc:
            _log("  timing warning: %s" % exc)

        items = _get_items(infile.adm)

        objs = [i for i in items if isinstance(i, ObjectRenderingItem)]
        dss = [i for i in items if isinstance(i, DirectSpeakersRenderingItem)]
        hoas = [i for i in items if isinstance(i, HOARenderingItem)]
        skipped = len(items) - len(objs) - len(dss) - len(hoas)

        _log("  found: %d objects, %d direct speakers, %d HOA packs"
             % (len(objs), len(dss), len(hoas)))
        if skipped:
            _log("  %d unsupported items ignored" % skipped)

        renderers = []
        if settings.include_objects and objs:
            r = ObjectHOARenderer(encoder,
                                  block_size=512,
                                  use_diffuse=settings.use_diffuse,
                                  apply_distance_gain=settings.apply_distance_gain)
            r.set_rendering_items(objs)
            renderers.append(r)
        if settings.include_direct_speakers and dss:
            r = DirectSpeakersHOARenderer(encoder)
            r.set_rendering_items(dss)
            renderers.append(r)
        if settings.include_hoa and hoas:
            r = HOAPassthrough(encoder)
            r.set_rendering_items(hoas)
            if r.dropped_channels:
                _log("  WARNING: %d HOA channels above order %d dropped"
                     % (r.dropped_channels, settings.order))
            renderers.append(r)

        if not renderers:
            raise RuntimeError("no items to render with these options")

        gains = np.ones(n_ch)
        if settings.apply_max_re:
            gains = gains * _max_re_gains(settings.order, n_ch)
            _log("  max-rE enabled")
        gains = gains * (10.0 ** (settings.gain_db / 20.0))

        bit_depth = settings.bit_depth or infile.bitdepth
        fmt = FormatInfoChunk(formatTag=1, channelCount=n_ch,
                              sampleRate=infile.sampleRate,
                              bitsPerSample=bit_depth)

        total = len(infile._bw64)
        done = 0
        peak = 0.0

        # The object path's decorrelators introduce a delay the other
        # paths don't have: BlockAligner realigns them, as the reference
        # renderer does. Summing them directly would be wrong.
        aligner = BlockAligner(n_ch)
        max_delay = max(getattr(r, "overall_delay", 0) for r in renderers)

        def render_block(start_sample, block):
            for r in renderers:
                delay = getattr(r, "overall_delay", 0)
                aligner.add(start_sample - delay,
                            r.render(infile.sampleRate, start_sample, block))
            return aligner.get()

        with openBw64(output_path, "w", formatInfo=fmt) as outfile:
            start_sample = 0
            blocks = chain(infile.iter_sample_blocks(settings.block_size),
                           [None])
            for block in blocks:
                if block is None:
                    if max_delay <= 0:
                        break
                    block = np.zeros((max_delay, infile.channels))

                out = render_block(start_sample, block)
                if len(out):
                    out = out * gains
                    peak = max(peak, float(np.max(np.abs(out))))
                    outfile.write(out)

                start_sample += len(block)
                done += len(block)
                if progress and total:
                    progress(min(done / float(total), 1.0))

    _log("  output peak: %.2f dBFS" % (20 * np.log10(peak) if peak > 0
                                       else -np.inf))
    if peak > 1.0:
        _log("  WARNING: output is clipping, reduce the gain")
    return peak


def convert_batch(jobs, settings, progress=None, log=None, should_stop=None):
    """jobs: list of (input_path, output_path). Runs sequentially."""
    results = []
    n = len(jobs)
    for idx, (src, dst) in enumerate(jobs):
        if should_stop and should_stop():
            if log:
                log("Stopped by user.")
            break
        if log:
            log("[%d/%d] %s" % (idx + 1, n, os.path.basename(src)))
        try:
            def sub_progress(frac, idx=idx):
                if progress:
                    progress((idx + frac) / n)
            peak = convert_file(src, dst, settings,
                                progress=sub_progress, log=log)
            results.append((src, dst, None, peak))
            if log:
                log("  written: %s" % os.path.basename(dst))
        except Exception as exc:
            results.append((src, dst, exc, None))
            if log:
                log("  ERROR: %s" % exc)
    if progress:
        progress(1.0)
    return results
