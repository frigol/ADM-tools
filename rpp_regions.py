"""
Reads regions (not point markers) directly from a REAPER project file
(.rpp), without needing REAPER open or a manual export from the
Region/Marker Manager.

Format verified on real projects (20260920 Prova ADM extract.RPP and
others, with mixed point markers and regions):

    MARKER <id> <pos> "<name>" <field4> [<color> <flag> {<guid>} <e1> <e2>]

Every MARKER line is either "long" (11 tokens: id, pos, name, field4,
color, flag, 'B', {guid}, e1, e2) or "short" (5 tokens: id, pos, empty
name, field4). A region is the PAIR [long line with id=N carrying the
name and start] + [the next short line with the same id=N carrying the
end]; a point marker is always a long line with NO matching short line.

The fourth field (after the name) is NOT a reliable "is a region" flag:
it was observed as 0 on some point markers but also as values like 8 on
others in the same role - probably a display number or a flag unrelated
to being a region. The only verified stable signal is therefore
structural (line length + pairing by id), not the value of a specific
field - which is why this parser ignores that field and relies only on
the shape of the lines.

Point-marker and region ids share the same numeric space (a real
project had both a point marker and a region with id=1): because of
this, the pairing below only matches a pending long line with the
immediately following short line for the same id, not a global search.

Positions in the project file are always in seconds, regardless of the
ruler's display unit in the interface.
"""

import shlex


def read_regions_from_rpp(path):
    """Returns a list of {id, name, start, end} dicts in seconds, one
    per region, sorted by start time. Point markers (long lines never
    followed by a short line with the same id) are ignored.
    """
    pending = {}  # REAPER id (string) -> {"name": ..., "start": ...}
    regions = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped.startswith("MARKER "):
                continue
            try:
                tokens = shlex.split(stripped)
            except ValueError:
                continue  # malformed quoting, line defensively skipped

            if len(tokens) >= 9:
                # "Long" line: name + start of a region, or a point
                # marker (we'll only know which once/if a short line
                # with the same id arrives).
                _, rid, pos_s, name = tokens[:4]
                try:
                    pos = float(pos_s)
                except ValueError:
                    continue
                pending[rid] = {"name": name, "start": pos}
            elif len(tokens) == 5:
                # "Short" line: end of a region. A point marker never
                # produces a line of this shape.
                _, rid, pos_s, _name, _field4 = tokens
                if rid not in pending:
                    continue  # orphan end line, defensively skipped
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
            # other lengths: unexpected shape, line defensively skipped

    # Long lines never paired stay in `pending`: they're point markers,
    # not regions, and are dropped here (never added above).
    regions.sort(key=lambda r: r["start"])
    return regions
