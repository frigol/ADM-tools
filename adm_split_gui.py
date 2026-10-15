#!/usr/bin/env python3
"""
ADM Split per Region — estrae da un file ADM/BW64 gia' esportato un file
standalone per ciascuna region del progetto REAPER (letta direttamente
dal file .rpp, senza REAPER aperto) o di un CSV (Region/Marker Manager,
o quello scritto da export_and_split.lua).

Interamente a posteriori: non tocca le impostazioni di render, lavora su
qualunque file ADM gia' esportato, in qualunque momento, a prescindere
da quando o come e' stato renderizzato.

Uso:
    python3 adm_split_gui.py        avvia l'interfaccia
"""

import os
import sys
import queue
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adm_split import split_region, verify, read_regions_csv
from rpp_regions import read_regions_from_rpp


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("ADM Split per Region")
    root.geometry("760x560")

    state = {"running": False, "stop": False}
    msg_q = queue.Queue()
    regions = []

    # ---- file sorgente ---------------------------------------------------
    frm_in = ttk.LabelFrame(root, text="File ADM da dividere", padding=8)
    frm_in.pack(fill="x", padx=10, pady=(10, 4))
    adm_path = tk.StringVar(value="")
    ttk.Entry(frm_in, textvariable=adm_path).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 6))

    def choose_adm():
        p = filedialog.askopenfilename(
            title="Seleziona il file ADM/BW64 gia' esportato",
            filetypes=[("BW64/WAV", "*.wav *.bw64 *.WAV"), ("Tutti", "*.*")])
        if p:
            adm_path.set(p)

    ttk.Button(frm_in, text="Sfoglia...", command=choose_adm).pack(side="left")

    # ---- region: progetto REAPER (autonomo) o CSV (manuale) --------------
    frm_csv = ttk.LabelFrame(
        root, text="Region: progetto REAPER (.rpp, letto direttamente, "
                   "nessun passaggio manuale) oppure CSV esportato dal "
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
                    "Nessuna region trovata",
                    "Nessuna region (solo marker puntuali, o file vuoto) "
                    "in:\n%s" % path)
            for r in found:
                regions.append(r)
                tree.insert("", "end", values=(r["id"], r["name"],
                                               "%.3f" % r["start"],
                                               "%.3f" % r["end"]))
            for iid in tree.get_children():
                tree.selection_add(iid)
        except Exception as exc:
            messagebox.showerror("Errore nella lettura delle region",
                                 "%s\n\n%s" % (path, exc))

    def choose_rpp():
        p = filedialog.askopenfilename(
            title="Seleziona il progetto REAPER (.rpp)",
            filetypes=[("Progetto REAPER", "*.rpp *.RPP"), ("Tutti", "*.*")])
        if p:
            csv_path.set(p)
            load_regions_from(p)

    def choose_csv():
        p = filedialog.askopenfilename(
            title="Seleziona il CSV delle region",
            filetypes=[("CSV", "*.csv *.txt"), ("Tutti", "*.*")])
        if p:
            csv_path.set(p)
            load_regions_from(p)

    ttk.Button(frm_csv, text="Progetto .rpp...",
              command=choose_rpp).pack(side="left")
    ttk.Button(frm_csv, text="CSV...",
              command=choose_csv).pack(side="left", padx=(4, 0))

    # ---- lista region rilevate -------------------------------------------
    frm_rg = ttk.LabelFrame(
        root, text="Region trovate (deseleziona quelle da escludere)",
        padding=8)
    frm_rg.pack(fill="both", expand=True, padx=10, pady=4)
    cols = ("id", "nome", "inizio_s", "fine_s")
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

    # ---- destinazione ------------------------------------------------------
    frm_out = ttk.LabelFrame(root, text="Destinazione", padding=8)
    frm_out.pack(fill="x", padx=10, pady=4)
    out_dir = tk.StringVar(value="")
    ttk.Entry(frm_out, textvariable=out_dir).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 6))

    def choose_dir():
        d = filedialog.askdirectory(title="Cartella di destinazione")
        if d:
            out_dir.set(d)

    ttk.Button(frm_out, text="Sfoglia...", command=choose_dir).pack(side="left")
    ttk.Label(frm_out, text="(vuoto = accanto al file ADM)").pack(side="left", padx=6)

    # ---- log e avanzamento -------------------------------------------------
    frm_log = ttk.LabelFrame(root, text="Registro", padding=6)
    frm_log.pack(fill="both", expand=True, padx=10, pady=4)
    log_box = tk.Text(frm_log, height=8, wrap="word")
    log_box.pack(fill="both", expand=True)
    log_box.configure(state="disabled")

    def log(msg):
        msg_q.put(("log", msg))

    frm_run = ttk.Frame(root)
    frm_run.pack(fill="x", padx=10, pady=(4, 10))
    btn_start = ttk.Button(frm_run, text="Dividi")
    btn_start.pack(side="left")

    def start():
        if state["running"]:
            return
        src = adm_path.get()
        if not src or not os.path.isfile(src):
            messagebox.showinfo("File mancante", "Seleziona il file ADM da dividere.")
            return
        selected_ids = set()
        for iid in tree.selection():
            idx = tree.index(iid)
            selected_ids.add(regions[idx]["id"])
        selected = [r for r in regions if r["id"] in selected_ids]
        if not selected:
            messagebox.showinfo("Nessuna region",
                                "Carica un CSV e seleziona almeno una region.")
            return

        dst_dir = out_dir.get() or os.path.dirname(src)
        base = os.path.splitext(os.path.basename(src))[0]

        state["running"] = True
        btn_start.configure(state="disabled")

        def worker():
            any_error = False
            for r in selected:
                safe_name = "".join(
                    c if c.isalnum() or c in "-_ " else "_"
                    for c in r["name"]).strip() or r["id"]
                out = os.path.join(dst_dir, "%s_%s.wav" % (base, safe_name))
                log("%s  [%.3f s - %.3f s] -> %s" %
                    (r["name"], r["start"], r["end"], os.path.basename(out)))
                try:
                    split_region(src, out, r["start"], r["end"],
                                 log=lambda m: log("  " + m))
                    verify(out, log=lambda m: log("  " + m))
                except Exception:
                    log("  ERRORE:\n" + traceback.format_exc())
                    any_error = True
            log("Finito con errori." if any_error else "Fatto.")
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

    pump()
    root.mainloop()


if __name__ == "__main__":
    try:
        run_gui()
    except ImportError:
        print("tkinter non disponibile; usa 'python3 adm2ambix.py --split ...'")
        sys.exit(1)
