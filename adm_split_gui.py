#!/usr/bin/env python3
"""
ADM Split by Region — extracts a standalone file per region from an
already-exported ADM/BW64 file, reading regions from a REAPER project
file (.rpp, read directly, no REAPER needed open) or from a CSV
(Region/Marker Manager export, or the one written by export_and_split.lua).

Entirely after-the-fact: it doesn't touch render settings, and works on
any already-exported ADM file, at any time, regardless of when or how it
was rendered.

Usage:
    python3 adm_split_gui.py        launches the interface
"""

import os
import sys
import queue
import tempfile
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adm_split import split_region, verify, read_regions_csv
from adm_repair import repair_file
from rpp_regions import read_regions_from_rpp


# A broken-reference ADM export (a known EAR Production Suite export
# fault - see adm_repair.py / the README) always fails here with this
# exact KeyError message from the `ear` library's reference resolver.
# repair_file() is a no-op on an already-correct file (just copies it),
# so it's run up front on every source unconditionally rather than only
# after a failed split - cheaper than explaining the error and asking
# for a manual repair-then-retry round trip.
# Caught specifically so the error tells people what to actually do
# (run Repair in ADM to AmbiX) instead of just a raw traceback.
_BROKEN_REFERENCE_MARKER = "Unknown element requested"


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("ADM Split by Region")
    # Real size is set at the end, once every widget exists, from Tk's
    # own computed required size (root.winfo_reqheight()) rather than a
    # hardcoded guess - a fixed WxH here was cutting off the bottom
    # buttons on setups where the font/widget metrics render taller
    # than whatever this was last measured on.
    root.geometry("760x100")

    state = {"running": False, "stop": False}
    msg_q = queue.Queue()
    regions = []

    # ---- source file -------------------------------------------------
    frm_in = ttk.LabelFrame(root, text="ADM file to split", padding=8)
    frm_in.pack(fill="x", padx=10, pady=(10, 4))
    adm_path = tk.StringVar(value="")
    ttk.Entry(frm_in, textvariable=adm_path).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 6))

    def choose_adm():
        p = filedialog.askopenfilename(
            title="Select the already-exported ADM/BW64 file",
            filetypes=[("BW64/WAV", "*.wav *.bw64 *.WAV"), ("All files", "*.*")])
        if p:
            adm_path.set(p)

    ttk.Button(frm_in, text="Browse...", command=choose_adm).pack(side="left")

    # ---- regions: REAPER project (autonomous) or CSV (manual) --------
    frm_csv = ttk.LabelFrame(
        root, text="Regions: REAPER project (.rpp, read directly, no "
                   "manual step) or CSV exported from the "
                   "Region/Marker Manager", padding=8)
    frm_csv.pack(fill="x", padx=10, pady=4)
    csv_path = tk.StringVar(value="")
    ttk.Entry(frm_csv, textvariable=csv_path).pack(side="left", fill="x",
                                                    expand=True, padx=(0, 6))

    def load_regions_from(path):
        regions.clear()
        tree.delete(*tree.get_children())
        if not path or not os.path.isfile(path):
            return
        try:
            if path.lower().endswith((".rpp",)):
                found = read_regions_from_rpp(path)
            else:
                found = read_regions_csv(path)
            if not found:
                messagebox.showinfo(
                    "No regions found",
                    "No regions (only point markers, or an empty file) "
                    "in:\n%s" % path)
            for r in found:
                regions.append(r)
                tree.insert("", "end", values=(r["id"], r["name"],
                                               "%.3f" % r["start"],
                                               "%.3f" % r["end"]))
            for iid in tree.get_children():
                tree.selection_add(iid)
        except Exception as exc:
            messagebox.showerror("Error reading regions",
                                 "%s\n\n%s" % (path, exc))

    def choose_rpp():
        p = filedialog.askopenfilename(
            title="Select the REAPER project (.rpp)",
            filetypes=[("REAPER project", "*.rpp *.RPP"), ("All files", "*.*")])
        if p:
            csv_path.set(p)
            load_regions_from(p)

    def choose_csv():
        p = filedialog.askopenfilename(
            title="Select the regions CSV",
            filetypes=[("CSV", "*.csv *.txt"), ("All files", "*.*")])
        if p:
            csv_path.set(p)
            load_regions_from(p)

    ttk.Button(frm_csv, text=".rpp project...",
              command=choose_rpp).pack(side="left")
    ttk.Button(frm_csv, text="CSV...",
              command=choose_csv).pack(side="left", padx=(4, 0))

    # ---- detected regions ----------------------------------------------
    frm_rg = ttk.LabelFrame(
        root, text="Regions found (uncheck any to exclude)",
        padding=8)
    frm_rg.pack(fill="both", expand=True, padx=10, pady=4)
    cols = ("id", "name", "start_s", "end_s")
    tree = ttk.Treeview(frm_rg, columns=cols, show="headings", height=8,
                        selectmode="extended")
    widths = (50, 300, 100, 100)
    for c, w in zip(cols, widths):
        tree.heading(c, text=c.capitalize())
        tree.column(c, width=w, anchor="w")
    tree.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(frm_rg, orient="vertical", command=tree.yview)
    sb.pack(side="right", fill="y")
    tree.configure(yscrollcommand=sb.set)

    # ---- destination -----------------------------------------------------
    frm_out = ttk.LabelFrame(root, text="Destination", padding=8)
    frm_out.pack(fill="x", padx=10, pady=4)
    out_dir = tk.StringVar(value="")
    ttk.Entry(frm_out, textvariable=out_dir).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 6))

    def choose_dir():
        d = filedialog.askdirectory(title="Destination folder")
        if d:
            out_dir.set(d)

    ttk.Button(frm_out, text="Browse...", command=choose_dir).pack(side="left")
    ttk.Label(frm_out, text="(empty = next to the ADM file)").pack(side="left", padx=6)

    # ---- log and progress -------------------------------------------------
    frm_log = ttk.LabelFrame(root, text="Log", padding=6)
    frm_log.pack(fill="both", expand=True, padx=10, pady=4)
    log_box = tk.Text(frm_log, height=8, wrap="word")
    log_box.pack(fill="both", expand=True)
    log_box.configure(state="disabled")

    def log(msg):
        msg_q.put(("log", msg))

    frm_run = ttk.Frame(root)
    frm_run.pack(fill="x", padx=10, pady=(4, 10))
    btn_start = ttk.Button(frm_run, text="Split")
    btn_start.pack(side="left")

    def start():
        if state["running"]:
            return
        src = adm_path.get()
        if not src or not os.path.isfile(src):
            messagebox.showinfo("Missing file", "Select the ADM file to split.")
            return
        selected_ids = set()
        for iid in tree.selection():
            idx = tree.index(iid)
            selected_ids.add(regions[idx]["id"])
        selected = [r for r in regions if r["id"] in selected_ids]
        if not selected:
            messagebox.showinfo("No regions",
                                "Load a CSV and select at least one region.")
            return

        dst_dir = out_dir.get() or os.path.dirname(src)
        base = os.path.splitext(os.path.basename(src))[0]

        state["running"] = True
        btn_start.configure(state="disabled")

        def worker():
            any_error = False
            saw_broken_reference = False

            # Repair once, up front, into a throwaway temp copy, reused
            # for every selected region below - not once per region,
            # and not left behind in the user's folder afterwards. A
            # file that's already correct passes through unchanged (just
            # copied), so this is always safe to run, not just when a
            # split would otherwise fail.
            split_src = src
            tmp_repaired = None
            try:
                fd, tmp_repaired = tempfile.mkstemp(suffix=".wav",
                                                     prefix="adm_repaired_")
                os.close(fd)
                log("Checking/repairing references...")
                repair_file(src, tmp_repaired, log=lambda m: log("  " + m))
                split_src = tmp_repaired
            except Exception:
                log("  Repair step failed, splitting the original file "
                    "as-is:\n" + traceback.format_exc())
                if tmp_repaired and os.path.exists(tmp_repaired):
                    os.remove(tmp_repaired)
                tmp_repaired = None

            try:
                for r in selected:
                    safe_name = "".join(
                        c if c.isalnum() or c in "-_ " else "_"
                        for c in r["name"]).strip() or r["id"]
                    out = os.path.join(dst_dir, "%s_%s.wav" % (base, safe_name))
                    log("%s  [%.3f s - %.3f s] -> %s" %
                        (r["name"], r["start"], r["end"], os.path.basename(out)))
                    try:
                        split_region(split_src, out, r["start"], r["end"],
                                     log=lambda m: log("  " + m))
                        verify(out, log=lambda m: log("  " + m))
                    except Exception as exc:
                        if _BROKEN_REFERENCE_MARKER in str(exc):
                            saw_broken_reference = True
                            log("  ERROR: this ADM file has broken references "
                                "that the automatic repair step couldn't "
                                "resolve (see the repair log above for which "
                                "ones) - the file may use a reference pattern "
                                "this tool doesn't know how to fix.")
                        else:
                            log("  ERROR:\n" + traceback.format_exc())
                        any_error = True
            finally:
                if tmp_repaired and os.path.exists(tmp_repaired):
                    os.remove(tmp_repaired)

            if saw_broken_reference:
                log("Finished with errors - see the repair note above.")
            else:
                log("Finished with errors." if any_error else "Done.")
            msg_q.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    btn_start.configure(command=start)

    def pump():
        try:
            while True:
                kind, payload = msg_q.get_nowait()
                if kind == "log":
                    log_box.configure(state="normal")
                    log_box.insert("end", payload + "\n")
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "done":
                    state["running"] = False
                    btn_start.configure(state="normal")
        except queue.Empty:
            pass
        root.after(80, pump)

    # Size the window to what it actually needs now that every widget
    # exists, instead of the small placeholder geometry set at the top -
    # update_idletasks() forces Tk to compute real layout sizes first
    # (winfo_reqheight() is 1 otherwise). minsize() stops a later manual
    # resize from accidentally hiding the bottom button again.
    root.update_idletasks()
    req_w, req_h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry("%dx%d" % (max(req_w, 760), req_h))
    root.minsize(max(req_w, 760), req_h)

    pump()
    root.mainloop()


if __name__ == "__main__":
    try:
        run_gui()
    except ImportError:
        print("tkinter not available; use 'python3 adm2ambix.py --split ...'")
        sys.exit(1)
