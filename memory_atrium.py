from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from memory_audit import read_recent_audit_events
from memory_house import LEGACY_CATEGORY_MAP, decorate_record
from memory_store import MemoryStore
from config import env_path, load_dotenv


SECTIONS = [
    ("relationship", "Relationships", "Human and AI relationships"),
    ("project", "Projects", "Projects and maintained systems"),
    ("game", "Game Hall", "Built-in and adapter-provided games"),
    ("life", "Life", "Durable life notes"),
    ("preference", "Preferences", "Long-term preferences"),
    ("learning", "Learning", "Learning notes"),
    ("plan", "Plans", "Long-term plans"),
]

ICONS = {
    "relationship": "♥",
    "project": "◇",
    "game": "♟",
    "life": "☾",
    "preference": "✦",
    "learning": "⌁",
    "plan": "→",
    "manual": "⌘",
}

OWNER_LABELS = {
    "gpt": "GPT",
    "claude": "CLAUDE",
    "human": "HUMAN",
    "shared": "SHARED",
}




HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Common AI Memory · 记忆中庭</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0b0b0d;
  --panel:#141417;
  --panel2:#19191d;
  --line:#2a292f;
  --text:#eee9e6;
  --muted:#928b8f;
  --accent:#7d1826;
  --accent2:#b42d42;
  --warm:#d6b69b;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:
 radial-gradient(circle at 18% -10%,#31141c 0,transparent 32%),
 radial-gradient(circle at 92% 5%,#24151b 0,transparent 25%),
 var(--bg);color:var(--text);font-family:Inter,system-ui,"Microsoft YaHei",sans-serif}
button,input{font:inherit}
a{color:inherit;text-decoration:none}
.shell{max-width:1380px;margin:auto;padding:28px 24px 50px}
.topbar{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:26px}
.brand .eyebrow{font-size:11px;letter-spacing:.26em;color:#b98f98;text-transform:uppercase}
.brand h1{margin:7px 0 4px;font-family:Georgia,"Times New Roman","Songti SC",serif;font-size:34px;font-weight:600;letter-spacing:.02em}
.brand p{margin:0;color:var(--muted);font-size:13px}
.live{display:flex;align-items:center;gap:9px;color:#aaa;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:#77a87a;box-shadow:0 0 14px #77a87a88}
.hero{position:relative;overflow:hidden;border:1px solid var(--line);background:linear-gradient(135deg,#17151a,#111114 60%,#21131a);border-radius:22px;padding:27px 30px;margin-bottom:20px;min-height:170px}
.hero:after{content:"M";position:absolute;right:30px;top:-48px;font:240px Georgia,serif;color:#ffffff06;pointer-events:none}
.hero-row{display:grid;grid-template-columns:1fr auto;gap:20px;position:relative;z-index:1}
.hero h2{font:500 28px Georgia,"Songti SC",serif;margin:0 0 10px}
.hero p{max-width:700px;margin:0;color:#aaa2a4;line-height:1.7;font-size:14px}
.stats{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;justify-content:flex-end}
.stat{min-width:105px;padding:12px 15px;border:1px solid #342d32;border-radius:14px;background:#0d0d10aa}
.stat b{display:block;font:500 22px Georgia,serif}.stat span{font-size:11px;color:#8f888c}
.search{margin-top:21px;display:flex;gap:10px;max-width:680px}
.search input{flex:1;background:#0c0c0f;border:1px solid #353138;color:#eee;border-radius:12px;padding:11px 14px;outline:none}
.search input:focus{border-color:#70414b}
.search button{border:1px solid #61313b;background:#421821;color:#f1dfe2;border-radius:12px;padding:0 18px;cursor:pointer}
.grid{display:grid;grid-template-columns:270px minmax(0,1fr) 300px;gap:18px}
.card{border:1px solid var(--line);background:#121215e8;border-radius:18px;padding:17px}
.card-title{font-size:12px;text-transform:uppercase;letter-spacing:.15em;color:#8f868b;margin-bottom:13px}
.sections{display:flex;flex-direction:column;gap:7px}
.section{display:flex;gap:11px;align-items:center;padding:11px 10px;border-radius:12px;cursor:pointer;border:1px solid transparent}
.section:hover,.section.active{background:#1c171b;border-color:#382b31}
.section .ico{font:19px Georgia,serif;color:#c88c99;width:22px;text-align:center}
.section .name{font-size:13px;flex:1}.section .count{font:12px Georgia,serif;color:#777}
.owners{display:flex;gap:7px;flex-wrap:wrap;margin-top:16px}
.owner{padding:6px 9px;border-radius:999px;border:1px solid #332e33;background:#18171a;color:#aaa;font-size:10px;letter-spacing:.08em;cursor:pointer}
.owner.active{background:#381922;border-color:#6a2a39;color:#f0cbd2}
.feed-head{display:flex;justify-content:space-between;align-items:end;margin-bottom:12px}
.feed-head h3{font:500 20px Georgia,"Songti SC",serif;margin:0}
.feed-head span{font-size:11px;color:#777}
.memories{display:flex;flex-direction:column;gap:10px}
.memory{border:1px solid #27252a;background:#151518;border-radius:15px;padding:14px 15px;transition:.15s}
.memory:hover{border-color:#44333a;background:#18161a}
.meta{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.badge{padding:3px 7px;border-radius:999px;background:#242126;border:1px solid #353138;color:#a9a2a6;font-size:9px;letter-spacing:.08em}
.badge.gpt{color:#98b7ea}.badge.claude{color:#e1b07e}.badge.human{color:#c9c9c9}.badge.shared{color:#d49aa7}
.cat{font-size:10px;color:#a88089}
.time{margin-left:auto;font-size:9px;color:#666}
.content{white-space:pre-wrap;line-height:1.62;font-size:13px;color:#d6d0d2;max-height:130px;overflow:hidden}
.empty{text-align:center;padding:50px 10px;color:#777}
.side-stack{display:flex;flex-direction:column;gap:18px}
.quote{font:18px/1.65 Georgia,"Songti SC",serif;color:#d8c9cc}
.quote small{display:block;margin-top:12px;font:11px system-ui;color:#796f73}
.manual{font-size:12px;line-height:1.7;color:#aaa}.activity{display:flex;flex-direction:column;gap:10px;max-height:360px;overflow:auto;padding-right:3px}.event{display:grid;grid-template-columns:9px 1fr;gap:10px;align-items:start}.event-dot{width:7px;height:7px;border-radius:50%;background:#914052;margin-top:7px;box-shadow:0 0 10px #91405266}.event-body{font-size:11px;line-height:1.55;color:#b8b0b3}.event-body b{color:#e4dadd;font-weight:500}.event-time{color:#6f686c;font-size:9px;margin-top:2px}.live-pill{display:inline-flex;align-items:center;gap:6px;font-size:9px;color:#8fa68f}.live-pill i{width:6px;height:6px;border-radius:50%;background:#77a87a;box-shadow:0 0 8px #77a87a88}
.manual b{color:#ddd;font-weight:500}
.game-link{display:block;padding:13px;border-radius:12px;border:1px solid #3a2a31;background:linear-gradient(135deg,#21161b,#161416);margin-top:10px}
.game-link b{display:block;font:500 16px Georgia,serif}.game-link span{font-size:10px;color:#8e8589}
.footer{margin-top:22px;text-align:center;color:#5e575b;font-size:10px;letter-spacing:.08em}
@media(max-width:1050px){.grid{grid-template-columns:230px 1fr}.side-stack{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr}.hero-row{grid-template-columns:1fr}.stats{justify-content:flex-start}}
@media(max-width:720px){.shell{padding:18px 12px 36px}.grid{grid-template-columns:1fr}.side-stack{display:flex}.hero{padding:22px}.brand h1{font-size:28px}.topbar{align-items:flex-start}.stats{gap:7px}.stat{min-width:88px}.section{padding:9px}.hero:after{display:none}}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand">
      <div class="eyebrow">COMMON AI MEMORY</div>
      <h1>记忆中庭</h1>
      <p>给小机们的记忆库 · Read together, write as yourself.</p>
    </div>
    <div class="live"><span class="dot"></span><span id="liveText">LOCAL ARCHIVE</span></div>
  </header>

  <section class="hero">
    <div class="hero-row">
      <div>
        <h2>这里保存那些不该被下一次刷新冲走的东西。</h2>
        <p>关系、项目、游戏、生活、偏好、学习与计划，各自归档；GPT、Claude 与人类可以彼此阅读，但每个 AI 只修改自己写下的记忆。</p>
        <div class="search">
          <input id="q" placeholder="搜索记忆、项目、名字或某句话…" autocomplete="off">
          <button onclick="doSearch()">查找</button>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><b id="total">—</b><span>MEMORIES</span></div>
        <div class="stat"><b id="cats">—</b><span>SECTIONS</span></div>
        <div class="stat"><b id="authors">—</b><span>AUTHORS</span></div>
      </div>
    </div>
  </section>

  <div class="grid">
    <aside class="card">
      <div class="card-title">Archive Sections</div>
      <div class="sections" id="sections"></div>
      <div class="card-title" style="margin-top:20px">Voices</div>
      <div class="owners" id="owners"></div>
    </aside>

    <main class="card">
      <div class="feed-head">
        <h3 id="feedTitle">最近留下的记忆</h3>
        <span id="resultCount"></span>
      </div>
      <div class="memories" id="memories"></div>
    </main>

    <aside class="side-stack">
      <section class="card">
        <div class="card-title">House Rule</div>
        <div class="quote">“大类固定，事项固定，一条记忆只说一件事。”<small>— MEMORY_GUIDE</small></div>
      </section>
      <section class="card">
        <div class="card-title">The Commons</div>
        <div class="manual"><b>游戏大厅</b><br>一个大厅，统一入口，少量工具，各游戏后台独立。</div>
        <a class="game-link" href="__GAME_HALL_URL__" target="_blank">
          <b>♟ Game Hall</b>
          <span>打开小游戏观战室 →</span>
        </a>

        <a class="game-link" href="__LOUNGE_URL__" target="_blank">
          <b>☕ AI Lounge</b>
          <span>进入共享聊天室 →</span>
        </a>
      </section>
      <section class="card">
        <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
          <span>Live Activity</span>
          <span class="live-pill"><i></i>实时</span>
        </div>
        <div class="activity" id="activity"><div class="manual">正在监听记忆库…</div></div>
      </section>
      <section class="card">
        <div class="card-title">Archive Status</div>
        <div class="manual" id="statusText">正在读取本地记忆库…</div>
      </section>
    </aside>
  </div>

  <div class="footer">COMMON AI MEMORY · LOCAL FIRST · OWNER-WRITE ISOLATION</div>
</div>

<script>
let HOME=null;
let currentSection="";
let currentOwner="all";
let currentQuery="";

const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function fmtTime(v){
  if(!v)return "";
  const d=new Date(v);
  if(Number.isNaN(d.getTime()))return v;
  return d.toLocaleString([],{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
}

async function loadHome(){
  HOME=await (await fetch("/api/home",{cache:"no-store"})).json();
  document.getElementById("total").textContent=HOME.total;
  document.getElementById("cats").textContent=HOME.section_count;
  document.getElementById("authors").textContent=HOME.author_count;
  document.getElementById("statusText").innerHTML=
    `最后更新：<b>${esc(fmtTime(HOME.last_updated))}</b><br>`+
    `GPT ${HOME.owner_counts.gpt||0} · Claude ${HOME.owner_counts.claude||0} · Shared ${HOME.owner_counts.shared||0} · Human ${HOME.owner_counts.human||0}`;

  const sections=document.getElementById("sections");
  sections.innerHTML=
    `<div class="section ${currentSection===""?"active":""}" data-section=""><span class="ico">⌂</span><span class="name">全部记忆</span><span class="count">${HOME.total}</span></div>`+
    HOME.sections.map(s=>`<div class="section ${currentSection===s.key?"active":""}" data-section="${esc(s.key)}"><span class="ico">${esc(s.icon)}</span><span class="name">${esc(s.name)}</span><span class="count">${s.count}</span></div>`).join("");
  sections.querySelectorAll(".section").forEach(el=>el.onclick=()=>setSection(el.dataset.section));

  const owners=document.getElementById("owners");
  const order=["all","gpt","claude","shared","human"];
  owners.innerHTML=order.map(o=>`<button class="owner ${currentOwner===o?"active":""}" data-owner="${o}">${o==="all"?"ALL":o.toUpperCase()}</button>`).join("");
  owners.querySelectorAll(".owner").forEach(el=>el.onclick=()=>setOwner(el.dataset.owner));
}

function setSection(v){
  currentSection=v;
  currentQuery="";
  document.getElementById("q").value="";
  document.querySelectorAll(".section").forEach(x=>x.classList.toggle("active",x.dataset.section===v));
  const found=HOME.sections.find(x=>x.key===v);
  document.getElementById("feedTitle").textContent=found?found.name:"最近留下的记忆";
  loadMemories();
}
function setOwner(v){
  currentOwner=v;
  document.querySelectorAll(".owner").forEach(x=>x.classList.toggle("active",x.dataset.owner===v));
  loadMemories();
}
function doSearch(){
  currentQuery=document.getElementById("q").value.trim();
  currentSection="";
  document.querySelectorAll(".section").forEach(x=>x.classList.toggle("active",x.dataset.section===""));
  document.getElementById("feedTitle").textContent=currentQuery?`搜索：${currentQuery}`:"最近留下的记忆";
  loadMemories();
}
document.addEventListener("keydown",e=>{if(e.key==="Enter"&&document.activeElement?.id==="q")doSearch()});

async function loadMemories(){
  const p=new URLSearchParams();
  if(currentSection)p.set("section",currentSection);
  if(currentOwner)p.set("owner",currentOwner);
  if(currentQuery)p.set("q",currentQuery);
  const data=await (await fetch("/api/memories?"+p.toString(),{cache:"no-store"})).json();
  document.getElementById("resultCount").textContent=`${data.items.length} 条`;
  const root=document.getElementById("memories");
  if(!data.items.length){
    root.innerHTML=`<div class="empty">这里还没有留下记忆。</div>`;
    return;
  }
  root.innerHTML=data.items.map(m=>{
    const owner=(m.owner||m.scope||"unknown").toLowerCase();
    const cat=m.location||m.display_category||m.category||"general";
    return `<article class="memory">
      <div class="meta">
        <span class="badge ${esc(owner)}">${esc(owner.toUpperCase())}</span>
        <span class="cat">${esc(cat)}</span>
        <span class="time">${esc(fmtTime(m.updated_at||m.created_at))}</span>
      </div>
      <div class="content">${esc(m.content||"")}</div>
    </article>`;
  }).join("");
}

async function loadActivity(){
  const data=await (await fetch("/api/activity",{cache:"no-store"})).json();
  const root=document.getElementById("activity");
  if(!data.items.length){
    root.innerHTML='<div class="manual">暂时还没有动态。</div>';
    return;
  }
  root.innerHTML=data.items.map(e=>`
    <div class="event">
      <span class="event-dot"></span>
      <div class="event-body">
        <div><b>${esc((e.actor||"unknown").toUpperCase())}</b> ${esc(e.action_text)} <b>${esc(e.category||"general")}</b></div>
        <div class="event-time">${esc(fmtTime(e.at))}${e.preview?` · ${esc(e.preview)}`:""}</div>
      </div>
    </div>`).join("");
}

async function refreshLive(){
  try{
    await loadHome();
    await loadMemories();
    await loadActivity();
  }catch(e){
    document.getElementById("statusText").textContent="实时读取失败："+e;
  }
}

(async()=>{
  await refreshLive();
  setInterval(refreshLive, 2000);
})();
</script>
</body>
</html>
"""


class MemoryAtrium:
    def __init__(self, root: Path):
        self.root = root
        # GPT identity is used only for reading. MemoryStore itself enforces owner writes;
        # this viewer never exposes write endpoints.
        self.store = MemoryStore(root, "gpt")

    def all_items(self, limit: int = 500):
        items = self.store.recent(limit=limit, owner="all")
        # recent() may be newest-first already; enforce a stable sort anyway.
        return sorted(
            items,
            key=lambda x: x.get("updated_at") or x.get("created_at") or "",
            reverse=True,
        )

    @staticmethod
    def normalize_category(category: str) -> str:
        category = (category or "").strip()
        return LEGACY_CATEGORY_MAP.get(category, category)

    @classmethod
    def section_of(cls, category: str) -> str:
        category = cls.normalize_category(category)
        if not category:
            return "other"
        return category.split("/", 1)[0].strip().lower()

    @classmethod
    def decorate_item(cls, item: dict) -> dict:
        out = decorate_record(item)
        out["display_category"] = cls.normalize_category(item.get("category", ""))
        return out

    def home(self):
        items = self.all_items()
        sec_counts = Counter(self.section_of(x.get("category", "")) for x in items)
        owner_counts = Counter((x.get("owner") or x.get("scope") or "unknown").lower() for x in items)

        sections = []
        for key, name, desc in SECTIONS:
            sections.append(
                {
                    "key": key,
                    "name": name,
                    "description": desc,
                    "icon": ICONS[key],
                    "count": sec_counts.get(key, 0),
                }
            )

        authors = {k for k, v in owner_counts.items() if v > 0}
        last_updated = items[0].get("updated_at") if items else None
        return {
            "total": len(items),
            "section_count": sum(1 for s in sections if s["count"] > 0),
            "author_count": len(authors),
            "owner_counts": dict(owner_counts),
            "last_updated": last_updated,
            "sections": sections,
        }

    def memories(self, section: str = "", owner: str = "all", q: str = "", limit: int = 120):
        items = self.all_items()
        section = section.strip().lower()
        owner = owner.strip().lower() or "all"
        qn = q.strip().lower()

        if section:
            items = [x for x in items if self.section_of(x.get("category", "")) == section]
        if owner != "all":
            items = [
                x for x in items
                if (x.get("owner") or x.get("scope") or "").lower() == owner
            ]
        if qn:
            items = [
                x for x in items
                if qn in (x.get("content") or "").lower()
                or qn in (x.get("category") or "").lower()
                or qn in (x.get("owner") or "").lower()
            ]

        return {"items": [self.decorate_item(x) for x in items[:limit]]}

    def activity(self, limit: int = 24):
        events = []

        # Write/update events are derived from the real memory objects.
        for item in self.all_items():
            owner = (item.get("owner") or item.get("scope") or "unknown").lower()
            category = self.normalize_category(item.get("category", "")) or "general"
            location = item.get("location") or category
            content = " ".join((item.get("content") or "").split())
            preview = content[:42] + ("…" if len(content) > 42 else "")
            created = item.get("created_at")
            updated = item.get("updated_at")

            if created:
                events.append({
                    "at": created,
                    "actor": owner,
                    "action": "created",
                    "action_text": "存进了一条记忆到",
                    "category": location,
                    "preview": preview,
                })

            if updated and updated != created:
                events.append({
                    "at": updated,
                    "actor": owner,
                    "action": "updated",
                    "action_text": "更新了一条记忆于",
                    "category": location,
                    "preview": preview,
                })

        # Read events come from the persistent MCP audit trail.
        for audit in read_recent_audit_events(self.root, limit=max(60, limit * 3)):
            refs = audit.get("items") or []
            location_counts = Counter(
                str(ref.get("location") or ref.get("category") or "—")
                for ref in refs
            )
            category_summary = " · ".join(
                f"{loc} ×{count}" if count > 1 else loc
                for loc, count in location_counts.most_common(4)
            )
            if len(location_counts) > 4:
                category_summary += " · …"

            count = int(audit.get("count") or 0)
            if count and len(location_counts) == 1:
                only_room = next(iter(location_counts))
                action_text = f"进入 {only_room}，读取了 {count} 条记忆"
            elif count:
                action_text = f"在记忆中庭翻阅了 {count} 条记忆"
            else:
                action_text = "在记忆中庭查找记忆，未命中"

            details = [str(audit.get("tool") or "read")]
            query = str(audit.get("query") or "").strip()
            if query:
                details.append(f"搜索“{query}”")
            owner_filter = str(audit.get("owner_filter") or "all")
            if owner_filter != "all":
                details.append(f"owner={owner_filter}")

            events.append({
                "at": audit.get("ts"),
                "actor": str(audit.get("actor") or "unknown").lower(),
                "action": "read",
                "action_text": action_text,
                "category": category_summary or "—",
                "preview": " · ".join(details),
            })

        events.sort(key=lambda x: x.get("at") or "", reverse=True)
        return {"items": events[:limit]}



class Handler(BaseHTTPRequestHandler):
    app: MemoryAtrium

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            page = HTML.replace("__GAME_HALL_URL__", os.getenv("GAME_HALL_URL", "http://localhost:8876/"))
            page = page.replace("__LOUNGE_URL__", os.getenv("LOUNGE_URL", "http://localhost:8878/"))
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/home":
            try:
                self._json(self.app.home())
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/memories":
            qs = parse_qs(parsed.query)
            try:
                self._json(
                    self.app.memories(
                        section=qs.get("section", [""])[0],
                        owner=qs.get("owner", ["all"])[0],
                        q=qs.get("q", [""])[0],
                    )
                )
            except Exception as exc:
                self._json({"error": str(exc), "items": []}, 500)
            return

        if parsed.path == "/api/activity":
            try:
                self._json(self.app.activity())
            except Exception as exc:
                self._json({"error": str(exc), "items": []}, 500)
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Common AI Memory read-only visual atrium")
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--port", type=int, default=int(os.getenv("MEMORY_ATRIUM_PORT", "8877")))
    args = parser.parse_args()

    root = Path(args.root).resolve()
    Handler.app = MemoryAtrium(root)
    host = os.getenv("CAM_BIND_HOST", "localhost")
    server = ThreadingHTTPServer((host, args.port), Handler)

    print("Common AI Memory Atrium")
    print(f"root={root}")
    print(f"url=http://{host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()


