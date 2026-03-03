from flask import Flask, request, jsonify, Response
from datetime import datetime, timedelta
import os
import csv
import io
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
APP_TZ = os.getenv("TZ", "Europe/Istanbul")

# ---- MOD SEÇİMİ (DB varsa DB, yoksa RAM) ----
DATABASE_URL = os.getenv("DATABASE_URL")
USE_DB = bool(DATABASE_URL)

# RAM MODE verisi (local için)
_reservations_mem = []  # list[dict]: {id, team, table, slot_index, day, area}
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
            # Ana tablo
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    id SERIAL PRIMARY KEY,
                    team INTEGER NOT NULL,
                    "table" TEXT NOT NULL,
                    slot_index INTEGER NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            # Migration: day / area kolonları
            cur.execute('ALTER TABLE reservations ADD COLUMN IF NOT EXISTS day TEXT NOT NULL DEFAULT %s;', ("Day1",))
            cur.execute('ALTER TABLE reservations ADD COLUMN IF NOT EXISTS area TEXT NOT NULL DEFAULT %s;', ("A",))

            # Unique constraint migration:
            # Eski unique (table, slot_index) varsa bile DB hata vermesin diye IF NOT EXISTS ile yeni index.
            # Postgres'te constraint IF NOT EXISTS yok; index ile çözüyoruz.
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS reservations_unique_slot
                ON reservations(day, area, "table", slot_index);
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


def norm_day_area(day, area):
    day = (day or "Day1").strip()
    area = (area or "A").strip()
    if day not in ("Day1", "Day2"):
        day = "Day1"
    if area not in ("A", "B"):
        area = "A"
    return day, area


def get_reservations(day="Day1", area="A"):
    day, area = norm_day_area(day, area)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    'SELECT id, team, "table", slot_index, day, area FROM reservations WHERE day=%s AND area=%s ORDER BY slot_index;',
                    (day, area)
                )
                return cur.fetchall()

    # RAM mode
    res = [r for r in _reservations_mem if r.get("day") == day and r.get("area") == area]
    return sorted(res, key=lambda r: r["slot_index"])


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


def insert_reservation(day: str, area: str, team: int, table: str, slot_index: int):
    global _next_id
    day, area = norm_day_area(day, area)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # ✅ sağlam: conflict olursa ekleme ve None dön
                cur.execute("""
                    INSERT INTO reservations(day, area, team, "table", slot_index)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (day, area, "table", slot_index) DO NOTHING
                    RETURNING id;
                """, (day, area, team, table, slot_index))
                row = cur.fetchone()
            conn.commit()

        if row is None:
            raise RuntimeError("taken")
        return

    # RAM mode uniqueness
    for r in _reservations_mem:
        if r["day"] == day and r["area"] == area and r["table"] == table and r["slot_index"] == slot_index:
            raise RuntimeError("taken")

    _reservations_mem.append({
        "id": _next_id,
        "team": team,
        "table": table,
        "slot_index": slot_index,
        "day": day,
        "area": area
    })
    _next_id += 1


def reset_all(day=None, area=None):
    global _reservations_mem, _next_id

    if day is None or area is None:
        # full reset
        if USE_DB:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("TRUNCATE TABLE reservations;")
                conn.commit()
            return
        _reservations_mem = []
        _next_id = 1
        return

    day, area = norm_day_area(day, area)
    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM reservations WHERE day=%s AND area=%s;", (day, area))
            conn.commit()
        return

    _reservations_mem = [r for r in _reservations_mem if not (r["day"] == day and r["area"] == area)]


def delete_by_team(day: str, area: str, team: int) -> int:
    global _reservations_mem
    day, area = norm_day_area(day, area)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM reservations WHERE day=%s AND area=%s AND team=%s;', (day, area, team))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [r for r in _reservations_mem if not (r["day"] == day and r["area"] == area and r["team"] == team)]
    return before - len(_reservations_mem)


def delete_by_table_slot(day: str, area: str, table: str, slot_index: int) -> int:
    global _reservations_mem
    day, area = norm_day_area(day, area)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'DELETE FROM reservations WHERE day=%s AND area=%s AND "table"=%s AND slot_index=%s;',
                    (day, area, table, slot_index)
                )
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [
        r for r in _reservations_mem
        if not (r["day"] == day and r["area"] == area and r["table"] == table and r["slot_index"] == slot_index)
    ]
    return before - len(_reservations_mem)


# ---- UI ----
@app.get("/")
def home():
    badge = "DB: Postgres ✅" if USE_DB else "DB: RAM mode (geçici) ⚠️"
    return f"""
<!doctype html>
<html>
<head>
<title>Robot Reservation</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body {{ font-family: Arial; margin: 20px; }}
.badge {{ display:inline-block; padding:4px 8px; border:1px solid #ddd; border-radius:999px; font-size:12px; margin-bottom: 10px; }}

.controls {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }}

button {{ padding: 8px 12px; border: 0; border-radius: 8px; cursor: pointer; }}
#resetBtn {{ background: #d11; color: white; }}
#reserveBtn {{ background: #0b6; color: white; }}
#deleteBtn {{ background: #555; color: white; }}
#exportBtn {{ background: #224; color: white; }}

table {{ border-collapse: collapse; width: 100%; max-width: 1100px; }}
td, th {{ border: 1px solid #aaa; padding: 6px; text-align:center; }}
.free {{ background:#d9ffd9; }}
.taken {{ background:#ffb3b3; }}

#msg {{ margin: 8px 0 14px 0; font-weight: 700; }}
.small {{ font-size: 12px; opacity: 0.85; }}

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
  font-weight: 800;
  letter-spacing: 0.5px;
  z-index: 10000;
}}

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

#wrap {{
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 16px;
  align-items: start;
}}

#side {{
  position: sticky;
  top: 110px;
  border: 1px solid #ddd;
  border-radius: 12px;
  padding: 12px;
  background: #fff;
}}

#toast {{
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: rgba(0,0,0,0.85);
  color: white;
  padding: 10px 12px;
  border-radius: 10px;
  display:none;
  z-index: 10001;
  max-width: min(720px, 92vw);
}}

#gridWrap {{
  overflow-x: auto;
}}

.stickyTime {{
  position: sticky;
  left: 0;
  background: white;
  z-index: 2;
}}

.stickyHead {{
  position: sticky;
  top: 0;
  background: white;
  z-index: 3;
}}

#modal {{
  position: fixed;
  top:0; left:0;
  width:100%; height:100%;
  background:rgba(0,0,0,0.6);
  display:none;
  align-items:center;
  justify-content:center;
  z-index: 20000;
}}

#modalBox {{
  position: relative;
  background:white;
  padding:18px;
  border-radius:12px;
  width: min(520px, 92vw);
}}

.closeX {{
  position: absolute;
  top: 10px;
  right: 12px;
  font-size: 22px;
  cursor: pointer;
  background: transparent;
  border: none;
  line-height: 1;
}}

input, select {{
  padding: 7px 10px;
  border: 1px solid #aaa;
  border-radius: 8px;
}}

.row {{
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  align-items:center;
}}

</style>
</head>
<body>

<div id="clock"></div>
<div id="adminBadge">ADMIN ✅</div>
<div id="toast"></div>

<div class="badge">{badge}</div>

<h2>Robot Reservation</h2>

<div class="controls">
  <label>Gün:
    <select id="daySel" onchange="saveContextAndReload()">
      <option value="Day1">Day1</option>
      <option value="Day2">Day2</option>
    </select>
  </label>

  <label>Alan:
    <select id="areaSel" onchange="saveContextAndReload()">
      <option value="A">A</option>
      <option value="B">B</option>
    </select>
  </label>

  <label>Takım:
    <input id="team" type="number" style="width:120px;" />
  </label>

  <label>Aralık:
    <input id="range" placeholder="11:20-12:10" style="width:160px;" onblur="normalizeRange()" />
  </label>

  <label>Masa:
    <select id="table"></select>
  </label>

  <button id="reserveBtn" onclick="reserve()">Rezervasyon Al</button>
  <button id="resetBtn" onclick="resetTable()">Alanı Sıfırla</button>
  <button id="exportBtn" onclick="exportCsv()">CSV İndir</button>

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

  <button onclick="openAdminLogin()">Admin Login</button>
</div>

<p id="msg"></p>

<div id="wrap">
  <div id="gridWrap">
    <table id="grid"></table>
  </div>

  <div id="side">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <strong>Takım Paneli</strong>
      <button id="deleteBtn" onclick="copyWhatsapp()" style="padding:6px 10px;">WhatsApp Kopyala</button>
    </div>
    <p class="small" style="margin-top:6px;">
      Takım ara kısmına numara yazınca burada liste çıkacak.
    </p>
    <div id="teamPanel"></div>
  </div>
</div>

<!-- Admin Login Modal -->
<div id="adminLoginModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:21000; align-items:center; justify-content:center;">
  <div style="background:#fff; border-radius:12px; padding:16px; width:min(420px,92vw); position:relative;">
    <button class="closeX" onclick="closeAdminLogin()" aria-label="Kapat">✕</button>
    <h3 style="margin-top:0;">Admin Login</h3>
    <div class="row">
      <input id="adminTokenInput" type="password" placeholder="Admin şifresi" style="flex:1;" />
      <button id="reserveBtn" onclick="adminLogin()">Giriş</button>
    </div>
    <p class="small" style="margin-bottom:0;">Giriş 15 dk geçerlidir (bu tarayıcı sekmesinde).</p>
  </div>
</div>

<!-- Cell Action Modal -->
<div id="modal">
  <div id="modalBox">
    <button class="closeX" onclick="closeCellModal()" aria-label="Kapat">✕</button>
    <h3 style="margin-top:0;">Admin Hücre İşlemi</h3>

    <div class="row">
      <label>Masa:
        <input id="cellTable" readonly style="width:90px;" />
      </label>
      <label>Saat:
        <input id="cellTime" readonly style="width:140px;" />
      </label>
      <label>Slot Index:
        <input id="cellIdx" readonly style="width:90px;" />
      </label>
    </div>

    <div class="row" style="margin-top:10px;">
      <label>Takım:
        <input id="cellTeam" type="number" style="width:140px;" />
      </label>
      <label style="user-select:none;">
        <input id="cellOverwrite" type="checkbox" checked />
        Doluysa üstüne yaz
      </label>
    </div>

    <p class="small" id="cellInfo" style="margin-top:10px;"></p>

    <div class="row" style="margin-top:10px;">
      <button id="reserveBtn" onclick="adminApplyCell()">Kaydet</button>
      <button id="deleteBtn" onclick="adminDeleteCell()">Sil</button>
    </div>
  </div>
</div>

<script>
let tables = [];
let tableCount = {TABLE_COUNT_DEFAULT};
let slotLabels = [];
let lastState = null;
let lastClicked = null; // {{table, slot_index, timeLabel, existingTeam}}

function pad2(n) {{ return String(n).padStart(2, "0"); }}

function toast(msg) {{
  const t = document.getElementById("toast");
  t.innerText = msg;
  t.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => t.style.display = "none", 2400);
}}

function startClock() {{
  const el = document.getElementById("clock");
  function tick() {{
    const d = new Date();
    el.innerText = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }}
  tick();
  setInterval(tick, 250);
}}
startClock();

function normalizeRange() {{
  const el = document.getElementById("range");
  let v = (el.value || "").trim();
  if (!v) return;
  v = v.replaceAll(".", ":").replaceAll(" ", "");
  // 1120-1210 gibi yazıldıysa dokunmuyoruz; sadece yaygın hataları toparlıyoruz.
  el.value = v;
}}

function getContext() {{
  const day = document.getElementById("daySel").value;
  const area = document.getElementById("areaSel").value;
  return {{day, area}};
}}

function saveContextAndReload() {{
  const {{day, area}} = getContext();
  localStorage.setItem("ctx_day", day);
  localStorage.setItem("ctx_area", area);
  load();
}}

function restoreContext() {{
  const day = localStorage.getItem("ctx_day") || "Day1";
  const area = localStorage.getItem("ctx_area") || "A";
  document.getElementById("daySel").value = day;
  document.getElementById("areaSel").value = area;
}}

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

function buildSlots(slots) {{
  slotLabels = slots.map(s => s[0] + "-" + s[1]);
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

  if (!team) {{
    renderTeamPanel(null);
    return;
  }}

  grid.querySelectorAll("td[data-team]").forEach(td => {{
    const t = parseInt(td.getAttribute("data-team"), 10);
    if (t === team) td.classList.add("hl");
    else if (only) td.classList.add("dim");
  }});

  renderTeamPanel(team);
}}

function renderTeamPanel(team) {{
  const panel = document.getElementById("teamPanel");
  if (!lastState) {{
    panel.innerHTML = "<p class='small'>Veri yok.</p>";
    return;
  }}
  if (!team) {{
    panel.innerHTML = "<p class='small'>Takım arayınca burada rezervasyonlar listelenecek.</p>";
    return;
  }}

  const rows = lastState.reservations
    .filter(r => parseInt(r.team,10) === team)
    .map(r => {{
      const s = lastState.slots[r.slot_index];
      return {{
        table: r.table,
        slot_index: r.slot_index,
        time: s[0] + "-" + s[1]
      }};
    }});

  if (rows.length === 0) {{
    panel.innerHTML = "<p class='small'>Bu takım için rezervasyon yok.</p>";
    return;
  }}

  let html = "<ul style='margin:0; padding-left:18px;'>";
  rows.forEach(x => {{
    html += "<li><strong>Masa "+x.table+"</strong> — "+x.time+"</li>";
  }});
  html += "</ul>";
  panel.innerHTML = html;
}}

function copyWhatsapp() {{
  const team = getSearchTeam();
  if (!team || !lastState) {{
    toast("Önce takım ara kısmına takım numarası yaz.");
    return;
  }}
  const rows = lastState.reservations
    .filter(r => parseInt(r.team,10) === team)
    .map(r => {{
      const s = lastState.slots[r.slot_index];
      return "Takım " + team + " — Masa " + r.table + " — " + s[0] + "-" + s[1];
    }});
  if (rows.length === 0) {{
    toast("Bu takım için rezervasyon yok.");
    return;
  }}
  const text = rows.join("\\n");
  navigator.clipboard.writeText(text);
  toast("WhatsApp metni kopyalandı ✅");
  }}
// ---- ADMIN (15 dk session) ----
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

function openAdminLogin() {{
  document.getElementById("adminLoginModal").style.display = "flex";
  document.getElementById("adminTokenInput").value = "";
}}

function closeAdminLogin() {{
  document.getElementById("adminLoginModal").style.display = "none";
}}

function adminLogin() {{
  const t = document.getElementById("adminTokenInput").value;
  if (!t) return;
  sessionStorage.setItem("admin_token", t);
  sessionStorage.setItem("admin_token_exp", String(Date.now() + 15 * 60 * 1000));
  closeAdminLogin();
  toast("Admin giriş OK (15 dk) ✅");
  toggleAdminUI();
}}

function isAdminReady() {{
  return document.getElementById("adminMode").checked && !!getAdminToken();
}}

function toggleAdminUI() {{
  const badge = document.getElementById("adminBadge");
  badge.style.display = isAdminReady() ? "block" : "none";
}}

function exportCsv() {{
  const {{day, area}} = getContext();
  window.location.href = "/api/export.csv?day=" + encodeURIComponent(day) + "&area=" + encodeURIComponent(area);
}}

async function load() {{
  const {{day, area}} = getContext();
  const res = await fetch('/api/state?day=' + encodeURIComponent(day) + '&area=' + encodeURIComponent(area));
  const data = await res.json();
  lastState = data;

  buildSlots(data.slots);

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  let head = "<tr><th class='stickyHead stickyTime'>Saat</th>";
  tables.forEach(t => head += "<th class='stickyHead'>Masa "+t+"</th>");
  head += "</tr>";
  grid.innerHTML += head;

  const taken = new Map();
  data.reservations.forEach(r => {{
    taken.set(r.slot_index + "-" + r.table, r.team);
  }});

  data.slots.forEach((s,i) => {{
    let row = "<tr>";
    row += "<td class='stickyTime'>"+s[0]+"-"+s[1]+"</td>";

    tables.forEach(t => {{
      let key = i + "-" + t;
      const timeLabel = s[0]+"-"+s[1];
      if (taken.has(key)) {{
        const team = taken.get(key);
        row += "<td class='taken' data-team='"+team+"' data-table='"+t+"' data-slot='"+i+"' data-time='"+timeLabel+"'>Takım "+team+"</td>";
      }} else {{
        row += "<td class='free' data-table='"+t+"' data-slot='"+i+"' data-time='"+timeLabel+"'></td>";
      }}
    }});
    row += "</tr>";
    grid.innerHTML += row;
  }});

  // cell click (admin)
  grid.querySelectorAll("td[data-table]").forEach(td => {{
    td.addEventListener("click", () => {{
      if (!isAdminReady()) return; // normal kullanıcıda tıklama yok
      const table = td.getAttribute("data-table");
      const slot_index = parseInt(td.getAttribute("data-slot"), 10);
      const timeLabel = td.getAttribute("data-time");
      const existingTeam = td.getAttribute("data-team");
      openCellModal(table, slot_index, timeLabel, existingTeam ? parseInt(existingTeam,10) : null);
    }});
  }});

  applyTeamFilter();
  toggleAdminUI();
}}

function showMsg(ok, text) {{
  const el = document.getElementById("msg");
  el.innerText = (ok ? "✅ " : "❌ ") + text;
  toast(text);
}}

async function reserve() {{
  const team = parseInt(document.getElementById("team").value);
  const range = document.getElementById("range").value;
  const table = document.getElementById("table").value;
  const {{day, area}} = getContext();

  const res = await fetch('/api/reserve', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{
      team, range, table, table_count: tableCount,
      day, area
    }})
  }});

  const data = await res.json();
  if (!data.ok) {{
    showMsg(false, data.error);
  }} else {{
    showMsg(true, "Takım " + data.result.team + " → Masa " + data.result.table + " → " + data.result.slot[0] + "-" + data.result.slot[1]);
  }}
  load();
}}

async function resetTable() {{
  const token = prompt("Admin şifresi:");
  if (!token) return;
  const {{day, area}} = getContext();

  const res = await fetch('/api/reset', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{ token, day, area }})
  }});

  const data = await res.json();
  if (!data.ok) {{
    alert("❌ Şifre yanlış / yetkisiz!");
  }} else {{
    alert("✅ Alan sıfırlandı!");
    load();
  }}
}}

// ---- Cell modal ----
function openCellModal(table, slot_index, timeLabel, existingTeam) {{
  lastClicked = {{table, slot_index, timeLabel, existingTeam}};
  document.getElementById("cellTable").value = table;
  document.getElementById("cellIdx").value = String(slot_index);
  document.getElementById("cellTime").value = timeLabel;
  document.getElementById("cellTeam").value = existingTeam ? String(existingTeam) : "";
  document.getElementById("cellInfo").innerText = existingTeam
    ? ("Dolu: Takım " + existingTeam + ". İstersen değiştir veya sil.")
    : "Boş hücre: takım numarası girip kaydet.";
  document.getElementById("modal").style.display = "flex";
}}

function closeCellModal() {{
  document.getElementById("modal").style.display = "none";
}}

async function adminApplyCell() {{
  if (!lastClicked) return;
  const token = getAdminToken();
  if (!token) {{
    toast("Admin token yok. Admin Login yap.");
    return;
  }}

  const teamVal = document.getElementById("cellTeam").value.trim();
  if (!teamVal) {{
    toast("Takım numarası gir.");
    return;
  }}
  const team = parseInt(teamVal, 10);
  if (!Number.isFinite(team)) {{
    toast("Takım numarası geçersiz.");
    return;
  }}

  const overwrite = document.getElementById("cellOverwrite").checked;
  if (overwrite && lastClicked.existingTeam) {{
    const ok = confirm("Bu hücre dolu. Üstüne yazılsın mı?");
    if (!ok) return;
  }}

  const {{day, area}} = getContext();

  const res = await fetch('/api/reserve', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{
      team,
      range: "",
      table: lastClicked.table,
      table_count: tableCount,
      day, area,
      admin_token: token,
      slot_index: lastClicked.slot_index,
      overwrite: overwrite
    }})
  }});

  const data = await res.json();
  if (!data.ok) {{
    showMsg(false, data.error);
  }} else {{
    showMsg(true, "Admin: Takım " + data.result.team + " → Masa " + data.result.table + " → " + data.result.slot[0] + "-" + data.result.slot[1]);
    closeCellModal();
  }}
  load();
}}

async function adminDeleteCell() {{
  if (!lastClicked) return;
  const token = getAdminToken();
  if (!token) {{
    toast("Admin token yok. Admin Login yap.");
    return;
  }}
  if (!lastClicked.existingTeam) {{
    toast("Bu hücre zaten boş.");
    return;
  }}
  const ok = confirm("Rezervasyon silinsin mi?");
  if (!ok) return;

  const {{day, area}} = getContext();

  const res = await fetch('/api/delete', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{
      token,
      day, area,
      table: lastClicked.table,
      slot_index: lastClicked.slot_index
    }})
  }});
  const data = await res.json();
  if (!data.ok) {{
    showMsg(false, data.error);
  }} else {{
    showMsg(true, data.message);
    closeCellModal();
  }}
  load();
}}

buildTables(tableCount);
restoreContext();
load();

// Auto-refresh: 10 saniyede bir
setInterval(() => {{
  // admin login modal açıkken rahatsız etmesin
  const isLoginOpen = document.getElementById("adminLoginModal").style.display === "flex";
  const isCellOpen = document.getElementById("modal").style.display === "flex";
  if (!isLoginOpen && !isCellOpen) load();
}}, 10000);
</script>

</body>
</html>
"""


# ---- API ----
@app.get("/api/state")
def state():
    day = request.args.get("day", "Day1")
    area = request.args.get("area", "A")
    day, area = norm_day_area(day, area)

    return jsonify({
        "slots": slots,
        "reservations": get_reservations(day, area),
        "day": day,
        "area": area
    })


@app.post("/api/reserve")
def reserve():
    data = request.json or {}

    day = data.get("day", "Day1")
    area = data.get("area", "A")
    day, area = norm_day_area(day, area)

    team = data.get("team")
    pref_range = parse_range(data.get("range"))
    pref_table = data.get("table", "Auto")
    table_count = data.get("table_count", TABLE_COUNT_DEFAULT)

    # admin override
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

    res = get_reservations(day, area)

    # ADMIN: istediğin masa + istediğin slot
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
            delete_by_table_slot(day, area, pref_table, exact_slot_index)

        try:
            insert_reservation(day, area, team, pref_table, exact_slot_index)
        except Exception:
            return jsonify({"ok": False, "error": "Bu slot dolu (overwrite aç)"})

        s = slots[exact_slot_index]
        return jsonify({"ok": True, "result": {"team": team, "table": pref_table, "slot": s}})

    # Normal kullanıcı (admin değilse kurallar var; admin ise kurallar kalkar ama exact seçmiyorsa bile rahat bulsun)
    t, s, idx = find_slot(
        res, team, pref_range, pref_table, table_count,
        allow_past=is_admin,
        bypass_spacing=is_admin
    )

    if not t:
        return jsonify({"ok": False, "error": "Uygun slot bulunamadı"})

    try:
        insert_reservation(day, area, team, t, idx)
    except Exception:
        return jsonify({"ok": False, "error": "Slot az önce alındı / dolu, tekrar dene"})

    return jsonify({"ok": True, "result": {"team": team, "table": t, "slot": s}})


@app.post("/api/reset")
def reset():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    day = data.get("day", "Day1")
    area = data.get("area", "A")
    day, area = norm_day_area(day, area)

    reset_all(day, area)
    return jsonify({"ok": True})


@app.post("/api/delete")
def delete():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    day = data.get("day", "Day1")
    area = data.get("area", "A")
    day, area = norm_day_area(day, area)

    team = data.get("team", None)
    table = data.get("table", None)
    slot_index = data.get("slot_index", None)

    if isinstance(team, int):
        deleted = delete_by_team(day, area, team)
        return jsonify({"ok": True, "message": f"Takım {team} için {deleted} rezervasyon silindi."})

    if isinstance(table, str) and (isinstance(slot_index, int) or (isinstance(slot_index, str) and slot_index.isdigit())):
        slot_index = int(slot_index)
        deleted = delete_by_table_slot(day, area, table, slot_index)
        if deleted == 0:
            return jsonify({"ok": False, "error": "Bu masa+saat için rezervasyon bulunamadı."})
        return jsonify({"ok": True, "message": "Rezervasyon silindi."})

    return jsonify({"ok": False, "error": "Silme için ya team ya da table+slot_index göndermelisin."}), 400


@app.get("/api/export.csv")
def export_csv():
    day = request.args.get("day", "Day1")
    area = request.args.get("area", "A")
    day, area = norm_day_area(day, area)

    res = get_reservations(day, area)

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["day", "area", "team", "table", "time", "slot_index"])
    for r in res:
        s = slots[r["slot_index"]]
        w.writerow([day, area, r["team"], r["table"], f"{s[0]}-{s[1]}", r["slot_index"]])

    csv_text = output.getvalue()
    output.close()
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="reservations_{day}_{area}.csv"'}
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
