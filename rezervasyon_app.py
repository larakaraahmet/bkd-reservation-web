import tkinter as tk
from tkinter import messagebox, simpledialog
from tkinter import ttk
from datetime import datetime, timedelta

def to_dt(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M")

def make_slots(start="09:50", end="17:00", step_min=10):
    cur, endt = to_dt(start), to_dt(end)
    out = []
    while cur < endt:
        nxt = cur + timedelta(minutes=step_min)
        out.append((cur.strftime("%H:%M"), nxt.strftime("%H:%M")))
        cur = nxt
    return out

def parse_range(text: str):
    text = text.strip()
    if not text:
        return None
    if "-" not in text:
        return None
    a, b = [x.strip() for x in text.split("-", 1)]
    try:
        sa, sb = to_dt(a), to_dt(b)
        if sb <= sa:
            return None
        return sa, sb
    except ValueError:
        return None

def run():
    ask = tk.Tk()
    ask.withdraw()
    masa_sayisi = simpledialog.askinteger("Masa Sayısı", "Kaç masa var?", minvalue=1, maxvalue=30)
    ask.destroy()
    if masa_sayisi is None:
        return

    tables = [str(i) for i in range(1, masa_sayisi + 1)]
    slots = make_slots()
    reservations = []

    def slot_start_dt(slot_tuple):
        return to_dt(slot_tuple[0])

    def team_ok(team, idx):
        return all(not (r["team"] == team and abs(r["slot_index"] - idx) < 3) for r in reservations)

    def free(table, idx):
        return all(not (r["table"] == table and r["slot_index"] == idx) for r in reservations)

    def find_slot(team, pref_range, pref_table):
        for i, s in enumerate(slots):
            st = slot_start_dt(s)
            if pref_range is not None:
                rs, re = pref_range
                if not (rs <= st < re):
                    continue

            table_list = [pref_table] if (pref_table and pref_table != "Auto") else tables
            for t in table_list:
                if free(t, i) and team_ok(team, i):
                    return t, s, i
        return None, None, None

    root = tk.Tk()
    root.title("BKD-FLL UNEARTHED Robot Reservation")
    root.geometry("900x650")

    top = tk.Frame(root)
    top.pack(pady=10)

    tk.Label(top, text="Takım Numarası:", font=("Arial", 12)).grid(row=0, column=0, padx=5, sticky="w")
    team_entry = tk.Entry(top, width=10, font=("Arial", 12))
    team_entry.grid(row=0, column=1, padx=5)

    tk.Label(top, text="İstenen Aralık (opsiyonel):", font=("Arial", 12)).grid(row=0, column=2, padx=5, sticky="w")
    range_entry = tk.Entry(top, width=14, font=("Arial", 12))
    range_entry.insert(0, "örn 11:20-12:10")
    range_entry.grid(row=0, column=3, padx=5)

    tk.Label(top, text="Masa (opsiyonel):", font=("Arial", 12)).grid(row=0, column=4, padx=5, sticky="w")
    table_var = tk.StringVar(value="Auto")
    table_combo = ttk.Combobox(top, textvariable=table_var, values=["Auto"] + tables, width=8, state="readonly")
    table_combo.grid(row=0, column=5, padx=5)

    result_label = tk.Label(root, text="", font=("Arial", 12))
    result_label.pack(pady=10)

    container = tk.Frame(root)
    container.pack(fill="both", expand=True)

    canvas = tk.Canvas(container)
    sb = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
    frame = tk.Frame(canvas)

    frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=frame, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)

    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    cell = {}

    tk.Label(frame, text="Saat", borderwidth=1, relief="solid", width=12).grid(row=0, column=0)
    for c, t in enumerate(tables, start=1):
        tk.Label(frame, text=f"Masa {t}", borderwidth=1, relief="solid", width=14).grid(row=0, column=c)

    for i, s in enumerate(slots):
        tk.Label(frame, text=f"{s[0]}-{s[1]}", borderwidth=1, relief="solid", width=12).grid(row=i+1, column=0)
        for c, t in enumerate(tables, start=1):
            lbl = tk.Label(frame, text="", borderwidth=1, relief="solid", width=14, bg="#d9ffd9")
            lbl.grid(row=i+1, column=c)
            cell[(i, t)] = lbl

    def refresh():
        for lbl in cell.values():
            lbl.config(text="", bg="#d9ffd9")
        for r in reservations:
            lbl = cell[(r["slot_index"], r["table"])]
            lbl.config(text=f"Takım {r['team']}", bg="#ffb3b3")

    def make_reservation():
        txt = team_entry.get().strip()
        if not txt.isdigit():
            messagebox.showerror("Hata", "Takım numarası bir sayı olmalı.")
            return
        team = int(txt)

        rtxt = range_entry.get().strip()
        if rtxt.lower().startswith("örn"):
            rtxt = ""
        pref_range = parse_range(rtxt)
        if rtxt and pref_range is None:
            messagebox.showerror("Hata", "Saat aralığı formatı: HH:MM-HH:MM (örn 11:20-12:10)")
            return

        pref_table = table_var.get()

        t, s, idx = find_slot(team, pref_range, pref_table)
        if t is None:
            if pref_table != "Auto":
                messagebox.showinfo("Bilgi", f"Seçtiğin masada ve/veya aralıkta uygun zaman yok: Masa {pref_table}")
            else:
                messagebox.showinfo("Bilgi", "Bu takım için uygun zaman bulunamadı.")
            result_label.config(text="❌ Rezervasyon yapılamadı.")
            return

        reservations.append({"team": team, "table": t, "slot_index": idx})
        result_label.config(text=f"✅ Takım {team} → Masa {t} → {s[0]}-{s[1]}")
        team_entry.delete(0, tk.END)
        refresh()

    def show_all():
        if not reservations:
            messagebox.showinfo("Rezervasyonlar", "Henüz rezervasyon yok.")
            return
        lines = []
        for r in reservations:
            s = slots[r["slot_index"]]
            lines.append(f"Takım {r['team']} - Masa {r['table']} - {s[0]}-{s[1]}")
        messagebox.showinfo("Tüm Rezervasyonlar", "\n".join(lines))

    tk.Button(top, text="Rezervasyon Al", font=("Arial", 12), command=make_reservation).grid(row=0, column=6, padx=10)
    tk.Button(root, text="Tüm Rezervasyonları Göster", font=("Arial", 12), command=show_all).pack(pady=5)

    refresh()
    root.mainloop()

if __name__ == "__main__":
    run()
