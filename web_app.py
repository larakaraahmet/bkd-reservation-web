from flask import Flask, request, jsonify, Response
from datetime import datetime, timedelta
import os, csv
from io import StringIO
import psycopg
import psycopg.rows

app = Flask(__name__)

START = "09:50"
END = "17:00"
STEP_MIN = 10

TABLE_COUNT = int(os.getenv("TABLE_COUNT", "5"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "secret")

tables = [str(i) for i in range(1, TABLE_COUNT + 1)]

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

@app.before_request
def startup():
    if not hasattr(app, "_db_inited"):
        init_db()
        app._db_inited = True


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
    except:
        return None

def get_reservations():
    with db_conn() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute('SELECT team, "table", slot_index FROM reservations ORDER BY slot_index;')
            return cur.fetchall()

def team_ok(res, team, idx):
    return all(not (r["team"] == team and abs(r["slot_index"] - idx) < 3) for r in res)

def free(res, table, idx):
    return all(not (r["table"] == table and r["slot_index"] == idx) for r in res)

def find_slot(res, team, pref_range, pref_table):
    for i, s in enumerate(slots):
        st = to_dt(s[0])
        if pref_range:
            rs, re = pref_range
            if not (rs <= st < re):
                continue

        table_list = [pref_table] if pref_table != "Auto" else tables
        for t in table_list:
            if free(res, t, i) and team_ok(res, team, i):
                return t, s, i
    return None, None, None

@app.get("/")
def home():
    return f"""
<html>
<head>
<title>Robot Reservation</title>
<style>
body {{ font-family: Arial; margin: 20px; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #aaa; padding: 6px; text-align:center; }}
.free {{ background:#d9ffd9; }}
.taken {{ background:#ffb3b3; }}

#modal {{
    position: fixed;
    top:0; left:0;
    width:100%; height:100%;
    background:rgba(0,0,0,0.6);
    display:flex;
    align-items:center;
    justify-content:center;
}}

#modalBox {{
    background:white;
    padding:20px;
    border-radius:8px;
}}
</style>
</head>
<body>

<div id="modal" style="display:none;">
  <div id="modalBox">
    <h3>Kaç masa var?</h3>
    <input id="masaInput" type="number" min="1">
    <button onclick="saveMasa()">Kaydet</button>
  </div>
</div>

<h2>Robot Reservation</h2>

Takım: <input id="team" type="number">
Aralık: <input id="range" placeholder="11:20-12:10">
Masa:
<select id="table"></select>

<button onclick="reserve()">Rezervasyon Al</button>

<p id="msg"></p>
<table id="grid"></table>

<script>

let tables = [];

function initTables() {{
    let saved = localStorage.getItem("masa_sayisi");

    if(!saved) {{
        document.getElementById("modal").style.display="flex";
        return;
    }}

    buildTables(parseInt(saved));
}}

function saveMasa() {{
    const n = parseInt(document.getElementById("masaInput").value);
    if(!n || n<1) return;
    localStorage.setItem("masa_sayisi", n);
    document.getElementById("modal").style.display="none";
    buildTables(n);
}}

function buildTables(n) {{
    tables = [];
    const select = document.getElementById("table");
    select.innerHTML = "<option>Auto</option>";
    for(let i=1;i<=n;i++) {{
        tables.push(String(i));
        select.innerHTML += "<option>"+i+"</option>";
    }}
    load();
}}

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
        taken.set(r.slot_index+"-"+r.table, r.team);
    }});

    data.slots.forEach((s,i) => {{
        let row = "<tr><td>"+s[0]+"-"+s[1]+"</td>";
        tables.forEach(t => {{
            let key = i+"-"+t;
            if(taken.has(key))
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
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{team,range,table}})
    }});
    const data = await res.json();

    if(!data.ok)
        document.getElementById("msg").innerText="❌ "+data.error;
    else
        document.getElementById("msg").innerText="✅ Takım "+data.result.team+
            " → Masa "+data.result.table+
            " → "+data.result.slot[0]+"-"+data.result.slot[1];

    load();
}}

initTables();

</script>
</body>
</html>
"""


@app.get("/api/state")
def state():
    return jsonify({"tables":tables,"slots":slots,"reservations":get_reservations()})

@app.post("/api/reserve")
def reserve():
    data=request.json
    team=data.get("team")
    pref_range=parse_range(data.get("range"))
    pref_table=data.get("table","Auto")

    if not isinstance(team,int):
        return jsonify({"ok":False,"error":"Takım numarası sayı olmalı"})

    res=get_reservations()
    t,s,idx=find_slot(res,team,pref_range,pref_table)

    if not t:
        return jsonify({"ok":False,"error":"Uygun slot bulunamadı"})

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO reservations(team,"table",slot_index) VALUES (%s,%s,%s);',(team,t,idx))
            conn.commit()
    except:
        return jsonify({"ok":False,"error":"Slot az önce alındı, tekrar dene"})

    return jsonify({"ok":True,"result":{"team":team,"table":t,"slot":s}})

if __name__=="__main__":
    init_db()
    app.run(debug=True)
