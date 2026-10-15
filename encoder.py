"""
Encodes an ADM scene into Ambisonics (AmbiX / ACN-SN3D or N3D).

Reuses the ITU-R BS.2127 reference implementation (the `ear` library) for:
  - rendering item selection and preprocessing
  - block temporal interpolation
  - extent weighting function (width/height)
  - spherical harmonics

Deliberate differences from the reference renderer, documented here:
  1. Spread points are combined LINEARLY, not energetically.
     `ear`'s PolarExtentPanner computes sqrt(sum(pv^2)) because speaker
     gains are non-negative. Spherical harmonic coefficients are signed:
     squaring them would destroy phase. Linear weighted averaging is the
     correct encoding here, and it has a physically sensible consequence:
     as the source widens, higher-order components attenuate on their
     own while W stays constant.
  2. The `diffuse` parameter is rendered with per-harmonic-channel
     decorrelators. The standard designs decorrelators for the speakers
     of a layout; no layout exists in the ambisonic domain. This is a
     design choice, not a requirement of the standard.
  3. Zone exclusion and channelLock are ignored: they are defined
     relative to a speaker layout and have no ambisonic equivalent.
"""

import numpy as np

from ear.core.hoa import sph_harm, from_acn, norm_SN3D, norm_N3D
from ear.core.geom import azimuth, elevation, cart
from ear.core.objectbased.extent import SpreadingPanner, PolarExtentPanner
from ear.core.objectbased.gain_calc import coord_trans
from ear.core.objectbased.renderer import InterpretObjectMetadata
from ear.core.direct_speakers.renderer import InterpretDirectSpeakersMetadata
from ear.core.renderer_common import BlockProcessingChannel
from ear.core.track_processor import TrackProcessor
from ear.core.metadata_input import (
    ObjectRenderingItem,
    DirectSpeakersRenderingItem,
    HOARenderingItem,
)
from ear.core.objectbased import decorrelate
from ear.core.convolver import OverlapSaveConvolver, VariableBlockSizeAdapter
from ear.core.delay import Delay


NORMS = {"SN3D": norm_SN3D, "N3D": norm_N3D}


def n_channels_for_order(order):
    return (order + 1) ** 2


class HOAEncoder:
    """Computes ambisonic coefficients for a direction, with extent."""

    def __init__(self, order, norm="SN3D", n_rows=37, fade_width=10.0):
        self.order = int(order)
        self.n_ch = n_channels_for_order(self.order)
        self.norm_func = NORMS[norm]
        self.fade_width = float(fade_width)

        acn = np.arange(self.n_ch)
        self._n, self._m = from_acn(acn)

        # SpreadingPanner precomputes coefficients on a uniform grid over
        # the sphere; we do the (linear) combination ourselves.
        self._spreading = SpreadingPanner(self.coeffs_for_cart, n_rows)

    def coeffs_for_cart(self, pos_cart):
        """Harmonic coefficients for a cartesian direction."""
        az = np.radians(azimuth(pos_cart))
        el = np.radians(elevation(pos_cart))
        return sph_harm(self._n, self._m, az, el, norm=self.norm_func)

    def _spread_coeffs(self, position, width, height):
        """Linear weighted average of coefficients over the spread grid."""
        weight_f = PolarExtentPanner.get_weight_func(position, width, height)
        weights = weight_f(self._spreading.panning_positions)
        total_w = np.sum(weights)
        if total_w < 1e-10:
            return self.coeffs_for_cart(position)
        return np.dot(weights, self._spreading.panning_positions_results) / total_w

    def encode(self, position, width=0.0, height=0.0, depth=0.0):
        """Coefficients for a source with extent.

        The point/spread interpolation follows the logic of
        calc_pv_spread (fade_width), but in linear form.
        """
        if depth:
            # depth widens further, equivalently to width/height
            width = np.hypot(width, depth)
            height = np.hypot(height, depth)

        amount_spread = np.interp(max(width, height), [0, self.fade_width], [0, 1])
        amount_point = 1.0 - amount_spread

        coeffs = np.zeros(self.n_ch)
        if amount_point > 1e-10:
            coeffs += amount_point * self.coeffs_for_cart(position)
        if amount_spread > 1e-10:
            w = max(width, self.fade_width / 2)
            h = max(height, self.fade_width / 2)
            coeffs += amount_spread * self._spread_coeffs(position, w, h)

        return coeffs


def direct_diffuse_split(coeffs, diffuse):
    """Splits into direct and diffuse components, conserving energy."""
    return (np.sqrt(1.0 - diffuse) * coeffs, np.sqrt(diffuse) * coeffs)


class ObjectHOARenderer:
    """Renders Object-type items in the ambisonic domain."""

    def __init__(self, encoder, block_size=512, use_diffuse=True,
                 apply_distance_gain=False):
        self.encoder = encoder
        self.n_ch = encoder.n_ch
        self.use_diffuse = use_diffuse
        self.apply_distance_gain = apply_distance_gain
        self.block_processing_channels = []

        if use_diffuse:
            filters = np.stack(
                [decorrelate.design_decorrelator_basic(i)
                 for i in range(self.n_ch)],
                axis=1,
            )
            delay_samples = (filters.shape[0] - 1) // 2
            conv = OverlapSaveConvolver(block_size, self.n_ch, filters)
            self.decorrelators = VariableBlockSizeAdapter(
                block_size, self.n_ch, conv.filter_block)
            self.overall_delay = self.decorrelators.delay(delay_samples)
            self.delays = Delay(self.n_ch, self.overall_delay)
        else:
            self.decorrelators = None
            self.overall_delay = 0
            self.delays = Delay(self.n_ch, 0)

    def _calc_gains(self, block):
        bf = block.block_format
        position = coord_trans(bf.position)

        coeffs = self.encoder.encode(
            position, bf.width, bf.height, bf.depth)
        coeffs = np.nan_to_num(coeffs) * bf.gain

        if self.apply_distance_gain:
            try:
                dist = float(bf.position.distance)
                if dist > 1e-6:
                    coeffs = coeffs / dist
            except (AttributeError, TypeError):
                pass

        if self.use_diffuse:
            direct, diffuse = direct_diffuse_split(coeffs, bf.diffuse)
            return np.concatenate((direct, diffuse))
        return np.concatenate((coeffs, np.zeros(self.n_ch)))

    def set_rendering_items(self, items):
        self.block_processing_channels = [
            (TrackProcessor(item.track_spec),
             BlockProcessingChannel(item.metadata_source,
                                    InterpretObjectMetadata(self._calc_gains)))
            for item in items
        ]

    def render(self, sample_rate, start_sample, input_samples):
        interp = np.zeros((len(input_samples), self.n_ch * 2))
        for proc, chan in self.block_processing_channels:
            track = proc.process(sample_rate, input_samples)
            chan.process(sample_rate, start_sample, track, interp)

        direct = self.delays.process(interp[:, : self.n_ch])
        if self.decorrelators is not None:
            diffuse = self.decorrelators.process(interp[:, self.n_ch:])
            return direct + diffuse
        return direct


class DirectSpeakersHOARenderer:
    """Encodes DirectSpeakers items from their nominal directions."""

    def __init__(self, encoder):
        self.encoder = encoder
        self.n_ch = encoder.n_ch
        self.block_processing_channels = []

    def _calc_gains(self, block):
        pos = block.block_format.position
        try:
            position = cart(pos.azimuth, pos.elevation, 1.0)
        except AttributeError:
            position = np.array([pos.X, pos.Y, pos.Z], dtype=float)
        return np.nan_to_num(self.encoder.encode(position))

    def set_rendering_items(self, items):
        self.block_processing_channels = [
            (TrackProcessor(item.track_spec),
             BlockProcessingChannel(item.metadata_source,
                                    InterpretDirectSpeakersMetadata(self._calc_gains)))
            for item in items
        ]

    def render(self, sample_rate, start_sample, input_samples):
        out = np.zeros((len(input_samples), self.n_ch))
        for proc, chan in self.block_processing_channels:
            track = proc.process(sample_rate, input_samples)
            chan.process(sample_rate, start_sample, track, out)
        return out


class HOAPassthrough:
    """Sums items that are already ambisonic, adapting order and normalisation."""

    def __init__(self, encoder):
        self.encoder = encoder
        self.n_ch = encoder.n_ch
        self.entries = []
        self.dropped_channels = 0

    def set_rendering_items(self, items):
        self.entries = []
        self.dropped_channels = 0
        for item in items:
            block = item.metadata_source.get_next_block()
            if block is None:
                continue
            acns, procs, scales = [], [], []
            for ts, n, m in zip(item.track_specs, block.orders, block.degrees):
                acn = n * n + n + m
                if acn >= self.n_ch:
                    self.dropped_channels += 1
                    continue
                acns.append(acn)
                procs.append(TrackProcessor(ts))
                scales.append(self._norm_scale(n, m, block.normalization))
            self.entries.append((procs, acns, scales))

    def _norm_scale(self, n, m, src_norm):
        src_f = NORMS.get(src_norm or "SN3D", norm_SN3D)
        src = float(src_f(np.array([n]), np.array([abs(m)]))[0])
        dst = float(self.encoder.norm_func(np.array([n]), np.array([abs(m)]))[0])
        return dst / src if src else 1.0

    def render(self, sample_rate, start_sample, input_samples):
        out = np.zeros((len(input_samples), self.n_ch))
        for procs, acns, scales in self.entries:
            for proc, acn, scale in zip(procs, acns, scales):
                out[:, acn] += proc.process(sample_rate, input_samples) * scale
        return out
