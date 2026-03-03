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

# ---- DB ----
DATABASE_URL = os.getenv("DATABASE_URL")
USE_DB = bool(DATABASE_URL)

# RAM MODE (local için)
_reservations_mem = []  # {id, tournament_id, team, table, slot_index}
_next_id = 1

# ---- Turnuva default ----
DEFAULT_TOURNAMENT_ID = os.getenv("DEFAULT_TOURNAMENT_ID", "ist_ms_1")


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


def norm_tournament_id(tournament_id: str | None) -> str:
    tid = (tournament_id or "").strip()
    return tid if tid else DEFAULT_TOURNAMENT_ID


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
                    tournament_id TEXT NOT NULL,
                    team INTEGER NOT NULL,
                    "table" TEXT NOT NULL,
                    slot_index INTEGER NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            # eski tabloda tournament_id yoksa ekle
            cur.execute("ALTER TABLE reservations ADD COLUMN IF NOT EXISTS tournament_id TEXT NOT NULL DEFAULT 'ist_ms_1';")

            # unique index (aynı turnuvada aynı masa+slot sadece 1 kere)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS reservations_unique_slot_v2
                ON reservations(tournament_id, "table", slot_index);
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


def get_reservations(tournament_id: str):
    tournament_id = norm_tournament_id(tournament_id)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    'SELECT id, tournament_id, team, "table", slot_index FROM reservations WHERE tournament_id=%s ORDER BY slot_index;',
                    (tournament_id,)
                )
                return cur.fetchall()

    res = [r for r in _reservations_mem if r.get("tournament_id") == tournament_id]
    return sorted(res, key=lambda r: r["slot_index"])


def team_ok(res, team, idx):
    # 3 slot aralığı kuralı (admin değilse)
    return all(not (r["team"] == team and abs(r["slot_index"] - idx) < 3) for r in res)


def free(res, table, idx):
    return all(not (r["table"] == table and r["slot_index"] == idx) for r in res)


def find_slot(res, team, pref_range, pref_table, table_count: int, allow_past: bool, bypass_spacing: bool):
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


def insert_reservation(tournament_id: str, team: int, table: str, slot_index: int):
    global _next_id
    tournament_id = norm_tournament_id(tournament_id)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO reservations(tournament_id, team, "table", slot_index)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT (tournament_id, "table", slot_index) DO NOTHING
                    RETURNING id;
                """, (tournament_id, team, table, slot_index))
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("taken")
        return

    for r in _reservations_mem:
        if r["tournament_id"] == tournament_id and r["table"] == table and r["slot_index"] == slot_index:
            raise RuntimeError("taken")

    _reservations_mem.append({
        "id": _next_id,
        "tournament_id": tournament_id,
        "team": team,
        "table": table,
        "slot_index": slot_index
    })
    _next_id += 1


def delete_by_team(tournament_id: str, team: int) -> int:
    global _reservations_mem
    tournament_id = norm_tournament_id(tournament_id)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM reservations WHERE tournament_id=%s AND team=%s;', (tournament_id, team))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [r for r in _reservations_mem if not (r["tournament_id"] == tournament_id and r["team"] == team)]
    return before - len(_reservations_mem)


def delete_by_table_slot(tournament_id: str, table: str, slot_index: int) -> int:
    global _reservations_mem
    tournament_id = norm_tournament_id(tournament_id)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'DELETE FROM reservations WHERE tournament_id=%s AND "table"=%s AND slot_index=%s;',
                    (tournament_id, table, slot_index)
                )
                deleted = cur.rowcount
            conn.commit()
        return deleted

    before = len(_reservations_mem)
    _reservations_mem = [
        r for r in _reservations_mem
        if not (r["tournament_id"] == tournament_id and r["table"] == table and r["slot_index"] == slot_index)
    ]
    return before - len(_reservations_mem)


def reset_tournament(tournament_id: str):
    global _reservations_mem, _next_id
    tournament_id = norm_tournament_id(tournament_id)

    if USE_DB:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM reservations WHERE tournament_id=%s;", (tournament_id,))
            conn.commit()
        return

    _reservations_mem = [r for r in _reservations_mem if r["tournament_id"] != tournament_id]
    if not _reservations_mem:
        _next_id = 1


HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
<title>Robot Reservation</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body { font-family: Arial; margin: 20px; }
.badge { display:inline-block; padding:4px 8px; border:1px solid #ddd; border-radius:999px; font-size:12px; margin-bottom: 10px; }

.controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }

button { padding: 8px 12px; border: 0; border-radius: 10px; cursor: pointer; }
#resetBtn { background: #d11; color: white; }
#reserveBtn { background: #0b6; color: white; }
#deleteBtn { background: #555; color: white; }
#exportBtn { background: #224; color: white; }

table { border-collapse: collapse; width: 100%; max-width: 1100px; }
td, th { border: 1px solid #aaa; padding: 6px; text-align:center; }
.free { background:#d9ffd9; }
.taken { background:#ffb3b3; }

#msg { margin: 8px 0 14px 0; font-weight: 800; }
.small { font-size: 12px; opacity: 0.85; }

#clock {
  position: fixed;
  top: 14px;
  right: 16px;
  background: rgba(255,255,255,0.92);
  border: 1px solid #ddd;
  border-radius: 12px;
  padding: 8px 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  font-size: 18px;
  font-weight: 900;
  letter-spacing: 0.5px;
  z-index: 10000;
}
#adminBadge {
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
}

.searchBox { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
#teamSearch { width: 160px; padding: 7px 10px; border: 1px solid #aaa; border-radius: 10px; }

.hl { outline: 3px solid #000; }
.dim { opacity: 0.18; }

#wrap { display: grid; grid-template-columns: 1fr 320px; gap: 16px; align-items: start; }
#side {
  position: sticky;
  top: 110px;
  border: 1px solid #ddd;
  border-radius: 14px;
  padding: 12px;
  background: #fff;
}

#toast {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: rgba(0,0,0,0.85);
  color: white;
  padding: 10px 12px;
  border-radius: 12px;
  display:none;
  z-index: 10001;
  max-width: min(720px, 92vw);
}

#gridWrap { overflow-x: auto; }
.stickyTime { position: sticky; left: 0; background: white; z-index: 2; }
.stickyHead { position: sticky; top: 0; background: white; z-index: 3; }

input, select { padding: 7px 10px; border: 1px solid #aaa; border-radius: 10px; }
.row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }

/* Modal (masa sayısı + silme) */
#modal {
  position: fixed;
  top:0; left:0;
  width:100%; height:100%;
  background:rgba(0,0,0,0.6);
  display:none;
  align-items:center;
  justify-content:center;
  z-index: 20000;
}
#modalBox {
  position: relative;
  background:white;
  padding:18px;
  border-radius:14px;
  width: min(640px, 92vw);
}
.closeX {
  position: absolute;
  top: 10px;
  right: 12px;
  font-size: 22px;
  cursor: pointer;
  background: transparent;
  border: none;
  line-height: 1;
}
hr { border:none; border-top:1px solid #ddd; margin: 14px 0; }

/* Admin Login Modal */
#adminLoginModal {
  display:none;
  position:fixed;
  inset:0;
  background:rgba(0,0,0,.6);
  z-index:21000;
  align-items:center;
  justify-content:center;
}
#adminLoginBox {
  background:#fff;
  border-radius:14px;
  padding:16px;
  width:min(420px,92vw);
  position:relative;
}

#tMeta {
  margin-top: -4px;
  margin-bottom: 12px;
  opacity: 0.85;
}
</style>
</head>
<body>

<div id="clock"></div>
<div id="adminBadge">ADMIN ✅</div>
<div id="toast"></div>

<div class="badge">%%BADGE%%</div>

<h2>Robot Reservation</h2>

<div id="tMeta" class="small"></div>

<div class="controls">
  <label>Turnuva:
    <select id="tSel" onchange="saveTournamentAndReload()"></select>
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
  <button id="resetBtn" onclick="resetTournament()">Turnuvayı Sıfırla</button>
  <button id="exportBtn" onclick="exportCsv()">CSV İndir</button>
  <button onclick="openMasaModal()">Masa Sayısı / Silme</button>

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

<!-- Masa Sayısı + Silme Modal -->
<div id="modal">
  <div id="modalBox">
    <button class="closeX" onclick="closeMasaModal()" aria-label="Kapat">✕</button>

    <h3 style="margin-top:0;">Kaç masa var?</h3>
    <p class="small" style="margin-top:0;">İlk açılışta sorar. Sonra buradan değiştirebilirsin.</p>
    <div class="row">
      <input id="masaInput" type="number" min="1" max="60" style="width:120px;" />
      <button id="reserveBtn" onclick="saveMasa()">Kaydet</button>
      <button onclick="clearMasa()">Sıfırla (tekrar sor)</button>
    </div>

    <hr/>

    <h3 style="margin-top:0;">🗑️ Rezervasyon Sil (Admin)</h3>
    <p class="small">Takım numarasıyla toplu sil veya masa+saat ile tek sil.</p>

    <div class="row">
      <label>Takım:
        <input id="delTeam" type="number" style="width:120px;" />
      </label>

      <label>Masa:
        <select id="delTable"></select>
      </label>

      <label>Saat:
        <select id="delSlot"></select>
      </label>

      <button id="deleteBtn" onclick="deleteReservation()">Sil</button>
    </div>

    <p class="small" style="margin-bottom:0;">
      Not: Silme için admin şifresi gerekir. Admin Login yaptıysan sormadan kullanır, yoksa prompt açar.
    </p>
  </div>
</div>

<!-- Admin Login Modal -->
<div id="adminLoginModal">
  <div id="adminLoginBox">
    <button class="closeX" onclick="closeAdminLogin()" aria-label="Kapat">✕</button>
    <h3 style="margin-top:0;">Admin Login</h3>
    <div class="row">
      <input id="adminTokenInput" type="password" placeholder="Admin şifresi" style="flex:1;" />
      <button id="reserveBtn" onclick="adminLogin()">Giriş</button>
    </div>
    <p class="small" style="margin-bottom:0;">Giriş 15 dk geçerlidir (bu tarayıcı sekmesinde).</p>
  </div>
</div>

<script>
let tables = [];
let tableCount = %%TABLE_COUNT_DEFAULT%%;
let slotLabels = [];
let lastState = null;

// ✅ Turnuva listesi
const TOURNAMENTS = [
  { id: "ist_ms_1", label: "İstanbul • 7 Şubat • 1. İstanbul Ortaokul Yerel Turnuvası", city:"İstanbul", date:"7 Şubat Cumartesi", venue:"Yeditepe Üniversitesi" },
  { id: "ist_hs_1", label: "İstanbul • 8 Şubat • İstanbul Lise Yerel Turnuvası", city:"İstanbul", date:"8 Şubat Pazar", venue:"Yeditepe Üniversitesi" },
  { id: "ank_ms",   label: "Ankara • 7 Şubat • Ankara Ortaokul Yerel Turnuvası", city:"Ankara", date:"7 Şubat Cumartesi", venue:"Çankaya Üniversitesi" },
  { id: "ank_hs",   label: "Ankara • 8 Şubat • Ankara Lise Yerel Turnuvası", city:"Ankara", date:"8 Şubat Pazar", venue:"Çankaya Üniversitesi" },
  { id: "izm_ms_1", label: "İzmir • 14 Şubat • 1. İzmir Ortaokul Yerel Turnuvası", city:"İzmir", date:"14 Şubat Cumartesi", venue:"fuarizmir" },
  { id: "izm_hs_1", label: "İzmir • 15 Şubat • İzmir Lise Yerel Turnuvası", city:"İzmir", date:"15 Şubat Pazar", venue:"fuarizmir" },
  { id: "mer_ms",   label: "Mersin • 14 Şubat • Mersin Ortaokul Yerel Turnuvası", city:"Mersin", date:"14 Şubat Cumartesi", venue:"Yenişehir Belediyesi Atatürk Kültür Merkezi" },
  { id: "mer_hs",   label: "Mersin • 15 Şubat • Mersin Lise Yerel Turnuvası", city:"Mersin", date:"15 Şubat Pazar", venue:"Yenişehir Belediyesi Atatürk Kültür Merkezi" },
  { id: "ord_ms",   label: "Ordu • 14 Şubat • Ordu Ortaokul Yerel Turnuvası", city:"Ordu", date:"14 Şubat Cumartesi", venue:"Ordu Üniversitesi" },
  { id: "ord_hs",   label: "Ordu • 15 Şubat • Ordu Lise Yerel Turnuvası", city:"Ordu", date:"15 Şubat Pazar", venue:"Ordu Üniversitesi" },
  { id: "izm_ms_2", label: "İzmir • 21 Şubat • 2. İzmir Ortaokul Yerel Turnuvası", city:"İzmir", date:"21 Şubat Cumartesi", venue:"fuarizmir" },
  { id: "izm_ms_3", label: "İzmir • 22 Şubat • 3. İzmir Ortaokul Yerel Turnuvası", city:"İzmir", date:"22 Şubat Pazar", venue:"fuarizmir" },
  { id: "ant_ms",   label: "Antalya • 21 Şubat • Antalya Ortaokul Yerel Turnuvası", city:"Antalya", date:"21 Şubat Cumartesi", venue:"ANFAŞ – Uluslararası Fuar ve Kongre Merkezi" },
  { id: "ant_hs",   label: "Antalya • 22 Şubat • Antalya Lise Yerel Turnuvası", city:"Antalya", date:"22 Şubat Pazar", venue:"ANFAŞ – Uluslararası Fuar ve Kongre Merkezi" },
  { id: "ist_ms_2", label: "İstanbul • 28 Şubat • 2. İstanbul Ortaokul Yerel Turnuvası", city:"İstanbul", date:"28 Şubat Cumartesi", venue:"Gebze Teknik Üniversitesi" },
  { id: "ist_ms_3", label: "İstanbul • 1 Mart • 3. İstanbul Ortaokul Yerel Turnuvası", city:"İstanbul", date:"1 Mart Pazar", venue:"Gebze Teknik Üniversitesi" },
  { id: "esk_ms",   label: "Eskişehir • 28 Şubat • Eskişehir Ortaokul Yerel Turnuvası", city:"Eskişehir", date:"28 Şubat Cumartesi", venue:"Atayurt Okulları" },
  { id: "esk_hs",   label: "Eskişehir • 1 Mart • Eskişehir Lise Yerel Turnuvası", city:"Eskişehir", date:"1 Mart Pazar", venue:"Atayurt Okulları" },
  { id: "nat",      label: "İzmir • 7-8 Mart • Ulusal Turnuva", city:"İzmir", date:"7-8 Mart", venue:"fuarizmir" },
];

function pad2(n) { return String(n).padStart(2, "0"); }

function toast(msg) {
  const t = document.getElementById("toast");
  t.innerText = msg;
  t.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => t.style.display = "none", 2400);
}

function startClock() {
  const el = document.getElementById("clock");
  function tick() {
    const d = new Date();
    el.innerText = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  tick();
  setInterval(tick, 250);
}
startClock();

function normalizeRange() {
  const el = document.getElementById("range");
  let v = (el.value || "").trim();
  if (!v) return;
  v = v.replaceAll(".", ":").replaceAll(" ", "");
  el.value = v;
}

/* ---- TURNOVA ---- */
function getTournamentId() {
  return document.getElementById("tSel").value;
}

function findTournament(tid) {
  return TOURNAMENTS.find(x => x.id === tid) || TOURNAMENTS[0];
}

function renderTournamentMeta() {
  const tid = getTournamentId();
  const t = findTournament(tid);
  document.getElementById("tMeta").innerText = "Mekan: " + t.venue + "  |  Şehir: " + t.city + "  |  Tarih: " + t.date;
}

function buildTournamentSelect() {
  const sel = document.getElementById("tSel");
  sel.innerHTML = "";
  TOURNAMENTS.forEach(t => {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = t.label;
    sel.appendChild(opt);
  });
}

function restoreTournament() {
  const saved = localStorage.getItem("ctx_tournament_id") || "";
  const exists = TOURNAMENTS.some(t => t.id === saved);
  document.getElementById("tSel").value = exists ? saved : TOURNAMENTS[0].id;
  renderTournamentMeta();
}

function saveTournamentAndReload() {
  const tid = getTournamentId();
  localStorage.setItem("ctx_tournament_id", tid);
  renderTournamentMeta();
  load();
}

/* ---- MASA SAYISI (localStorage) ---- */
function getSavedMasa() {
  const v = localStorage.getItem("masa_sayisi");
  if (!v) return null;
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n < 1 || n > 60) return null;
  return n;
}

function buildTables(n) {
  tableCount = n;
  tables = [];

  const select = document.getElementById("table");
  const delSelect = document.getElementById("delTable");

  select.innerHTML = "<option>Auto</option>";
  delSelect.innerHTML = "<option>Seç</option>";

  for (let i=1; i<=n; i++) {
    tables.push(String(i));
    select.innerHTML += "<option>"+i+"</option>";
    delSelect.innerHTML += "<option>"+i+"</option>";
  }
}

function openMasaModal() {
  document.getElementById("modal").style.display = "flex";
  const cur = getSavedMasa() || tableCount;
  document.getElementById("masaInput").value = String(cur);
}

function closeMasaModal() {
  document.getElementById("modal").style.display = "none";
}

function saveMasa() {
  const n = parseInt(document.getElementById("masaInput").value, 10);
  if (!Number.isFinite(n) || n < 1 || n > 60) {
    alert("Geçersiz masa sayısı");
    return;
  }
  localStorage.setItem("masa_sayisi", String(n));
  buildTables(n);
  closeMasaModal();
  load();
}

function clearMasa() {
  localStorage.removeItem("masa_sayisi");
  alert("Masa sayısı sıfırlandı. Sayfayı yenilersen tekrar sorar.");
}

/* ---- TAKIM PANELİ / FİLTRE ---- */
function getSearchTeam() {
  const v = (document.getElementById("teamSearch")?.value || "").trim();
  if (!v) return null;
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}

function applyTeamFilter() {
  const team = getSearchTeam();
  const only = document.getElementById("onlyThisTeam")?.checked;

  const grid = document.getElementById("grid");
  if (!grid) return;

  grid.querySelectorAll("td").forEach(td => {
    td.classList.remove("hl");
    td.classList.remove("dim");
  });

  if (!team) {
    renderTeamPanel(null);
    return;
  }

  grid.querySelectorAll("td[data-team]").forEach(td => {
    const t = parseInt(td.getAttribute("data-team"), 10);
    if (t === team) td.classList.add("hl");
    else if (only) td.classList.add("dim");
  });

  renderTeamPanel(team);
}

function renderTeamPanel(team) {
  const panel = document.getElementById("teamPanel");
  if (!lastState) {
    panel.innerHTML = "<p class='small'>Veri yok.</p>";
    return;
  }
  if (!team) {
    panel.innerHTML = "<p class='small'>Takım arayınca burada rezervasyonlar listelenecek.</p>";
    return;
  }

  const rows = lastState.reservations
    .filter(r => parseInt(r.team,10) === team)
    .map(r => {
      const s = lastState.slots[r.slot_index];
      return { table: r.table, time: s[0] + "-" + s[1] };
    });

  if (rows.length === 0) {
    panel.innerHTML = "<p class='small'>Bu takım için rezervasyon yok.</p>";
    return;
  }

  let html = "<ul style='margin:0; padding-left:18px;'>";
  rows.forEach(x => {
    html += "<li><strong>Masa "+x.table+"</strong> — "+x.time+"</li>";
  });
  html += "</ul>";
  panel.innerHTML = html;
}

function copyWhatsapp() {
  const team = getSearchTeam();
  if (!team || !lastState) {
    toast("Önce takım ara kısmına takım numarası yaz.");
    return;
  }
  const rows = lastState.reservations
    .filter(r => parseInt(r.team,10) === team)
    .map(r => {
      const s = lastState.slots[r.slot_index];
      return "Takım " + team + " — Masa " + r.table + " — " + s[0] + "-" + s[1];
    });
  if (rows.length === 0) {
    toast("Bu takım için rezervasyon yok.");
    return;
  }
  navigator.clipboard.writeText(rows.join("\n"));
  toast("WhatsApp metni kopyalandı ✅");
}

/* ---- ADMIN TOKEN (15 dk) ---- */
function getAdminToken() {
  const raw = sessionStorage.getItem("admin_token") || "";
  const exp = parseInt(sessionStorage.getItem("admin_token_exp") || "0", 10);
  if (!raw) return "";
  if (Date.now() > exp) {
    sessionStorage.removeItem("admin_token");
    sessionStorage.removeItem("admin_token_exp");
    return "";
  }
  return raw;
}

function openAdminLogin() {
  document.getElementById("adminLoginModal").style.display = "flex";
  document.getElementById("adminTokenInput").value = "";
}

function closeAdminLogin() {
  document.getElementById("adminLoginModal").style.display = "none";
}

function adminLogin() {
  const t = document.getElementById("adminTokenInput").value;
  if (!t) return;
  sessionStorage.setItem("admin_token", t);
  sessionStorage.setItem("admin_token_exp", String(Date.now() + 15 * 60 * 1000));
  closeAdminLogin();
  toast("Admin giriş OK (15 dk) ✅");
  toggleAdminUI();
}

function isAdminReady() {
  return document.getElementById("adminMode").checked && !!getAdminToken();
}

function toggleAdminUI() {
  const badge = document.getElementById("adminBadge");
  badge.style.display = isAdminReady() ? "block" : "none";
}

/* ---- LOAD / GRID ---- */
function buildSlotsForDelete(slots) {
  slotLabels = slots.map(s => s[0] + "-" + s[1]);
  const sel = document.getElementById("delSlot");
  sel.innerHTML = "<option>Seç</option>";
  slotLabels.forEach(lbl => { sel.innerHTML += "<option>"+lbl+"</option>"; });
}

async function load() {
  const tid = getTournamentId();
  const res = await fetch('/api/state?tournament_id=' + encodeURIComponent(tid));
  const data = await res.json();
  lastState = data;

  buildSlotsForDelete(data.slots);

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  let head = "<tr><th class='stickyHead stickyTime'>Saat</th>";
  tables.forEach(t => head += "<th class='stickyHead'>Masa "+t+"</th>");
  head += "</tr>";
  grid.innerHTML += head;

  const taken = new Map();
  data.reservations.forEach(r => { taken.set(r.slot_index + "-" + r.table, r.team); });

  data.slots.forEach((s,i) => {
    let row = "<tr>";
    row += "<td class='stickyTime'>"+s[0]+"-"+s[1]+"</td>";
    tables.forEach(t => {
      let key = i + "-" + t;
      const timeLabel = s[0]+"-"+s[1];
      if (taken.has(key)) {
        const team = taken.get(key);
        row += "<td class='taken' data-team='"+team+"' data-table='"+t+"' data-slot='"+i+"' data-time='"+timeLabel+"'>Takım "+team+"</td>";
      } else {
        row += "<td class='free' data-table='"+t+"' data-slot='"+i+"' data-time='"+timeLabel+"'></td>";
      }
    });
    row += "</tr>";
    grid.innerHTML += row;
  });

  applyTeamFilter();
  toggleAdminUI();
  renderTournamentMeta();
}

function showMsg(ok, text) {
  const el = document.getElementById("msg");
  el.innerText = (ok ? "✅ " : "❌ ") + text;
  toast(text);
}

async function reserve() {
  const team = parseInt(document.getElementById("team").value);
  const range = document.getElementById("range").value;
  const table = document.getElementById("table").value;
  const tournament_id = getTournamentId();

  const admin_token = getAdminToken();

  const res = await fetch('/api/reserve', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      tournament_id,
      team, range, table, table_count: tableCount,
      admin_token: admin_token
    })
  });

  const data = await res.json();
  if (!data.ok) showMsg(false, data.error);
  else showMsg(true, "Takım " + data.result.team + " → Masa " + data.result.table + " → " + data.result.slot[0] + "-" + data.result.slot[1]);
  load();
}

async function resetTournament() {
  let token = getAdminToken();
  if (!token) token = prompt("Admin şifresi:");
  if (!token) return;

  const tournament_id = getTournamentId();

  const res = await fetch('/api/reset', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ token, tournament_id })
  });

  const data = await res.json();
  if (!data.ok) alert("❌ Şifre yanlış / yetkisiz!");
  else { alert("✅ Turnuva sıfırlandı!"); load(); }
}

function exportCsv() {
  const tournament_id = getTournamentId();
  window.location.href = "/api/export.csv?tournament_id=" + encodeURIComponent(tournament_id);
}

/* ---- SİLME (aynısı) ---- */
async function deleteReservation() {
  let token = getAdminToken();
  if (!token) token = prompt("Admin şifresi:");
  if (!token) return;

  const tournament_id = getTournamentId();

  const delTeam = document.getElementById("delTeam").value;
  const delTable = document.getElementById("delTable").value;
  const delSlot = document.getElementById("delSlot").value;

  if (delTeam) {
    const team = parseInt(delTeam, 10);
    const res = await fetch('/api/delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ token, tournament_id, team })
    });
    const data = await res.json();
    if (!data.ok) alert("❌ " + data.error);
    else alert("✅ " + data.message);
    document.getElementById("delTeam").value = "";
    load();
    return;
  }

  if (delTable === "Seç" || delSlot === "Seç") {
    alert("Takım gir ya da Masa + Saat seç.");
    return;
  }

  const slot_index = slotLabels.indexOf(delSlot);
  if (slot_index < 0) {
    alert("Saat bulunamadı.");
    return;
  }

  const res = await fetch('/api/delete', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ token, tournament_id, table: delTable, slot_index })
  });
  const data = await res.json();
  if (!data.ok) alert("❌ " + data.error);
  else alert("✅ " + data.message);
  load();
}

/* ---- INIT ---- */
buildTournamentSelect();
restoreTournament();

const savedMasa = getSavedMasa();
buildTables(savedMasa || tableCount);

if (!savedMasa) {
  openMasaModal();
}

load();

// 10 sn auto refresh
setInterval(() => {
  const isM = document.getElementById("modal").style.display === "flex";
  const isA = document.getElementById("adminLoginModal").style.display === "flex";
  if (!isM && !isA) load();
}, 10000);

// modal dışına tıklayınca kapat
window.addEventListener("click", (e) => {
  const modal = document.getElementById("modal");
  if (e.target === modal) closeMasaModal();

  const am = document.getElementById("adminLoginModal");
  if (e.target === am) closeAdminLogin();
});
</script>

</body>
</html>
"""


# ---- UI ----
@app.get("/")
def home():
    badge = "DB: Postgres ✅" if USE_DB else "DB: RAM mode (geçici) ⚠️"
    html = HTML_TEMPLATE.replace("%%BADGE%%", badge).replace("%%TABLE_COUNT_DEFAULT%%", str(TABLE_COUNT_DEFAULT))
    return html


# ---- API ----
@app.get("/api/state")
def state():
    tournament_id = norm_tournament_id(request.args.get("tournament_id", DEFAULT_TOURNAMENT_ID))
    return jsonify({
        "slots": slots,
        "reservations": get_reservations(tournament_id),
        "tournament_id": tournament_id
    })


@app.post("/api/reserve")
def reserve():
    data = request.json or {}

    tournament_id = norm_tournament_id(data.get("tournament_id", DEFAULT_TOURNAMENT_ID))

    team = data.get("team")
    pref_range = parse_range(data.get("range"))
    pref_table = data.get("table", "Auto")
    table_count = data.get("table_count", TABLE_COUNT_DEFAULT)

    admin_token = data.get("admin_token", "")
    is_admin = (admin_token == ADMIN_TOKEN)

    if not isinstance(team, int):
        return jsonify({"ok": False, "error": "Takım numarası sayı olmalı"})

    try:
        table_count = int(table_count)
        if table_count < 1 or table_count > 60:
            return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})
    except Exception:
        return jsonify({"ok": False, "error": "Masa sayısı geçersiz"})

    res = get_reservations(tournament_id)

    t, s, idx = find_slot(
        res,
        team,
        pref_range,
        pref_table,
        table_count,
        allow_past=is_admin,       # admin geçmişe de yazabilir
        bypass_spacing=is_admin    # admin takım aralığı kuralını bypass eder
    )

    if not t:
        return jsonify({"ok": False, "error": "Uygun slot bulunamadı"})

    try:
        insert_reservation(tournament_id, team, t, idx)
    except Exception:
        return jsonify({"ok": False, "error": "Slot dolu / az önce alındı, tekrar dene"})

    return jsonify({"ok": True, "result": {"team": team, "table": t, "slot": s}})


@app.post("/api/reset")
def reset():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    tournament_id = norm_tournament_id(data.get("tournament_id", DEFAULT_TOURNAMENT_ID))
    reset_tournament(tournament_id)
    return jsonify({"ok": True})


@app.post("/api/delete")
def delete():
    data = request.json or {}
    token = data.get("token", "")

    if token != ADMIN_TOKEN:
        return jsonify({"ok": False, "error": "Yetkisiz"}), 401

    tournament_id = norm_tournament_id(data.get("tournament_id", DEFAULT_TOURNAMENT_ID))

    team = data.get("team", None)
    table = data.get("table", None)
    slot_index = data.get("slot_index", None)

    if isinstance(team, int):
        deleted = delete_by_team(tournament_id, team)
        return jsonify({"ok": True, "message": f"Takım {team} için {deleted} rezervasyon silindi."})

    if isinstance(table, str) and (isinstance(slot_index, int) or (isinstance(slot_index, str) and str(slot_index).isdigit())):
        slot_index = int(slot_index)
        deleted = delete_by_table_slot(tournament_id, table, slot_index)
        if deleted == 0:
            return jsonify({"ok": False, "error": "Bu masa+saat için rezervasyon bulunamadı."})
        return jsonify({"ok": True, "message": "Rezervasyon silindi."})

    return jsonify({"ok": False, "error": "Silme için ya team ya da table+slot_index göndermelisin."}), 400


@app.get("/api/export.csv")
def export_csv():
    tournament_id = norm_tournament_id(request.args.get("tournament_id", DEFAULT_TOURNAMENT_ID))
    res = get_reservations(tournament_id)

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["tournament_id", "team", "table", "time", "slot_index"])
    for r in res:
        s = slots[r["slot_index"]]
        w.writerow([tournament_id, r["team"], r["table"], f"{s[0]}-{s[1]}", r["slot_index"]])

    csv_text = output.getvalue()
    output.close()
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="reservations_{tournament_id}.csv"'}
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)