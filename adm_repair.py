"""
Repairs BW64/ADM files exported by the EAR Production Suite (EPS) when the
chna references audioTrackFormat elements that were never written into the
axml, or written with an ID that doesn't follow the ADM naming convention.

Diagnosis (verified on real exported files, two cases):

CASE A - a channel format defined LOCALLY in the file (e.g. an Object):
  the axml contains neither <audioTrackFormat> nor <audioStreamFormat>.
  The chna references "AC_00031001_00": the ID of an audioChannelFormat
  that REALLY IS present in the file ("AC_00031001") with "_00" stuck on
  the end - a mechanical transformation.

CASE B - a channel format from a "common definition" (e.g. the standard
5.1 pack AP_00010003, channels AC_00010001..6): per the spec, these
channel formats must NOT be defined in the file (a compliant reader loads
them from the standard catalogue). Here the chna still references
"AC_0001000N_00", which is neither a local ID nor a valid common
definition ID: the correct one is "AT_0001000N_01" - structurally
different, retrieved from the reference library's own catalogue (no
guessing at the naming pattern).

Note on v2: the first version, for case A, reused the ID already written
in the chna ("AC_00031001_00") as the ID of the new audioTrackFormat, to
avoid touching the chna. The reference library accepts this (it doesn't
validate the prefix), but REAPER rejects it on import: it expects an ID
with an "AT_" prefix, conforming to the ADM convention. This version
always generates conforming IDs (AT_/AS_, following the same pattern as
the common definitions themselves: "AT_<hex>_01") and rewrites the chna
row accordingly, for BOTH cases. It's no longer "leave the chna untouched
when possible" - it's "IDs are always correct".

If a reference doesn't fit either case, the tool reports it and writes
nothing.
"""

import re
import shutil

import lxml.etree as ET

from ear.fileio import openBw64
from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
from ear.fileio.adm.adm import ADM
from ear.fileio.adm.common_definitions import load_common_definitions


NS = "urn:ebu:metadata-schema:ebuCore_2014"


def _tag(name):
    return "{%s}%s" % (NS, name)


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _find_root_format(tree):
    for el in tree.iter():
        if _local(el.tag) == "audioFormatExtended":
            return el
    raise ValueError("axml has no audioFormatExtended: unexpected structure")


def _existing_ids(root, element_name, id_attr):
    return {el.get(id_attr) for el in root
            if _local(el.tag) == element_name and el.get(id_attr)}


_common_map_cache = None


def _common_channel_to_track_map():
    """channelFormatID -> trackFormatID, from the common definitions catalogue."""
    global _common_map_cache
    if _common_map_cache is not None:
        return _common_map_cache
    adm = ADM()
    load_common_definitions(adm)
    adm.lazy_lookup_references()
    mapping = {}
    for tf in adm.audioTrackFormats:
        sf = tf.audioStreamFormat
        cf = sf.audioChannelFormat if sf is not None else None
        if cf is not None and cf.id not in mapping:
            mapping[cf.id] = tf.id
    _common_map_cache = mapping
    return mapping


def _base_channel_id(ref):
    """'AC_00031001_00' -> 'AC_00031001'. None if the pattern doesn't match."""
    m = re.match(r"^(AC_[0-9A-Fa-f]+)_\d{2}$", ref)
    return m.group(1) if m else None


def _hex_suffix(channel_format_id):
    """'AC_00031001' -> '00031001'."""
    return channel_format_id.split("_", 1)[1]


def diagnose(path):
    """Reports what's missing and how it would be fixed, without changing anything."""
    report = {"path": path, "already_ok": [], "local_fix": [],
             "common_def_fix": [], "unresolvable": []}
    common_map = _common_channel_to_track_map()
    valid_common_track_ids = set(common_map.values())

    with openBw64(path) as f:
        axml, chna = f.axml, f.chna
        if not axml:
            report["error"] = "the file has no axml chunk"
            return report
        if not chna:
            report["error"] = "the file has no chna chunk"
            return report

        tree = ET.fromstring(axml)
        root = _find_root_format(tree)
        existing_track_fmt = _existing_ids(root, "audioTrackFormat",
                                           "audioTrackFormatID")
        existing_channel_fmt = _existing_ids(root, "audioChannelFormat",
                                             "audioChannelFormatID")

        for row in chna.audioIDs:
            ref = row.audioTrackFormatIDRef
            # already fine only if it's a conforming ID (AT_ prefix) AND
            # it actually resolves - locally or as a common definition.
            if ref.startswith("AT_") and (ref in existing_track_fmt or
                                          ref in valid_common_track_ids):
                report["already_ok"].append(ref)
                continue

            base = _base_channel_id(ref)

            if base in common_map:
                report["common_def_fix"].append({
                    "row_uid": row.audioTrackUID,
                    "wrong_ref": ref,
                    "correct_ref": common_map[base],
                    "channel_format": base,
                })
            elif base and base in existing_channel_fmt:
                report["local_fix"].append({
                    "row_uid": row.audioTrackUID,
                    "wrong_ref": ref,
                    "channel_format": base,
                    "pack_format": row.audioPackFormatIDRef,
                })
            else:
                report["unresolvable"].append(ref)
    return report


def _find_audio_track_uid(root, uid):
    for el in root:
        if _local(el.tag) == "audioTrackUID" and el.get("UID") == uid:
            return el
    return None


def _strip_direct_channel_ref(track_uid_el):
    """Removes direct <audioChannelFormatIDRef>/<audioPackFormatIDRef>
    children of <audioTrackUID>: this is the simplified BS.2076-2 style,
    which the EPS ALWAYS writes, even when (as here) the classic 2076-1
    style is needed instead, with an audioTrackFormat resolvable from the
    chna. Having both schemes on the same track is exactly what REAPER
    flags as "mutually-exclusive AudioTrackUid references": it isn't
    extra data, it's two ways of saying the same thing that contradict
    each other.
    """
    removed = False
    for child in list(track_uid_el):
        if _local(child.tag) in ("audioChannelFormatIDRef",
                                 "audioPackFormatIDRef"):
            track_uid_el.remove(child)
            removed = True
    return removed


def _free_suffix(existing_track_ids, hex_part):
    """'AT_00031001_01', _02, ... until one is free."""
    n = 1
    while True:
        candidate = "AT_%s_%02d" % (hex_part, n)
        if candidate not in existing_track_ids:
            return candidate
        n += 1


def repair_file(path, output_path, log=None):
    """Writes a repaired copy of the file. Does not touch the original."""
    def _log(msg):
        if log:
            log(msg)

    report = diagnose(path)
    if report.get("error"):
        raise RuntimeError(report["error"])
    if report["unresolvable"]:
        raise RuntimeError(
            "References that don't trace back to either a local channel "
            "format or a common definition: %s. This isn't the fault this "
            "tool was written for: not proceeding, to avoid writing an "
            "incorrect file." % report["unresolvable"])

    n_fixes = len(report["local_fix"]) + len(report["common_def_fix"])
    if n_fixes == 0:
        _log("No repair needed: the file is already consistent.")
        shutil.copyfile(path, output_path)
        return report

    _log("To fix: %d local, %d from common definitions (already fine: %d)"
         % (len(report["local_fix"]), len(report["common_def_fix"]),
            len(report["already_ok"])))

    with openBw64(path) as f:
        axml, chna = f.axml, f.chna
        sample_rate, channels, bitdepth = f.sampleRate, f.channels, f.bitdepth
        n_samples = len(f)

        # --- axml: missing local elements, with conforming IDs (AT_/AS_) ---
        tree = ET.fromstring(axml)
        root = _find_root_format(tree)
        existing_track_ids = _existing_ids(root, "audioTrackFormat",
                                           "audioTrackFormatID")
        existing_stream_ids = _existing_ids(root, "audioStreamFormat",
                                            "audioStreamFormatID")

        # row_uid -> correct ID to write into the chna (for BOTH cases)
        chna_fix_by_uid = {}

        for item in report["local_fix"]:
            hex_part = _hex_suffix(item["channel_format"])
            stream_id = "AS_%s" % hex_part
            if stream_id in existing_stream_ids:
                # unlikely collision, handled anyway
                stream_id = "AS_%s_2" % hex_part
            existing_stream_ids.add(stream_id)

            track_id = _free_suffix(existing_track_ids, hex_part)
            existing_track_ids.add(track_id)

            stream_el = ET.SubElement(root, _tag("audioStreamFormat"))
            stream_el.set("audioStreamFormatID", stream_id)
            stream_el.set("audioStreamFormatName", "PCM_" + item["channel_format"])
            stream_el.set("formatLabel", "0001")
            stream_el.set("formatDefinition", "PCM")
            ET.SubElement(stream_el, _tag("audioChannelFormatIDRef")
                         ).text = item["channel_format"]
            ET.SubElement(stream_el, _tag("audioTrackFormatIDRef")
                         ).text = track_id

            track_el = ET.SubElement(root, _tag("audioTrackFormat"))
            track_el.set("audioTrackFormatID", track_id)
            track_el.set("audioTrackFormatName", "PCM_" + item["channel_format"])
            track_el.set("formatLabel", "0001")
            track_el.set("formatDefinition", "PCM")
            ET.SubElement(track_el, _tag("audioStreamFormatIDRef")).text = stream_id

            chna_fix_by_uid[item["row_uid"]] = track_id
            uid_el = _find_audio_track_uid(root, item["row_uid"])
            if uid_el is not None:
                _strip_direct_channel_ref(uid_el)
            _log("  [local] %s: %s -> %s  (+ %s)" %
                 (item["row_uid"], item["wrong_ref"], track_id, stream_id))

        for item in report["common_def_fix"]:
            chna_fix_by_uid[item["row_uid"]] = item["correct_ref"]
            uid_el = _find_audio_track_uid(root, item["row_uid"])
            if uid_el is not None:
                _strip_direct_channel_ref(uid_el)
            _log("  [common definition] %s: %s -> %s" %
                 (item["row_uid"], item["wrong_ref"], item["correct_ref"]))

        new_axml = ET.tostring(tree, xml_declaration=True, encoding="utf-8",
                               pretty_print=True)

        # --- chna: rewritten for BOTH cases with the correct ID ---
        new_rows = []
        for row in chna.audioIDs:
            correct = chna_fix_by_uid.get(row.audioTrackUID)
            if correct:
                new_rows.append(AudioID(
                    trackIndex=row.trackIndex,
                    audioTrackUID=row.audioTrackUID,
                    audioTrackFormatIDRef=correct,
                    audioPackFormatIDRef=row.audioPackFormatIDRef))
            else:
                new_rows.append(row)
        new_chna = ChnaChunk(audioIDs=new_rows)

        fmt = FormatInfoChunk(formatTag=1, channelCount=channels,
                              sampleRate=sample_rate, bitsPerSample=bitdepth)

        with openBw64(output_path, "w", formatInfo=fmt, axml=new_axml,
                     chna=new_chna) as out:
            block_size = 65536
            done = 0
            while done < n_samples:
                n = min(block_size, n_samples - done)
                out.write(f.read(n))
                done += n

    _log("Written: %s" % output_path)
    return report


def verify(output_path, log=None):
    """Reopens the repaired file with the full ADM reader, for confirmation."""
    from ear.fileio import openBw64Adm
    from ear.core.select_items import select_rendering_items
    with openBw64Adm(output_path) as f:
        items = select_rendering_items(f.adm)
    if log:
        log("Verification: the repaired file reads correctly, %d items found."
            % len(items))
    return len(items)
