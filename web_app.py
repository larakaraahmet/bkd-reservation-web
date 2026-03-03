from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import psycopg
import psycopg.rows

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

app = Flask(__name__)

# ---- ZAMAN AYARLARI ----
START = "09:50"
END = "17:00"
STEP_MIN = 10

# ---- ENV AYARLARI ----
TABLE_COUNT_DEFAULT = int(os.getenv("TABLE_COUNT", "5"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "secret")

# Saat dilimi (Render'da ENV: TZ=Europe/Istanbul önerilir)
APP_TZ = os.getenv("TZ", "Europe/Istanbul")

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


def minutes_from_hhmm(hhmm: str) -> int:
    d = to_dt(hhmm)
    return d.hour * 60 + d.minute


def ceil_to_step(mins: int, step: int) -> int:
    return ((mins + step - 1) // step) * step


def now_local_minutes() -> int:
    if ZoneInfo:
        try:
            n = datetime.now(ZoneInfo(APP_TZ))
            return n.hour * 60 + n.minute
        except Exception:
            pass
    n = datetime.now()
    return n.hour * 60 + n.minute


# ---- DB ----
def db_conn():
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
    return sorted(_reservations_mem, key=lambda r: r["slot_index"])


def team_ok(res, team, idx):
    return all(not (r["team"] == team and abs(r["slot_index"] - idx) < 3) for r in res)


def free(res, table, idx):
    return all(not (r["table"] == table and r["slot_index"] == idx) for r in res)


def find_slot(res, team, pref_range, pref_table, table_count: int, allow_past: bool = False, bypass_spacing: bool = False):
    tables = [str(i) for i in range(1, table_count + 1)]

    earliest_m = None
    if not allow_past:
        earliest_m = ceil_to_step(now_local_minutes(), STEP_MIN)

    for i, s in enumerate(slots):
        if earliest_m is not None and minutes_from_hhmm(s[0]) < earliest_m:
            continue

        st = to_dt(s[0])

        if pref_range:
            rs, re = pref_range
            if not (rs <= st < re):
                continue

        table_list = [pref_table] if (pref_table and pref_table != "Auto") else tables
        for t in table_list:
            if not free(res, t, i):
                continue
            if (not bypass_spacing) and (not team_ok(res, team, i)):
                continue
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
#modalBox {{ position: relative; background:white; padding:20px; border-radius:10px; width: min(640px, 92vw); }}
#msg {{ margin: 8px 0 14px 0; font-weight: 600; }}
.small {{ font-size: 12px; opacity: 0.85; }}
hr {{ border:none; border-top:1px solid #ddd; margin: 14px 0; }}
.badge {{ display:inline-block; padding:4px 8px; border:1px solid #ddd; border-radius:999px; font-size:12px; }}

.closeX {{
  position: absolute;
  top: 12px;
  right: 14px;
  font-size: 22px;
  cursor: pointer;
  background: transparent;
  border: none;
  line-height: 1;
}}

/* ✅ Sağ üst saat */
#clock {{
  position: fixed;
  top: 14px;
  right: 16px;
  background: rgba(255,255,255,0.92);
  border: 1px solid #ddd;
  border-radius: 10px;
  padding: 8px 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.5px;
  z-index: 10000;
}}

/* ✅ Takım arama */
.searchBox {{
  display:flex;
  gap:10px;
  align-items:center;
  flex-wrap:wrap;
}}
#teamSearch {{
  width: 160px;
  padding: 7px 10px;
  border: 1px solid #aaa;
  border-radius: 8px;
}}
.hl {{ outline: 3px solid #000; }}
.dim {{ opacity: 0.18; }}

/* ✅ admin badge */
#adminBadge {{
  display:none;
  position: fixed;
  top: 62px;
  right: 16px;
  background: #111;
  color: #fff;
  border-radius: 999px;
  padding: 6px 10px;
  font-size: 12px;
  z-index: 10000;
}}
</style>
</head>
<body>

<div id="clock"></div>
<div id="adminBadge">ADMIN ✅</div>

<div class="badge">{badge}</div>

<div id="modal">
  <div id="modalBox">
    <button class="closeX" onclick="closeModal()" aria-label="Kapat">✕</button>

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

  <div class="searchBox">
    <label>Takım Ara:
      <input id="teamSearch" placeholder="örn: 3641" oninput="applyTeamFilter()" />
    </label>
    <label style="user-select:none;">
      <input id="onlyThisTeam" type="checkbox" onchange="applyTeamFilter()" />
      Sadece bu takımı göster
    </label>
  </div>

  <label style="user-select:none;">
    <input id="adminMode" type="checkbox" onchange="toggleAdminUI()" />
    Admin Mod
  </label>

  <button onclick="adminLogin()">Admin Giriş</button>

  <label id="slotPickWrap" style="display:none;">Saat:
    <select id="slotPick"></select>
  </label>

  <label id="overwriteWrap" style="display:none; user-select:none;">
    <input id="overwrite" type="checkbox" checked />
    Doluysa üstüne yaz
  </label>
</div>

<p id="msg"></p>
<table id="grid"></table>

<script>
let tables = [];
let tableCount = {TABLE_COUNT_DEFAULT};
let slotLabels = [];

function pad2(n) {{ return String(n).padStart(2, "0"); }}

function startClock() {{
  const el = document.getElementById("clock");
  if(!el) return;
  function tick() {{
    const d = new Date();
    el.innerText = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }}
  tick();
  setInterval(tick, 250);
}}
startClock();

function closeModal() {{
  document.getElementById("modal").style.display = "none";
}}

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
  closeModal();
  buildTables(n);
  load();
}}

function changeMasa() {{
  document.getElementById("modal").style.display = "flex";
  const cur = localStorage.getItem("masa_sayisi");
  document.getElementById("masaInput").value = cur ? parseInt(cur) : tableCount;
}}

function getSearchTeam() {{
  const v = (document.getElementById("teamSearch")?.value || "").trim();
  if (!v) return null;
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}}

function applyTeamFilter() {{
  const team = getSearchTeam();
  const only = document.getElementById("onlyThisTeam")?.checked;

  const grid = document.getElementById("grid");
  if (!grid) return;

  grid.querySelectorAll("td").forEach(td => {{
    td.classList.remove("hl");
    td.classList.remove("dim");
  }});

  if (!team) return;

  grid.querySelectorAll("td[data-team]").forEach(td => {{
    const t = parseInt(td.getAttribute("data-team"), 10);
    if (t === team) td.classList.add("hl");
    else if (only) td.classList.add("dim");
  }});
}}

// ---- ADMIN MODE (15dk session) ----
function getAdminToken() {{
  const raw = sessionStorage.getItem("admin_token") || "";
  const exp = parseInt(sessionStorage.getItem("admin_token_exp") || "0", 10);
  if (!raw) return "";
  if (Date.now() > exp) {{
    sessionStorage.removeItem("admin_token");
    sessionStorage.removeItem("admin_token_exp");
    return "";
  }}
  return raw;
}}

function adminLogin() {{
  const t = prompt("Admin şifresi:");
  if (!t) return;
  sessionStorage.setItem("admin_token", t);
  sessionStorage.setItem("admin_token_exp", String(Date.now() + 15 * 60 * 1000));
  alert("✅ Admin giriş OK (15 dk)");
  // checkbox açık ise badge göster
  toggleAdminUI();
}}

function toggleAdminUI() {{
  const on = document.getElementById("adminMode").checked;
  document.getElementById("slotPickWrap").style.display = on ? "inline-block" : "none";
  document.getElementById("overwriteWrap").style.display = on ? "inline-block" : "none";

  const badge = document.getElementById("adminBadge");
  const hasToken = !!getAdminToken();
  badge.style.display = (on && hasToken) ? "block" : "none";
}}

async function load() {{
  const res = await fetch('/api/state');
  const data = await res.json();

  buildSlotsForDelete(data.slots);

  // admin slot picker doldur
  const sp = document.getElementById("slotPick");
  if (sp) {{
    sp.innerHTML = "";
    data.slots.forEach((s, i) => {{
      sp.innerHTML += "<option value='"+i+"'>"+s[0]+"-"+s[1]+"</option>";
    }});
  }}

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
      if (taken.has(key)) {{
        const team = taken.get(key);
        row += "<td class='taken' data-team='"+team+"'>Takım "+team+"</td>";
      }} else {{
        row += "<td class='free'></td>";
      }}
    }});
    row += "</tr>";
    grid.innerHTML += row;
  }});

  applyTeamFilter();
  toggleAdminUI();
}}

async function reserve() {{
  const team = parseInt(document.getElementById("team").value);
  const range = document.getElementById("range").value;
  const table = document.getElementById("table").value;

  const adminMode = document.getElementById("adminMode").checked;
  const admin_token = adminMode ? getAdminToken() : "";
  const slot_index = adminMode ? parseInt(document.getElementById("slotPick").value, 10) : null;
  const overwrite = adminMode ? document.getElementById("overwrite").checked : false;

  const res = await fetch('/api/reserve', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{
      team, range, table, table_count: tableCount,
      admin_token, slot_index, overwrite
    }})
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

window.addEventListener("click", (e) => {{
  const modal = document.getElementById("modal");
  if (e.target === modal) closeModal();
}});

// ✅ Auto-refresh: 10 saniyede bir (modal açıkken bekler)
setInterval(() => {{
  const modal = document.getElementById("modal");
  const isOpen = modal && modal.style.display === "flex";
  if (!isOpen) load();
}}, 10000);
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

    # ✅ admin override
    admin_token = data.get("admin_token", "")
    is_admin = (admin_token == ADMIN_TOKEN)
    exact_slot_index = data.get("slot_index", None)
    overwrite = bool(data.get("overwrite", False))

    if not isinstance(team, int):
        return jsonify({"ok": False, "error": "Takım numarası sayı olmalı"})

    try:
        table_count = int(table_count)
        if table_count < 1 or table_count > 60:
            return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})
    except:
        return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})

    res = get_reservations()

    # ✅ ADMIN: istediğin masa + istediğin slot_index (kurallar yok)
    if is_admin and exact_slot_index is not None:
        try:
            exact_slot_index = int(exact_slot_index)
        except Exception:
            return jsonify({"ok": False, "error": "slot_index geçersiz"})

        if exact_slot_index < 0 or exact_slot_index >= len(slots):
            return jsonify({"ok": False, "error": "slot_index aralık dışı"})

        if not isinstance(pref_table, str) or pref_table in ("", "Auto"):
            return jsonify({"ok": False, "error": "Admin modda masa seçmelisin"})

        if overwrite:
            delete_by_table_slot(pref_table, exact_slot_index)

        try:
            insert_reservation(team, pref_table, exact_slot_index)
        except Exception:
            return jsonify({"ok": False, "error": "Bu slot dolu (overwrite aç)"})

        s = slots[exact_slot_index]
        return jsonify({"ok": True, "result": {"team": team, "table": pref_table, "slot": s}})

    # Normal kullanıcı akışı (admin değilse kurallar var, admin ise kurallar kalkar)
    t, s, idx = find_slot(
        res, team, pref_range, pref_table, table_count,
        allow_past=is_admin,
        bypass_spacing=is_admin
    )

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
    init_db()
    app.run(debug=True)
