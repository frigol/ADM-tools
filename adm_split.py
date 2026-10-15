"""
Estrae una porzione temporale ("region") da un file ADM/BW64 gia'
esportato, come brano o atto autonomo.

Perche' non e' un taglio audio soltanto: per ogni oggetto i tempi di
blocco (rtime/duration) sono relativi all'inizio dell'oggetto, che a sua
volta e' relativo all'inizio del file (verificato nel codice della
libreria `ear`: block_start = object_start + rtime). Estrarre una region
richiede quindi tenere solo i blocchi che la toccano, accorciare quelli a
cavallo del confine, e ricalcolare tutti i tempi rispetto al nuovo zero.
La logica di calcolo (clip_object_timing) e' stata verificata a mano su
tre casi — sovrapposizione parziale, oggetto fuori regione, taglio esatto
su un confine — prima di essere usata qui.

Gli oggetti che non si sovrappongono affatto alla region vengono rimossi
per intero, canale audio compreso, non lasciati come canali silenziosi:
pensato per chi lavora con molte take multitraccia nello stesso progetto.

Implementazione: invece di editare il testo XML del file sorgente a mano
(rischioso: un campo dimenticato in un blocco copiato a meta' e' un
errore silenzioso, non un crash), il file viene RICOSTRUITO da zero con
ADMBuilder a partire dal modello a oggetti che la libreria `ear` fornisce
in lettura. I singoli blockFormat vengono clonati con `attr.evolve`,
cambiando solo rtime/duration: ogni altro campo (posizione, width, gain,
diffuse...) e' garantito identico all'originale perche' non viene mai
ricopiato a mano.
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
# Lettura del CSV region/marker esportato da REAPER (Region/Marker Manager)
# --------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")


def _parse_time(s):
    """'0:00:12.000000' o '12.000000' -> secondi (float).

    Il primo formato e' quello di REAPER (Region/Marker Manager, con il
    righello impostato su Time (seconds)); il secondo e' quello scritto
    direttamente da export_and_split.lua (secondi semplici, via API,
    senza passare dal Manager). Entrambi i CSV usano la stessa
    intestazione "#,Name,Start,End,Length", quindi vanno accettati
    entrambi qui piuttosto che vincolare lo script Lua a un formato che
    poi deve essere riformattato. None se vuoto (marker puntuale).
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
            "Formato tempo non riconosciuto: %r. Atteso H:MM:SS.ffffff "
            "(REAPER, Region/Marker Manager con righello in Time "
            "(seconds)) oppure secondi semplici (es. 12.000000)." % s)


def read_regions_csv(path):
    """Legge il CSV esportato da Region/Marker Manager.

    Ritorna una lista di dict {id, name, start, end} in SECONDI — i
    marker puntuali (senza End) sono scartati: non definiscono un
    intervallo da estrarre.
    """
    with open(path, encoding="utf-8-sig") as f:
        lines = [l.rstrip("\n").rstrip("\r") for l in f if l.strip()]
    if not lines:
        raise ValueError("File vuoto")

    header = [h.strip() for h in lines[0].split(",")]
    try:
        idx_id = header.index("#")
        idx_name = header.index("Name")
        idx_start = header.index("Start")
        idx_end = header.index("End")
    except ValueError:
        raise ValueError(
            "Intestazione CSV inattesa: %r. Atteso qualcosa come "
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
            continue  # marker puntuale, non una region
        regions.append({"id": row_id, "name": name or row_id,
                        "start": start, "end": end})
    return regions


# --------------------------------------------------------------------------
# Taglio/ribasamento dei tempi
# --------------------------------------------------------------------------

def clip_object_timing(obj_start, obj_duration, blocks, t0, t1):
    """blocks: lista di (rtime, duration) Fraction, relativi a obj_start.

    Ritorna (new_obj_duration, [(new_rtime, new_duration), ...]) con i
    tempi gia' ribasati sul nuovo zero (inizio della region), oppure None
    se l'oggetto non si sovrappone a [t0, t1) per nulla.
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
# Splitter principale
# --------------------------------------------------------------------------

def split_region(input_path, output_path, region_start_s, region_end_s,
                 log=None):
    """Estrae [region_start_s, region_end_s) da input_path in output_path."""

    def _log(msg):
        if log:
            log(msg)

    t0 = Fraction(region_start_s).limit_denominator(10_000_000)
    t1 = Fraction(region_end_s).limit_denominator(10_000_000)
    if t1 <= t0:
        raise ValueError("La fine della region deve essere dopo l'inizio")

    with openBw64Adm(input_path) as reader:
        sample_rate = reader.sampleRate
        bitdepth = reader.bitdepth
        n_samples_total = len(reader._bw64)
        file_duration = Fraction(n_samples_total, sample_rate)

        items = select_rendering_items(reader.adm)

        builder = ADMBuilder()
        builder.create_programme(audioProgrammeName="split")
        builder.create_content(audioContentName="c")

        # canale audio sorgente (indice 0-based) -> nuovo indice (0-based)
        # nell'ordine in cui li aggiungiamo al builder
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

            # raccolgo TUTTI i blocchi di questo channel format (non solo
            # il primo): riavvolgo il metadata_source per scorrerli tutti
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
                _log("  ! tipo non gestito da questo splitter (%s), "
                     "oggetto scartato" % type(item).__name__)
                source_channels.pop()
                n_dropped += 1
                continue

            adm = builder.adm
            adm.audioObjects[-1].start = Fraction(0)
            adm.audioObjects[-1].duration = new_obj_duration
            n_kept += 1

        _log("Canali mantenuti: %d, rimossi: %d" % (n_kept, n_dropped))

        if n_kept == 0:
            raise ValueError(
                "Nessun oggetto si sovrappone a questa region "
                "(%.3f-%.3f s): il file risulterebbe vuoto. Controlla i "
                "tempi della region rispetto alla durata del file "
                "sorgente." % (float(t0), float(t1)))

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

    _log("Scritto: %s" % output_path)
    return {"kept": n_kept, "dropped": n_dropped}


def verify(output_path, log=None):
    """Riapre il file con il lettore ADM completo, per conferma."""
    with openBw64Adm(output_path) as f:
        items = select_rendering_items(f.adm)
    if log:
        log("Verifica: %d item, %d canali audio." %
            (len(items), f.channels))
    return len(items)
