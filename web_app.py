from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import psycopg
import psycopg.rows

app = Flask(__name__)

# ---- ZAMAN AYARLARI ----
START = "09:50"
END = "17:00"
STEP_MIN = 10

# ---- ENV AYARLARI ----
TABLE_COUNT_DEFAULT = int(os.getenv("TABLE_COUNT", "5"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "secret")

# ---- MOD SEÇİMİ (DB varsa DB, yoksa RAM) ----
DATABASE_URL = os.getenv("DATABASE_URL")
USE_DB = bool(DATABASE_URL)

# RAM MODE verisi (local için)
_reservations_mem = []  # list[dict]: {id, team, table, slot_index}
_next_id = 1


# ---- ZAMAN YARDIMCILARI ----
def to_dt(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M")


def make_slots():
    cur, endt = to_dt(START), to_dt(END)
    out = []
    while cur < endt:
        nxt = cur + timedelta(minutes=STEP_MIN)
        out.append((cur.strftime("%H:%M"), nxt.strftime("%H:%M")))
        cur = nxt
    return out


slots = make_slots()


# ---- DB ----
def db_conn():
    # Sadece DB modunda çağrılır
    return psycopg.connect(DATABASE_URL)


def init_db():
    if not USE_DB:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id SERIAL PRIMARY KEY,
                    team INTEGER NOT NULL,
                    "table" TEXT NOT NULL,
                    slot_index INTEGER NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE ("table", slot_index)
                );
            """)
        conn.commit()


# DB modunda ilk requestte init
@app.before_request
def startup():
    if USE_DB and not hasattr(app, "_db_inited"):
        init_db()
        app._db_inited = True


# ---- REZERVASYON LOGIC ----
def parse_range(text):
    text = (text or "").strip()
    if not text or "-" not in text:
        return None
    a, b = [x.strip() for x in text.split("-", 1)]
    try:
        sa, sb = to_dt(a), to_dt(b)
        if sb <= sa:
            return None
        return sa, sb
    except ValueError:
        return None


def get_reservations():
    if USE_DB:
        with db_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute('SELECT id, team, "table", slot_index FROM reservations ORDER BY slot_index;')
                return cur.fetchall()
    # RAM mode
    return sorted(_reservations_mem, key=lambda r: r["slot_index"])


def team_ok(res, team, idx):
    return all(not (r["team"] == team and abs(r["slot_index"] - idx) < 3) for r in res)


def free(res, table, idx):
    return all(not (r["table"] == table and r["slot_index"] == idx) for r in res)


def find_slot(res, team, pref_range, pref_table, table_count: int):
    tables = [str(i) for i in range(1, table_count + 1)]

    for i, s in enumerate(slots):
        st = to_dt(s[0])

        if pref_range:
            rs, re = pref_range
            if not (rs <= st < re):
                continue

        table_list = [pref_table] if (pref_table and pref_table != "Auto") else tables
        for t in table_list:
            if free(res, t, i) and team_ok(res, team, i):
                return t, s, i

    return None, None, None


def insert_reservation(team: int, table: str, slot_index: int):
    global _next_id

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO reservations(team,"table",slot_index) VALUES (%s,%s,%s);',
                    (team, table, slot_index)
                )
            conn.commit()
        return

    # RAM mode uniqueness: same table+slot only once
    for r in _reservations_mem:
        if r["table"] == table and r["slot_index"] == slot_index:
            raise RuntimeError("taken")

    _reservations_mem.append({
        "id": _next_id,
        "team": team,
        "table": table,
        "slot_index": slot_index
    })
    _next_id += 1


def reset_all():
    global _reservations_mem, _next_id
    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE reservations;")
            conn.commit()
        return

    _reservations_mem = []
    _next_id = 1


def delete_by_team(team: int) -> int:
    global _reservations_mem
    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM reservations WHERE team = %s;', (team,))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [r for r in _reservations_mem if r["team"] != team]
    return before - len(_reservations_mem)


def delete_by_table_slot(table: str, slot_index: int) -> int:
    global _reservations_mem
    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM reservations WHERE "table" = %s AND slot_index = %s;', (table, slot_index))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [r for r in _reservations_mem if not (r["table"] == table and r["slot_index"] == slot_index)]
    return before - len(_reservations_mem)


# ---- UI ----
@app.get("/")
def home():
    badge = "DB: Postgres ✅" if USE_DB else "DB: RAM mode (geçici) ⚠️"
    return f"""
<html>
<head>
<title>Robot Reservation</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body {{ font-family: Arial; margin: 20px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 1100px; }}
td, th {{ border: 1px solid #aaa; padding: 6px; text-align:center; }}
.free {{ background:#d9ffd9; }}
.taken {{ background:#ffb3b3; }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }}
button {{ padding: 8px 12px; border: 0; border-radius: 6px; cursor: pointer; }}
#resetBtn {{ background: #d11; color: white; }}
#reserveBtn {{ background: #0b6; color: white; }}
#deleteBtn {{ background: #555; color: white; }}
#modal {{ position: fixed; top:0; left:0; width:100%; height:100%;
  background:rgba(0,0,0,0.6); display:none; align-items:center; justify-content:center; z-index: 9999; }}
#modalBox {{ background:white; padding:20px; border-radius:10px; width: min(520px, 92vw); }}
#msg {{ margin: 8px 0 14px 0; font-weight: 600; }}
.small {{ font-size: 12px; opacity: 0.85; }}
hr {{ border:none; border-top:1px solid #ddd; margin: 14px 0; }}
.badge {{ display:inline-block; padding:4px 8px; border:1px solid #ddd; border-radius:999px; font-size:12px; }}
</style>
</head>
<body>

<div class="badge">{badge}</div>

<div id="modal">
  <div id="modalBox">
    <h3>Kaç masa var?</h3>
    <p style="margin-top:0;">(Kaç adet deneme masası var?)</p>
    <input id="masaInput" type="number" min="1" style="width:120px;padding:6px;" />
    <button onclick="saveMasa()">Kaydet</button>

    <hr/>

    <h3>🗑️ Rezervasyon Sil (Admin)</h3>
    <p class="small">Takım numarasıyla toplu sil veya masa+saat ile tek sil.</p>

    <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
      <label>Takım:
        <input id="delTeam" type="number" style="width:110px;" />
      </label>

      <label>Masa:
        <select id="delTable"></select>
      </label>

      <label>Saat:
        <select id="delSlot"></select>
      </label>

      <button id="deleteBtn" onclick="deleteReservation()">Sil</button>
    </div>

    <p class="small" style="margin-bottom:0;">Not: Silme için admin şifresi sorulacak.</p>
  </div>
</div>

<h2>Robot Reservation</h2>

<div class="controls">
  <label>Takım:
    <input id="team" type="number" style="width:110px;" />
  </label>

  <label>Aralık:
    <input id="range" placeholder="11:20-12:10" style="width:140px;" />
  </label>

  <label>Masa:
    <select id="table"></select>
  </label>

  <button id="reserveBtn" onclick="reserve()">Rezervasyon Al</button>
  <button id="resetBtn" onclick="resetTable()">Tabloyu Sıfırla</button>
  <button onclick="changeMasa()">Masa Sayısını Değiştir / Silme Menüsü</button>
</div>

<p id="msg"></p>
<table id="grid"></table>

<script>
let tables = [];
let tableCount = {TABLE_COUNT_DEFAULT};
let slotLabels = [];

function buildTables(n) {{
  tableCount = n;
  tables = [];
  const select = document.getElementById("table");
  const delSelect = document.getElementById("delTable");

  select.innerHTML = "<option>Auto</option>";
  delSelect.innerHTML = "<option>Seç</option>";

  for (let i=1; i<=n; i++) {{
    tables.push(String(i));
    select.innerHTML += "<option>"+i+"</option>";
    delSelect.innerHTML += "<option>"+i+"</option>";
  }}
}}

function buildSlotsForDelete(slots) {{
  slotLabels = slots.map(s => s[0] + "-" + s[1]);
  const sel = document.getElementById("delSlot");
  sel.innerHTML = "<option>Seç</option>";
  slotLabels.forEach(lbl => {{
    sel.innerHTML += "<option>"+lbl+"</option>";
  }});
}}

function initTables() {{
  let saved = localStorage.getItem("masa_sayisi");
  if (!saved) {{
    document.getElementById("modal").style.display = "flex";
    return;
  }}
  buildTables(parseInt(saved));
  load();
}}

function saveMasa() {{
  const n = parseInt(document.getElementById("masaInput").value);
  if (!n || n < 1) return;
  localStorage.setItem("masa_sayisi", n);
  document.getElementById("modal").style.display = "none";
  buildTables(n);
  load();
}}

function changeMasa() {{
  document.getElementById("modal").style.display = "flex";
  const cur = localStorage.getItem("masa_sayisi");
  document.getElementById("masaInput").value = cur ? parseInt(cur) : tableCount;
}}

async function load() {{
  const res = await fetch('/api/state');
  const data = await res.json();

  buildSlotsForDelete(data.slots);

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  let head = "<tr><th>Saat</th>";
  tables.forEach(t => head += "<th>Masa "+t+"</th>");
  head += "</tr>";
  grid.innerHTML += head;

  const taken = new Map();
  data.reservations.forEach(r => {{
    taken.set(r.slot_index + "-" + r.table, r.team);
  }});

  data.slots.forEach((s,i) => {{
    let row = "<tr><td>"+s[0]+"-"+s[1]+"</td>";
    tables.forEach(t => {{
      let key = i + "-" + t;
      if (taken.has(key))
        row += "<td class='taken'>Takım "+taken.get(key)+"</td>";
      else
        row += "<td class='free'></td>";
    }});
    row += "</tr>";
    grid.innerHTML += row;
  }});
}}

async function reserve() {{
  const team = parseInt(document.getElementById("team").value);
  const range = document.getElementById("range").value;
  const table = document.getElementById("table").value;

  const res = await fetch('/api/reserve', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ team, range, table, table_count: tableCount }})
  }});

  const data = await res.json();

  if (!data.ok) {{
    document.getElementById("msg").innerText = "❌ " + data.error;
  }} else {{
    document.getElementById("msg").innerText =
      "✅ Takım " + data.result.team +
      " → Masa " + data.result.table +
      " → " + data.result.slot[0] + "-" + data.result.slot[1];
  }}

  load();
}}

async function resetTable() {{
  const token = prompt("Admin şifresi:");
  if (!token) return;

  const res = await fetch('/api/reset', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ token }})
  }});

  const data = await res.json();

  if (!data.ok) {{
    alert("❌ Şifre yanlış / yetkisiz!");
  }} else {{
    alert("✅ Tüm rezervasyonlar silindi!");
    load();
  }}
}}

async function deleteReservation() {{
  const token = prompt("Admin şifresi:");
  if (!token) return;

  const delTeam = document.getElementById("delTeam").value;
  const delTable = document.getElementById("delTable").value;
  const delSlot = document.getElementById("delSlot").value;

  if (delTeam) {{
    const team = parseInt(delTeam);
    const res = await fetch('/api/delete', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ token, team }})
    }});
    const data = await res.json();
    if (!data.ok) alert("❌ " + data.error);
    else alert("✅ " + data.message);
    document.getElementById("delTeam").value = "";
    load();
    return;
  }}

  if (delTable === "Seç" || delSlot === "Seç") {{
    alert("Takım gir ya da Masa + Saat seç.");
    return;
  }}

  const slot_index = slotLabels.indexOf(delSlot);
  if (slot_index < 0) {{
    alert("Saat bulunamadı.");
    return;
  }}

  const res = await fetch('/api/delete', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ token, table: delTable, slot_index }})
  }});
  const data = await res.json();
  if (!data.ok) alert("❌ " + data.error);
  else alert("✅ " + data.message);
  load();
}}

buildTables(tableCount);
initTables();
</script>

</body>
</html>
"""


# ---- API ----
@app.get("/api/state")
def state():
    return jsonify({
        "slots": slots,
        "reservations": get_reservations(),
    })


@app.post("/api/reserve")
def reserve():
    data = request.json or {}

    team = data.get("team")
    pref_range = parse_range(data.get("range"))
    pref_table = data.get("table", "Auto")
    table_count = data.get("table_count", TABLE_COUNT_DEFAULT)

    if not isinstance(team, int):
        return jsonify({"ok": False, "error": "Takım numarası sayı olmalı"})

    try:
        table_count = int(table_count)
        if table_count < 1 or table_count > 60:
            return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})
    except:
        return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})

    res = get_reservations()
    t, s, idx = find_slot(res, team, pref_range, pref_table, table_count)

    if not t:
        return jsonify({"ok": False, "error": "Uygun slot bulunamadı"})

    try:
        insert_reservation(team, t, idx)
    except Exception:
        return jsonify({"ok": False, "error": "Slot az önce alındı, tekrar dene"})

    return jsonify({"ok": True, "result": {"team": team, "table": t, "slot": s}})


@app.post("/api/reset")
def reset():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    reset_all()
    return jsonify({"ok": True})


@app.post("/api/delete")
def delete():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    team = data.get("team", None)
    table = data.get("table", None)
    slot_index = data.get("slot_index", None)

    if isinstance(team, int):
        deleted = delete_by_team(team)
        return jsonify({"ok": True, "message": f"Takım {team} için {deleted} rezervasyon silindi."})

    if isinstance(table, str) and (isinstance(slot_index, int) or (isinstance(slot_index, str) and slot_index.isdigit())):
        slot_index = int(slot_index)
        deleted = delete_by_table_slot(table, slot_index)
        if deleted == 0:
            return jsonify({"ok": False, "error": "Bu masa+saat için rezervasyon bulunamadı."})
        return jsonify({"ok": True, "message": "Rezervasyon silindi."})

    return jsonify({"ok": False, "error": "Silme için ya team ya da table+slot_index göndermelisin."}), 400


if __name__ == "__main__":
    # Localde de çalışsın diye: DB yoksa init_db no-op zaten.
    init_db()
    app.run(debug=True)
