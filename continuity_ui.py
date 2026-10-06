"""Read-only UI pages for continuity: 动态 (timeline), 断点 (handoffs), 快照 (snapshots) and a Relay status panel.

Everything here is a view. Nothing calls a model, nothing restores, nothing enables Relay. Snapshot
creation and restore stay with the MCP tools and the CLI; relay settings stay in owner-config.json.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

API_PREFIX = "/api/continuity"
VIEWS = {"timeline": "/timeline", "handoffs": "/handoffs", "snapshots": "/snapshots"}
RANGES = {"today": None, "7d": 7, "30d": 30}
KIND_GROUPS = ("memory", "dream", "handoff", "snapshot", "relay")
PLAN_PREVIEW = 20
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _since(range_key: str) -> str:
    now = datetime.now().astimezone()
    days = RANGES.get(range_key, None) if range_key in RANGES else 7
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) if days is None else now - timedelta(days=days)
    return start.isoformat()


def api(root: Path, path: str, query: str) -> tuple[int, dict[str, Any]]:
    """GET-only JSON API. ``path`` is the part after API_PREFIX."""
    params = {k: v[-1] for k, v in parse_qs(query or "").items()}
    parts = [p for p in path.strip("/").split("/") if p]
    if parts == ["changes"]:
        import timeline

        kind = params.get("kind", "")
        kinds = [kind] if kind in KIND_GROUPS else None
        result = timeline.changes(root, since=_since(params.get("range", "7d")), kinds=kinds, limit=100)
        return 200, result
    if parts == ["handoffs"]:
        if not (root / "state" / "handoffs.sqlite3").is_file():
            return 200, {"ok": True, "handoffs": []}
        from handoffs import HandoffStore

        return 200, HandoffStore(root, "viewer").list(owner="all", limit=50)
    if len(parts) == 2 and parts[0] == "handoffs":
        if not (root / "state" / "handoffs.sqlite3").is_file():
            return 404, {"ok": False, "error": "not found"}
        from handoffs import HandoffStore

        result = HandoffStore(root, "viewer").get(parts[1])
        return (200 if result.get("ok") else 404), result
    if parts and parts[0] == "snapshots":
        from snapshots import SnapshotError, SnapshotManager

        manager = SnapshotManager(root)
        if len(parts) == 1:
            return 200, manager.list()
        if len(parts) == 3 and _SNAPSHOT_ID.match(parts[1]) and parts[2] in ("verify", "plan"):
            try:
                if parts[2] == "verify":
                    return 200, manager.verify(parts[1])
                plan = manager.restore_plan(parts[1])
            except SnapshotError as exc:
                return 404, {"ok": False, "error": str(exc)}
            preview = {k: {"count": len(plan[k]), "first": plan[k][:PLAN_PREVIEW]} for k in ("add", "overwrite", "remove")}
            return 200, {"ok": True, "snapshot_id": plan["snapshot_id"], "created_at": plan["created_at"],
                         **preview, "unchanged": plan["unchanged"], "sqlite": plan["sqlite"],
                         "derived_indexes_rebuilt": plan["derived_indexes_rebuilt"], "plan_digest": plan["plan_digest"]}
    if parts == ["relay"]:
        from relay import Relay

        return 200, Relay(root).status()
    return 404, {"ok": False, "error": "not found"}


HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Continuity</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:18px 20px 40px;font:13px/1.5 Inter,system-ui,"Microsoft YaHei",sans-serif;background:var(--cam-bg,#f2f2ee);color:var(--cam-text,#11110f)}
h1{font-size:15px;margin:0 0 4px}
.sub{color:var(--cam-muted,#777);margin:0 0 14px;font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:12px}
button,select{font:inherit;color:var(--cam-text,#111);background:var(--cam-surface,#fff);border:1px solid var(--cam-line-strong,#bbb);border-radius:var(--cam-control-radius,4px);padding:5px 10px;cursor:pointer}
button[aria-pressed="true"]{background:var(--cam-accent,#111);color:var(--cam-accent-text,#fff);border-color:var(--cam-accent,#111)}
.card{background:var(--cam-surface,#fff);border:1px solid var(--cam-line,#d6d6cf);border-radius:var(--cam-radius,0);box-shadow:var(--cam-shadow,none);padding:12px 14px;margin-bottom:10px;backdrop-filter:blur(var(--cam-blur,0))}
.row{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--cam-line,#e2e2dc);min-width:0}
.row:last-child{border-bottom:0}
.when{flex:0 0 118px;color:var(--cam-muted,#777);font-variant-numeric:tabular-nums;font-size:12px}
.kind{flex:0 0 150px;font-size:11px;letter-spacing:.03em;color:var(--cam-muted,#777)}
.what{flex:1;min-width:0;overflow-wrap:anywhere}
.muted{color:var(--cam-muted,#777)}
.warn{color:var(--cam-danger,#8d3030)}
.ok{color:var(--cam-ok,#41624c)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:8px 0 0}
dt{color:var(--cam-muted,#777)}
dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.relay{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px}
.relay b{font-weight:600}
.empty{padding:16px 0;color:var(--cam-muted,#777)}
code{font-size:11px;overflow-wrap:anywhere}
@media (max-width:600px){body{padding:12px}.row{flex-wrap:wrap}.when{flex-basis:auto}.kind{flex-basis:auto}.what{flex-basis:100%}}
</style></head><body data-view="__VIEW__">
<section id="timeline" hidden>
  <h1>动态</h1><p class="sub">确定性的事件视图：直接来自回执、Dream 提交、断点、快照和 Relay 记录。不是 AI 总结，不含记忆正文。</p>
  <div class="card" id="relay-panel"><div class="muted">Relay 状态读取中…</div></div>
  <div class="bar" id="ranges">
    <button data-range="today">今天</button><button data-range="7d" aria-pressed="true">7 天</button><button data-range="30d">30 天</button>
    <select id="kind" aria-label="事件类型"><option value="">全部类型</option><option value="memory">记忆</option><option value="dream">Dream</option><option value="handoff">断点</option><option value="snapshot">快照</option><option value="relay">Relay</option></select>
    <span class="muted" id="count"></span>
  </div>
  <div class="card" id="events"></div>
</section>
<section id="handoffs" hidden>
  <h1>断点</h1><p class="sub">短期接力胶囊，不是长期记忆：不进 recall、检索或 Dream，默认 48 小时过期。这里只读；由各自的 AI 用 handoff_close 关闭。</p>
  <div id="handoff-list"></div>
</section>
<section id="snapshots" hidden>
  <h1>快照</h1><p class="sub">恢复点，不是 git 提交。这里只能查看、校验和预览恢复计划；创建用 snapshot_create，恢复只能在命令行两步完成：<code>common-ai-memory snapshot restore-plan &lt;id&gt;</code> → <code>common-ai-memory snapshot restore &lt;id&gt; --confirm &lt;token&gt;</code>（先停服务）。</p>
  <div id="snapshot-list"></div>
</section>
<script>
const $ = (sel, root=document) => root.querySelector(sel);
function el(tag, attrs={}, ...children){
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === "class") node.className = v; else node.setAttribute(k, v); }
  for (const child of children) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  return node;
}
async function getJSON(path){
  const res = await fetch("/api/continuity/" + path, {headers: {"Accept": "application/json", "X-Memory-UI": "1"}});
  return res.json();
}
function when(iso){
  if (!iso) return "—";
  const d = new Date(iso); if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2, "0");
  return `${d.getMonth()+1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function bytes(n){ return n > 1048576 ? (n/1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n/1024)) + " KB"; }

let range = "7d";
async function loadEvents(){
  const box = $("#events"); box.replaceChildren(el("div", {class: "empty"}, "读取中…"));
  const kind = $("#kind").value;
  const data = await getJSON(`changes?range=${range}` + (kind ? `&kind=${kind}` : ""));
  box.replaceChildren();
  $("#count").textContent = data.ok ? `${data.total} 条` + (data.truncated ? "（显示最新 100 条）" : "") : "";
  if (!data.ok || !data.events.length) { box.append(el("div", {class: "empty"}, data.ok ? "这段时间没有事件。" : (data.error || "读取失败"))); return; }
  for (const e of data.events) box.append(el("div", {class: "row"},
    el("span", {class: "when"}, when(e.timestamp)), el("span", {class: "kind"}, e.kind), el("span", {class: "what"}, e.summary)));
}
async function loadRelay(){
  const panel = $("#relay-panel");
  let data; try { data = await getJSON("relay"); } catch (_) { data = {ok: false}; }
  panel.replaceChildren(el("div", {}, el("b", {}, "Agent Relay"), el("span", {class: "muted"}, " · 只读状态，单跳、只回复、默认关闭")));
  if (!data.ok) { panel.append(el("div", {class: "muted"}, "状态不可用")); return; }
  for (const o of data.owners) {
    const last = o.last_run ? `${when(o.last_run.started_at)} · ${o.last_run.outcome || "进行中"}` : "无";
    panel.append(el("div", {class: "relay"},
      el("span", {}, el("b", {}, o.owner), " ", o.enabled ? el("span", {class: "warn"}, "已开启") : el("span", {class: "muted"}, "关闭")),
      el("span", {}, `近 24 小时 ${o.runs_last_24h}/${o.limits.max_per_day} 次`),
      el("span", {}, `近 1 小时 ${o.runs_last_hour}/${o.limits.max_per_hour} 次`),
      el("span", {}, `上次：${last}`),
      el("span", {class: "muted"}, `冷却 ${o.limits.cooldown_seconds}s · 输入≤${o.limits.max_input_chars} 输出≤${o.limits.max_output_chars} 字`)));
  }
  panel.append(el("div", {class: "muted", style: "margin-top:6px;font-size:12px"},
    "开启方式：在 owner-config.json 给对应身份写 relay_enabled: true。注意：每次自动回复都会调用该身份的 CLI，消耗它的额度/配额；只有对方显式请求时才会触发。"));
}
async function loadHandoffs(){
  const box = $("#handoff-list"); const data = await getJSON("handoffs");
  box.replaceChildren();
  if (!data.ok || !data.handoffs.length) { box.append(el("div", {class: "card empty"}, "没有进行中的断点。")); return; }
  for (const h of data.handoffs) {
    const detail = el("div");
    const card = el("div", {class: "card"},
      el("div", {class: "row"}, el("span", {class: "when"}, when(h.updated_at)), el("span", {class: "kind"}, h.owner),
        el("span", {class: "what"}, el("b", {}, h.topic), el("div", {class: "muted"}, h.next_hint || ""))),
      el("div", {class: "bar"}, el("span", {class: "muted"}, `过期于 ${when(h.expires_at)}`)), detail);
    const btn = el("button", {}, "展开");
    btn.addEventListener("click", async () => {
      if (detail.childElementCount) { detail.replaceChildren(); btn.textContent = "展开"; return; }
      const full = (await getJSON("handoffs/" + encodeURIComponent(h.handoff_id))).handoff || {};
      detail.replaceChildren(el("dl", {}, el("dt", {}, "摘要"), el("dd", {}, full.summary || "—"),
        el("dt", {}, "下一步"), el("dd", {}, full.next_steps || "—"), el("dt", {}, "临时上下文"), el("dd", {}, full.temporary_context || "—")));
      btn.textContent = "收起";
    });
    card.children[1].prepend(btn);
    box.append(card);
  }
}
async function loadSnapshots(){
  const box = $("#snapshot-list"); const data = await getJSON("snapshots");
  box.replaceChildren();
  if (!data.ok || !data.snapshots.length) { box.append(el("div", {class: "card empty"}, "还没有快照。")); return; }
  for (const s of data.snapshots) {
    const out = el("div");
    const verify = el("button", {}, "校验"), plan = el("button", {}, "恢复计划（只读）");
    verify.addEventListener("click", async () => {
      out.replaceChildren(el("div", {class: "muted"}, "校验中…"));
      const r = await getJSON(`snapshots/${encodeURIComponent(s.snapshot_id)}/verify`);
      out.replaceChildren(r.ok ? el("div", {class: "ok"}, `完好：${r.files_checked} 个文件 SHA256 一致`)
        : el("div", {class: "warn"}, `有问题：${(r.problems || []).map(p => p.path + " " + p.problem).join("；") || r.error}`));
    });
    plan.addEventListener("click", async () => {
      out.replaceChildren(el("div", {class: "muted"}, "计算中…"));
      const r = await getJSON(`snapshots/${encodeURIComponent(s.snapshot_id)}/plan`);
      if (!r.ok) { out.replaceChildren(el("div", {class: "warn"}, r.error || "失败")); return; }
      out.replaceChildren(el("dl", {},
        el("dt", {}, "新增"), el("dd", {}, `${r.add.count}`), el("dt", {}, "覆盖"), el("dd", {}, `${r.overwrite.count}`),
        el("dt", {}, "删除"), el("dd", {}, `${r.remove.count}` + (r.remove.count ? "（快照之后新建的文件）" : "")),
        el("dt", {}, "不变"), el("dd", {}, `${r.unchanged}`),
        el("dt", {}, "重建索引"), el("dd", {}, r.derived_indexes_rebuilt.join(", ") || "—"),
        el("dt", {}, "plan digest"), el("dd", {}, el("code", {}, r.plan_digest))),
        el("div", {class: "muted", style: "margin-top:6px"}, "这里不会恢复任何东西。要恢复请停服务后在命令行执行上面的两步命令。"));
    });
    box.append(el("div", {class: "card"},
      el("div", {class: "row"}, el("span", {class: "when"}, when(s.created_at)), el("span", {class: "kind"}, s.reason || ""),
        el("span", {class: "what"}, el("b", {}, s.label || s.snapshot_id), el("div", {class: "muted"},
          s.status === "unreadable" ? "清单不可读" : `${s.file_count} 个文件 · ${bytes(s.total_bytes || 0)} · ${s.snapshot_id}`))),
      el("div", {class: "bar"}, verify, plan), out));
  }
}
const view = document.body.dataset.view;
$("#" + view).hidden = false;
if (view === "timeline") {
  document.querySelectorAll("[data-range]").forEach(b => b.addEventListener("click", () => {
    range = b.dataset.range;
    document.querySelectorAll("[data-range]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    loadEvents();
  }));
  $("#kind").addEventListener("change", loadEvents);
  loadRelay(); loadEvents();
} else if (view === "handoffs") loadHandoffs();
else loadSnapshots();
</script></body></html>"""


def page(view: str) -> str:
    return HTML.replace("__VIEW__", view if view in VIEWS else "timeline")
