#!/usr/bin/env python3
"""
ADM to AmbiX — converte scene ADM/BW64 in file ambisonici.

Interfaccia grafica: coda di file, cartella di destinazione, ordine di
uscita e opzioni di codifica.

Uso:
    python3 adm2ambix.py            avvia l'interfaccia
    python3 adm2ambix.py --cli ...  modalita' riga di comando

Requisiti: python3, numpy, ear  (pip install ear)
"""

import os
import sys
import queue
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import Settings, convert_batch, inspect_file, n_channels_for_order


ORDERS = [1, 2, 3, 4, 5, 6, 7]
SUFFIX = "_ambix"


def default_output(src, out_dir, order):
    base = os.path.splitext(os.path.basename(src))[0]
    name = "%s%s_%do.wav" % (base, SUFFIX, order)
    return os.path.join(out_dir or os.path.dirname(src), name)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("ADM to AmbiX")
    root.geometry("860x640")

    state = {"running": False, "stop": False}
    msg_q = queue.Queue()

    # ---- coda dei file -------------------------------------------------
    frm_files = ttk.LabelFrame(root, text="File da convertire", padding=8)
    frm_files.pack(fill="both", expand=True, padx=10, pady=(10, 4))

    cols = ("file", "sr", "ch", "durata", "contenuto")
    tree = ttk.Treeview(frm_files, columns=cols, show="headings", height=8)
    widths = (300, 70, 50, 80, 240)
    for c, w in zip(cols, widths):
        tree.heading(c, text=c.capitalize())
        tree.column(c, width=w, anchor="w")
    tree.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(frm_files, orient="vertical", command=tree.yview)
    sb.pack(side="right", fill="y")
    tree.configure(yscrollcommand=sb.set)

    paths = []

    def add_path(p):
        """Aggiunge un percorso alla coda (se non gia' presente) e ne
        avvia la lettura in background. Riusata sia da 'Aggiungi file'
        sia dopo una riparazione, per accodare l'output."""
        if p in paths:
            return
        paths.append(p)
        tree.insert("", "end", values=(os.path.basename(p), "", "", "",
                                       "lettura in corso..."))
        threading.Thread(target=scan_one, args=(len(paths) - 1,),
                         daemon=True).start()

    def scan_one(idx):
        p = paths[idx]
        try:
            info = inspect_file(p)
            desc = []
            if info["objects"]:
                desc.append("%d oggetti" % info["objects"])
            if info["direct_speakers"]:
                desc.append("%d direct spk" % info["direct_speakers"])
            if info["hoa"]:
                o = max(info["hoa_orders"]) if info["hoa_orders"] else "?"
                desc.append("%d HOA (ord. %s)" % (info["hoa"], o))
            if info["errors"]:
                desc.append("! validazione")
            vals = (os.path.basename(p), "%d" % info["sample_rate"],
                    "%d" % info["channels"], "%.1f s" % info["duration_s"],
                    ", ".join(desc) or "nessun item")
        except Exception as exc:
            vals = (os.path.basename(p), "", "", "", "ERRORE: %s" % exc)
        msg_q.put(("row", (idx, vals)))

    def add_files():
        sel = filedialog.askopenfilenames(
            title="Seleziona file ADM/BW64",
            filetypes=[("BW64/WAV", "*.wav *.bw64 *.WAV"), ("Tutti", "*.*")])
        for p in sel:
            add_path(p)

    def remove_selected():
        for iid in tree.selection():
            idx = tree.index(iid)
            tree.delete(iid)
            del paths[idx]

    def clear_all():
        tree.delete(*tree.get_children())
        paths.clear()

    frm_btn = ttk.Frame(root)
    frm_btn.pack(fill="x", padx=10)
    ttk.Button(frm_btn, text="Aggiungi file...", command=add_files).pack(side="left")
    ttk.Button(frm_btn, text="Rimuovi", command=remove_selected).pack(side="left", padx=4)
    ttk.Button(frm_btn, text="Svuota", command=clear_all).pack(side="left")
    ttk.Label(frm_btn, text="   ").pack(side="left")  # separatore visivo
    btn_repair = ttk.Button(frm_btn, text="Ripara selezionati (EPS)")
    btn_repair.pack(side="left")
    ttk.Label(frm_btn,
             text="genera un file _repaired.wav accanto all'originale "
                  "e lo accoda; l'originale non viene toccato",
             foreground="#888").pack(side="left", padx=6)

    # ---- destinazione --------------------------------------------------
    frm_out = ttk.LabelFrame(root, text="Destinazione", padding=8)
    frm_out.pack(fill="x", padx=10, pady=(8, 4))
    out_dir = tk.StringVar(value="")
    ttk.Entry(frm_out, textvariable=out_dir).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 6))

    def choose_dir():
        d = filedialog.askdirectory(title="Cartella di destinazione")
        if d:
            out_dir.set(d)

    ttk.Button(frm_out, text="Sfoglia...", command=choose_dir).pack(side="left")
    ttk.Label(frm_out, text="(vuoto = accanto all'originale)").pack(side="left", padx=6)

    # ---- opzioni -------------------------------------------------------
    frm_opt = ttk.LabelFrame(root, text="Codifica", padding=8)
    frm_opt.pack(fill="x", padx=10, pady=4)

    order_var = tk.IntVar(value=3)
    norm_var = tk.StringVar(value="SN3D")
    gain_var = tk.DoubleVar(value=0.0)
    maxre_var = tk.BooleanVar(value=False)
    diffuse_var = tk.BooleanVar(value=True)
    dist_var = tk.BooleanVar(value=False)
    obj_var = tk.BooleanVar(value=True)
    ds_var = tk.BooleanVar(value=True)
    hoa_var = tk.BooleanVar(value=True)
    ch_label = tk.StringVar()

    def update_ch(*_):
        ch_label.set("%d canali" % n_channels_for_order(order_var.get()))

    order_var.trace_add("write", update_ch)
    update_ch()

    r1 = ttk.Frame(frm_opt); r1.pack(fill="x", pady=2)
    ttk.Label(r1, text="Ordine:").pack(side="left")
    ttk.Combobox(r1, textvariable=order_var, values=ORDERS, width=4,
                 state="readonly").pack(side="left", padx=(4, 2))
    ttk.Label(r1, textvariable=ch_label, width=12).pack(side="left")
    ttk.Label(r1, text="Normalizzazione:").pack(side="left", padx=(12, 4))
    ttk.Combobox(r1, textvariable=norm_var, values=["SN3D", "N3D"], width=6,
                 state="readonly").pack(side="left")
    ttk.Label(r1, text="Guadagno dB:").pack(side="left", padx=(12, 4))
    ttk.Spinbox(r1, textvariable=gain_var, from_=-24, to=24, increment=0.5,
                width=6).pack(side="left")

    r2 = ttk.Frame(frm_opt); r2.pack(fill="x", pady=2)
    ttk.Checkbutton(r2, text="max-rE", variable=maxre_var).pack(side="left")
    ttk.Checkbutton(r2, text="rendi diffuse (decorrelatori)",
                    variable=diffuse_var).pack(side="left", padx=10)
    ttk.Checkbutton(r2, text="applica distanza come guadagno",
                    variable=dist_var).pack(side="left")

    r3 = ttk.Frame(frm_opt); r3.pack(fill="x", pady=2)
    ttk.Label(r3, text="Includi:").pack(side="left")
    ttk.Checkbutton(r3, text="oggetti", variable=obj_var).pack(side="left", padx=4)
    ttk.Checkbutton(r3, text="direct speakers", variable=ds_var).pack(side="left", padx=4)
    ttk.Checkbutton(r3, text="pacchetti HOA", variable=hoa_var).pack(side="left", padx=4)

    # ---- log e avanzamento ---------------------------------------------
    frm_log = ttk.LabelFrame(root, text="Registro", padding=6)
    frm_log.pack(fill="both", expand=True, padx=10, pady=4)
    log_box = tk.Text(frm_log, height=8, wrap="word")
    log_box.pack(fill="both", expand=True)
    log_box.configure(state="disabled")

    frm_run = ttk.Frame(root)
    frm_run.pack(fill="x", padx=10, pady=(4, 10))
    prog = ttk.Progressbar(frm_run, mode="determinate", maximum=1000)
    prog.pack(side="left", fill="x", expand=True, padx=(0, 8))
    btn_start = ttk.Button(frm_run, text="Converti")
    btn_start.pack(side="left")
    btn_stop = ttk.Button(frm_run, text="Interrompi", state="disabled")
    btn_stop.pack(side="left", padx=4)

    def log(msg):
        msg_q.put(("log", msg))

    def start():
        if state["running"]:
            return
        if not paths:
            messagebox.showinfo("Nessun file", "Aggiungi almeno un file.")
            return
        settings = Settings(
            order=order_var.get(), norm=norm_var.get(),
            include_objects=obj_var.get(),
            include_direct_speakers=ds_var.get(),
            include_hoa=hoa_var.get(),
            use_diffuse=diffuse_var.get(),
            apply_max_re=maxre_var.get(),
            apply_distance_gain=dist_var.get(),
            gain_db=float(gain_var.get()))
        jobs = [(p, default_output(p, out_dir.get(), settings.order))
                for p in paths]

        state["running"] = True
        state["stop"] = False
        btn_start.configure(state="disabled")
        btn_stop.configure(state="normal")

        def worker():
            try:
                convert_batch(jobs, settings,
                              progress=lambda f: msg_q.put(("prog", f)),
                              log=log,
                              should_stop=lambda: state["stop"])
                log("Fatto.")
            except Exception:
                log("ERRORE:\n" + traceback.format_exc())
            finally:
                msg_q.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def stop():
        state["stop"] = True
        log("Interruzione richiesta, attendo la fine del file corrente...")

    def repair_selected():
        if state["running"]:
            return
        sel_idx = sorted(tree.index(iid) for iid in tree.selection())
        if not sel_idx:
            messagebox.showinfo("Nessuna selezione",
                                "Seleziona nella lista uno o piu' file "
                                "esportati dalla EPS da riparare.")
            return

        from adm_repair import repair_file, verify

        state["running"] = True
        btn_start.configure(state="disabled")
        btn_repair.configure(state="disabled")

        def worker():
            try:
                for i in sel_idx:
                    if state["stop"]:
                        break
                    src = paths[i]
                    base, _ = os.path.splitext(src)
                    dst = base + "_repaired.wav"
                    log("Riparazione: %s" % os.path.basename(src))
                    try:
                        report = repair_file(src, dst, log=lambda m: log("  " + m))
                        verify(dst, log=lambda m: log("  " + m))
                        root.after(0, add_path, dst)
                    except Exception as exc:
                        log("  ERRORE: %s" % exc)
                log("Riparazione completata.")
            except Exception:
                log("ERRORE:\n" + traceback.format_exc())
            finally:
                msg_q.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    btn_start.configure(command=start)
    btn_stop.configure(command=stop)
    btn_repair.configure(command=repair_selected)

    def pump():
        try:
            while True:
                kind, payload = msg_q.get_nowait()
                if kind == "log":
                    log_box.configure(state="normal")
                    log_box.insert("end", payload + "\n")
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "prog":
                    prog["value"] = payload * 1000
                elif kind == "row":
                    idx, vals = payload
                    kids = tree.get_children()
                    if idx < len(kids):
                        tree.item(kids[idx], values=vals)
                elif kind == "done":
                    state["running"] = False
                    btn_start.configure(state="normal")
                    btn_stop.configure(state="disabled")
                    btn_repair.configure(state="normal")
        except queue.Empty:
            pass
        root.after(80, pump)

    pump()
    root.mainloop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run_repair_cli(argv):
    import argparse
    from adm_repair import repair_file, verify

    p = argparse.ArgumentParser(
        description="Ripara file ADM esportati dalla EAR Production Suite "
                    "(audioTrackFormat/audioStreamFormat mancanti o "
                    "riferimenti a common definition sbagliati). Passaggio "
                    "SEPARATO dalla conversione: un file gia' corretto passa "
                    "senza modifiche, quindi e' sicuro lanciarlo sempre "
                    "prima di --inspect o della conversione se non sei "
                    "certo che l'export sia valido.")
    p.add_argument("inputs", nargs="+", help="file .wav da riparare")
    p.add_argument("-o", "--out-dir", default="",
                   help="cartella di destinazione (default: accanto "
                        "all'originale, suffisso _repaired)")
    a = p.parse_args(argv)

    any_error = False
    for f in a.inputs:
        base = os.path.splitext(os.path.basename(f))[0]
        out = os.path.join(a.out_dir or os.path.dirname(f),
                           base + "_repaired.wav")
        print("%s" % os.path.basename(f))
        try:
            report = repair_file(f, out, log=lambda m: print("  " + m))
            verify(out, log=lambda m: print("  " + m))
            if report["unresolvable"]:
                any_error = True
        except Exception as exc:
            print("  ERRORE: %s" % exc)
            any_error = True
    return 1 if any_error else 0


def run_cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="Converte ADM/BW64 in AmbiX")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--out-dir", default="")
    p.add_argument("-n", "--order", type=int, default=3, choices=ORDERS)
    p.add_argument("--norm", default="SN3D", choices=["SN3D", "N3D"])
    p.add_argument("--gain", type=float, default=0.0)
    p.add_argument("--max-re", action="store_true")
    p.add_argument("--no-diffuse", action="store_true")
    p.add_argument("--distance-gain", action="store_true")
    p.add_argument("--inspect", action="store_true",
                   help="mostra solo il contenuto dei file")
    a = p.parse_args(argv)

    if a.inspect:
        for f in a.inputs:
            i = inspect_file(f)
            print("%s: %d Hz, %d ch, %.1f s — %d oggetti, %d direct spk, "
                  "%d HOA" % (os.path.basename(f), i["sample_rate"],
                              i["channels"], i["duration_s"], i["objects"],
                              i["direct_speakers"], i["hoa"]))
        return 0

    settings = Settings(order=a.order, norm=a.norm, gain_db=a.gain,
                        apply_max_re=a.max_re,
                        use_diffuse=not a.no_diffuse,
                        apply_distance_gain=a.distance_gain)
    jobs = [(f, default_output(f, a.out_dir, a.order)) for f in a.inputs]
    results = convert_batch(jobs, settings, log=print)
    return 1 if any(r[2] for r in results) else 0


def run_split_cli(argv):
    import argparse
    from adm_split import split_region, verify, read_regions_csv

    p = argparse.ArgumentParser(
        description="Extracts one time region (a song, an act) from a "
                    "full ADM export as a standalone file, with all "
                    "object timings rebased to zero and objects outside "
                    "the region removed entirely (audio channel included).")
    p.add_argument("input", help="the full ADM .wav to extract from")
    p.add_argument("-o", "--out-dir", default="",
                   help="destination folder (default: next to the input)")
    p.add_argument("--regions-csv",
                   help="a Region/Marker Manager CSV export (Time in "
                        "seconds - H:MM:SS.ffffff); every region in it "
                        "is extracted as a separate file named after it")
    p.add_argument("--start", type=float, help="region start, in seconds")
    p.add_argument("--end", type=float, help="region end, in seconds")
    p.add_argument("--name", default="region",
                   help="output file suffix when using --start/--end "
                        "instead of --regions-csv")
    a = p.parse_args(argv)

    base = os.path.splitext(os.path.basename(a.input))[0]
    out_dir = a.out_dir or os.path.dirname(a.input)

    if a.regions_csv:
        regions = read_regions_csv(a.regions_csv)
        if not regions:
            print("No regions found in %s (only point markers?)" %
                 a.regions_csv)
            return 1
        any_error = False
        for r in regions:
            safe_name = "".join(c if c.isalnum() or c in "-_ " else "_"
                                for c in r["name"]).strip() or r["id"]
            out = os.path.join(out_dir, "%s_%s.wav" % (base, safe_name))
            print("%s  [%.3f s - %.3f s]" % (r["name"], r["start"], r["end"]))
            try:
                split_region(a.input, out, r["start"], r["end"],
                             log=lambda m: print("  " + m))
                verify(out, log=lambda m: print("  " + m))
            except Exception as exc:
                print("  ERROR: %s" % exc)
                any_error = True
        return 1 if any_error else 0

    if a.start is None or a.end is None:
        print("Provide either --regions-csv or both --start and --end.")
        return 1

    out = os.path.join(out_dir, "%s_%s.wav" % (base, a.name))
    try:
        split_region(a.input, out, a.start, a.end, log=print)
        verify(out, log=print)
    except Exception as exc:
        print("ERROR: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    if "--split" in sys.argv:
        args = [x for x in sys.argv[1:] if x != "--split"]
        sys.exit(run_split_cli(args))
    if "--repair" in sys.argv:
        args = [x for x in sys.argv[1:] if x != "--repair"]
        sys.exit(run_repair_cli(args))
    if "--cli" in sys.argv:
        args = [x for x in sys.argv[1:] if x != "--cli"]
        sys.exit(run_cli(args))
    try:
        run_gui()
    except ImportError:
        print("tkinter non disponibile; usa --cli")
        sys.exit(run_cli(sys.argv[1:]))
