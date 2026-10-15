"""
Legge le region (non i marker puntuali) direttamente da un file progetto
REAPER (.rpp), senza bisogno di REAPER aperto ne' di un export manuale
dal Region/Marker Manager.

Formato verificato su progetti reali (20260920 Prova ADM extract.RPP e
altri, con marker puntuali e region miste):

    MARKER <id> <pos> "<nome>" <campo4> [<colore> <flag> {<guid>} <e1> <e2>]

Ogni riga MARKER e' o "lunga" (11 token: id, pos, nome, campo4, colore,
flag, 'B', {guid}, e1, e2) o "corta" (5 token: id, pos, nome vuoto,
campo4). Una region e' la COPPIA [riga lunga con id=N che porta nome e
inizio] + [riga corta successiva con lo stesso id=N che porta la fine];
un marker puntuale e' sempre una riga lunga SENZA una riga corta
corrispondente.

Il quarto campo (dopo il nome) NON e' un affidabile flag "e' una
region": e' stato osservato a 0 su alcuni marker puntuali ma anche a
valori come 8 su altri nello stesso ruolo — probabilmente un numero di
visualizzazione o un flag non legato alla region-ita'. L'unico segnale
verificato stabile e' quindi strutturale (lunghezza riga + pairing per
id), non il valore di un campo specifico: per questo questo parser
ignora quel campo e si basa solo sulla forma delle righe.

Gli id di marker puntuali e region condividono lo stesso spazio
numerico (un progetto reale aveva sia un marker puntuale sia una
region con id=1): per questo il pairing qui sotto associa SOLO una
riga lunga in attesa con la riga corta immediatamente successiva per
lo stesso id, non con una ricerca globale.

Le posizioni nel file progetto sono sempre in secondi, a prescindere
dall'unita' di misura del righello mostrata nell'interfaccia.
"""

import shlex


def read_regions_from_rpp(path):
    """Ritorna una lista di dict {id, name, start, end} in secondi,
    una per region, ordinata per tempo di inizio. I marker puntuali
    (righe lunghe mai seguite da una riga corta con lo stesso id) sono
    ignorati.
    """
    pending = {}  # id REAPER (stringa) -> {"name": ..., "start": ...}
    regions = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped.startswith("MARKER "):
                continue
            try:
                tokens = shlex.split(stripped)
            except ValueError:
                continue  # quoting malformato, riga scartata difensivamente

            if len(tokens) >= 9:
                # Riga "lunga": nome + inizio di una region, oppure un
                # marker puntuale (lo sapremo solo se/quando arriva una
                # riga corta con lo stesso id).
                _, rid, pos_s, name = tokens[:4]
                try:
                    pos = float(pos_s)
                except ValueError:
                    continue
                pending[rid] = {"name": name, "start": pos}
            elif len(tokens) == 5:
                # Riga "corta": fine di una region. Un marker puntuale
                # non produce mai una riga di questa forma.
                _, rid, pos_s, _name, _campo4 = tokens
                if rid not in pending:
                    continue  # riga di fine orfana, scartata difensivamente
                try:
                    pos = float(pos_s)
                except ValueError:
                    continue
                start = pending.pop(rid)
                regions.append({
                    "id": rid,
                    "name": start["name"] or ("R%s" % rid),
                    "start": start["start"],
                    "end": pos,
                })
            # altre lunghezze: forma inattesa, riga ignorata difensivamente

    # Righe lunghe mai accoppiate restano in `pending`: sono marker
    # puntuali, non region, e vengono scartate qui (non aggiunte sopra).
    regions.sort(key=lambda r: r["start"])
    return regions
