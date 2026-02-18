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
TABLE_COUNT_DEFAULT = int(os.getenv("TABLE_COUNT", "5"))  # web açılışında popup var, bu sadece default
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "secret")


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
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(url)


def init_db():
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


# Flask 3 uyumlu: ilk requestte bir kez init
@app.before_request
def startup():
    if not hasattr(app, "_db_inited"):
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
    with db_conn() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute('SELECT id, team, "table", slot_index FROM reservations ORDER BY slot_index;')

            return cur.fetchall()


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


# ---- UI ----
@app.get("/")
def home():
    # HTML içinde default table count kullanıyoruz, kullanıcı popup ile değiştirecek
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

button {{
  padding: 8px 12px;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}}

#resetBtn {{
  background: #d11;
  color: white;
}}

#reserveBtn {{
  background: #0b6;
  color: white;
}}

#modal {{
  position: fixed;
  top:0; left:0;
  width:100%; height:100%;
  background:rgba(0,0,0,0.6);
  display:none;
  align-items:center;
  justify-content:center;
  z-index: 9999;
}}

#modalBox {{
  background:white;
  padding:20px;
  border-radius:10px;
  width: min(420px, 92vw);
}}

#msg {{
  margin: 8px 0 14px 0;
  font-weight: 600;
}}
</style>
</head>
<body>

<div id="modal">
  <div id="modalBox">
    <h3>Kaç masa var?</h3>
    <p style="margin-top:0;">(Kaç adet deneme masası var?)</p>
    <input id="masaInput" type="number" min="1" style="width:120px;padding:6px;" />
    <button onclick="saveMasa()">Kaydet</button>
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

  <button onclick="changeMasa()">Masa Sayısını Değiştir</button>
</div>

<p id="msg"></p>
<table id="grid"></table>

<script>
let tables = [];
let tableCount = {TABLE_COUNT_DEFAULT};

function buildTables(n) {{
  tableCount = n;
  tables = [];
  const select = document.getElementById("table");
  select.innerHTML = "<option>Auto</option>";
  for (let i=1; i<=n; i++) {{
    tables.push(String(i));
    select.innerHTML += "<option>"+i+"</option>";
  }}
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

async function delRes(id) {
  const token = prompt("Admin şifresi:");
  if (!token) return;

  const ok = confirm("Bu rezervasyonu silmek istiyor musun?");
  if (!ok) return;

  const res = await fetch('/api/delete', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ token, id })
  });

  const data = await res.json();

  if (!data.ok) {
    alert("❌ " + (data.error || "Silinemedi"));
  } else {
    document.getElementById("msg").innerText = "🗑️ Rezervasyon silindi.";
    load();
  }
}

async function load() {{
  const res = await fetch('/api/state');
  const data = await res.json();

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  let head = "<tr><th>Saat</th>";
  tables.forEach(t => head += "<th>Masa "+t+"</th>");
  head += "</tr>";
  grid.innerHTML += head;

  const taken = new Map();
  data.reservations.forEach(r => {{
    taken.set(r.slot_index + "-" + r.table, { team: r.team, id: r.id });
  }});

  data.slots.forEach((s,i) => {{
    let row = "<tr><td>"+s[0]+"-"+s[1]+"</td>";
    tables.forEach(t => {{
      let key = i + "-" + t;
      if (taken.has(key))
        const info = taken.get(key);
row += "<td class='taken' style='cursor:pointer' onclick='delRes("+info.id+")'>Takım "+info.team+"</td>";

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
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO reservations(team,"table",slot_index) VALUES (%s,%s,%s);',
                    (team, t, idx)
                )
            conn.commit()
    except Exception:
        return jsonify({"ok": False, "error": "Slot az önce alındı, tekrar dene"})

    return jsonify({"ok": True, "result": {"team": team, "table": t, "slot": s}})


@app.post("/api/reset")
def reset():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE reservations;")
        conn.commit()

    return jsonify({"ok": True})

@app.post("/api/delete")
def delete_reservation():
    data = request.json or {}
    token = data.get("token", "")
    rid = data.get("id")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    try:
        rid = int(rid)
    except Exception:
        return jsonify({"ok": False, "error": "Geçersiz id"}), 400

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reservations WHERE id = %s;", (rid,))
            deleted = cur.rowcount
        conn.commit()

    if deleted == 0:
        return jsonify({"ok": False, "error": "Rezervasyon bulunamadı"}), 404

    return jsonify({"ok": True})



if __name__ == "__main__":
    init_db()
    app.run(debug=True)
