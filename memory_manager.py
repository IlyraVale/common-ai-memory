"""Local memory manager: a localhost-only admin UI over the regular MemoryStore API.

Every change goes through MemoryStore.remember/update/forget, so Markdown stays
the only source of truth and FTS, vectors, receipts, locks and validation behave
exactly as for MCP callers. This module never opens memory files for writing.

Admin semantics: the operator must name the acting owner explicitly, and it must
be the memory's own owner and one of the configured admin owners. MCP owner rules
are untouched; this is a separate, local-only entry point.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import memory_store as _memory_store
from category_policy import CategoryPolicy
from memory_store import VALID_LIFECYCLE, VALID_SOURCE, VALID_STATUS, VALID_VERIFICATION, MemoryStore
from config import data_root, load_dotenv

DEFAULT_PORT = 8882
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PREVIEW_CHARS = 140
MAX_BODY_BYTES = 256 * 1024


class AdminError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _preview(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


class MemoryAdmin:
    def __init__(self, root: str | Path, admin_owners: tuple[str, ...] | None = None) -> None:
        self.root = Path(root).resolve()
        self.policy = CategoryPolicy(self.root)
        self._reader = MemoryStore(self.root, "memory-manager")
        self._scan_cache: tuple[Any, dict[str, Any]] | None = None
        # Without an explicit list, manage the owners that already have ordinary memories.
        self.admin_owners = tuple(admin_owners) if admin_owners else tuple(
            sorted({str(r.get("owner")) for r in self._records() if r.get("owner") not in (None, "human")})
        )

    # ---------- reads ----------

    def _records(self) -> list[dict[str, Any]]:
        return [r for r in self._reader._read_all() if not str(r.get("id") or "").startswith("human:")]

    def _record(self, memory_id: str) -> dict[str, Any]:
        found = self._reader._find_record(str(memory_id))
        if found is None:
            raise AdminError(404, "memory not found")
        return found[1]

    @staticmethod
    def _row(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"), "owner": record.get("owner"), "scope": record.get("scope"),
            "category": record.get("category"), "preview": _preview(record.get("content") or ""),
            "created_at": record.get("created_at"), "updated_at": record.get("updated_at"),
            "source": record.get("source"), "status": record.get("status"),
            "lifecycle": record.get("lifecycle") or "active",
            "verification": record.get("verification") or "unknown",
            "superseded_by": record.get("superseded_by"),
            "evidence_ref_count": len(record.get("evidence_refs") or []),
        }

    def list(self, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        filters = {k: v for k, v in (filters or {}).items() if v}
        query = filters.pop("q", "").casefold()
        rows = []
        for record in self._records():
            row = self._row(record)
            if any(str(row.get(key) or "") != value for key, value in filters.items()):
                continue
            if query and query not in " ".join(
                str(x or "") for x in (record.get("content"), record.get("id"), record.get("category"))
            ).casefold():
                continue
            rows.append(row)
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return rows

    def get(self, memory_id: str) -> dict[str, Any]:
        record = self._record(memory_id)
        detail = self._row(record)
        detail["content"] = record.get("content")
        detail["evidence_refs"] = list(record.get("evidence_refs") or [])
        detail["lifecycle_updated_at"] = record.get("lifecycle_updated_at")
        detail["verification_updated_at"] = record.get("verification_updated_at")
        detail["version"] = self._version(record)
        return detail

    @staticmethod
    def _version(record: dict[str, Any]) -> str:
        # The serialized Markdown covers content and every frontmatter field, so any
        # change by another window, an MCP caller or the nightly runner shows up here.
        return hashlib.sha256(MemoryStore._serialize(record).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _version_guard(store: MemoryStore):
        # A reentrant store lock lets the version check and the write share one critical
        # section. Stores without reentrant locks fall back to check-then-write.
        if getattr(_memory_store, "_HELD_WRITE_LOCKS", None) is not None:
            return store._write_lock()
        return contextlib.nullcontext()

    def _check_version(self, memory_id: str, expected_version: str | None) -> dict[str, Any]:
        record = self._record(memory_id)
        if expected_version is not None and self._version(record) != expected_version:
            raise AdminError(409, "this memory changed since it was loaded; reload it before saving")
        return record

    def options(self) -> dict[str, Any]:
        return {"owners": list(self.admin_owners), "categories": self.policy.allowed_categories(),
                "sources": list(VALID_SOURCE), "statuses": list(VALID_STATUS),
                "lifecycles": list(VALID_LIFECYCLE), "verifications": list(VALID_VERIFICATION)}

    # ---------- owner gate ----------

    def _store_for(self, record: dict[str, Any], acting_owner: str) -> MemoryStore:
        acting_owner = str(acting_owner or "").strip().lower()
        if acting_owner not in self.admin_owners:
            raise AdminError(403, "acting owner is not an admin-managed owner")
        if str(record.get("owner")) != acting_owner:
            raise AdminError(403, "acting owner must be the memory's owner")
        return MemoryStore(self.root, acting_owner)

    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PermissionError as exc:
            raise AdminError(403, str(exc)) from exc
        except KeyError as exc:
            raise AdminError(404, str(exc).strip("'\"")) from exc
        except ValueError as exc:
            raise AdminError(400, str(exc)) from exc

    # ---------- mutations (regular MemoryStore API only) ----------

    def update(self, memory_id: str, acting_owner: str, changes: dict[str, Any],
               expected_version: str | None = None) -> dict[str, Any]:
        store = self._store_for(self._record(memory_id), acting_owner)
        # Re-read and compare under the store's own (reentrant) write lock, so a save
        # can never silently overwrite a newer version written in between.
        with self._version_guard(store):
            record = self._check_version(memory_id, expected_version)
            result = self._apply_update(store, record, memory_id, changes)
        self._scan_cache = None
        detail = self.get(memory_id)
        detail["saved"] = True
        detail["receipt"] = "recorded" if result.get("execution_receipt_id") else "unavailable"
        return detail

    def _apply_update(self, store: MemoryStore, record: dict[str, Any], memory_id: str,
                      changes: dict[str, Any]) -> dict[str, Any]:
        content = changes.get("content", record.get("content"))
        if not isinstance(content, str) or not content.strip():
            raise AdminError(400, "content must not be empty")
        kwargs: dict[str, Any] = {}
        if "category" in changes and changes["category"] != record.get("category"):
            kwargs["category"] = self._call(self.policy.normalize, str(changes["category"]))
        for key in ("source", "status"):
            if key in changes:
                desired = changes[key] or None
                if desired != (record.get(key) or None):
                    kwargs[key] = desired if desired else "none"
        if "verification" in changes and changes["verification"] != (record.get("verification") or "unknown"):
            kwargs["verification"] = changes["verification"]
        lifecycle = changes.get("lifecycle", record.get("lifecycle") or "active")
        superseded_by = (changes.get("superseded_by") or None) if lifecycle == "superseded" else None
        if lifecycle != (record.get("lifecycle") or "active") or superseded_by != (record.get("superseded_by") or None):
            kwargs["lifecycle"] = lifecycle
            if superseded_by is not None:
                kwargs["superseded_by"] = superseded_by
        if "evidence_refs" in changes:
            refs = changes["evidence_refs"]
            if not isinstance(refs, list):
                raise AdminError(400, "evidence_refs must be a list")
            if [str(r).strip().lower() for r in refs] != list(record.get("evidence_refs") or []):
                kwargs["evidence_refs"] = refs
        return self._call(store.update, str(memory_id), content, **kwargs)

    def forget(self, memory_id: str, acting_owner: str, confirm_id: str,
               expected_version: str | None = None) -> dict[str, Any]:
        if str(confirm_id) != str(memory_id):
            raise AdminError(400, "deletion requires confirm_id equal to the memory id")
        store = self._store_for(self._record(memory_id), acting_owner)
        with self._version_guard(store):
            self._check_version(memory_id, expected_version)
            result = self._call(store.forget, str(memory_id))
        self._scan_cache = None
        # Same follow-up as the MCP forget tool.
        from dream_scraps import DreamScrapStore
        from memory_witness import MemoryWitnessStore

        DreamScrapStore(self.root).delete_by_source_ref(acting_owner, str(memory_id))
        MemoryWitnessStore(self.root).delete_evidence_ref(acting_owner, str(memory_id))
        return {"ok": True, "id": result.get("id"), "execution_receipt_id": result.get("execution_receipt_id")}

    def mark_review_needed(self, memory_id: str, acting_owner: str) -> dict[str, Any]:
        return self.update(memory_id, acting_owner, {"lifecycle": "review_needed"})

    def supersede(self, old_id: str, new_id: str, acting_owner: str) -> dict[str, Any]:
        return self.update(old_id, acting_owner, {"lifecycle": "superseded", "superseded_by": new_id})

    # ---------- merge ----------

    def merge_preview(self, memory_ids: list[str], base_id: str | None = None) -> dict[str, Any]:
        if len(memory_ids) < 2 or len(set(memory_ids)) != len(memory_ids):
            raise AdminError(400, "merge needs at least two distinct memories")
        records = [self._record(mid) for mid in memory_ids]
        owners = {str(r.get("owner")) for r in records}
        base = next((r for r in records if str(r.get("id")) == base_id), records[0])
        problems = []
        if len(owners) != 1:
            problems.append("memories belong to different owners; supersession requires one owner")
        if any((r.get("lifecycle") or "active") == "superseded" for r in records):
            problems.append("an already superseded memory cannot be merged again")
        draft = "\n\n".join(str(r.get("content") or "").strip() for r in records)
        return {
            "executable": not problems, "problems": problems,
            "owner": next(iter(owners)) if len(owners) == 1 else None,
            "draft": {"content": draft, "category": base.get("category"), "source": base.get("source"),
                      "status": base.get("status"), "verification": base.get("verification") or "unknown"},
            "will_supersede": [self._row(r) for r in records],
        }

    def merge_execute(self, memory_ids: list[str], acting_owner: str, draft: dict[str, Any],
                      confirm: bool) -> dict[str, Any]:
        if confirm is not True:
            raise AdminError(400, "merge requires explicit confirm=true")
        preview = self.merge_preview(memory_ids)
        if not preview["executable"]:
            raise AdminError(400, "; ".join(preview["problems"]))
        records = [self._record(mid) for mid in memory_ids]
        store = self._store_for(records[0], acting_owner)
        content = str(draft.get("content") or "").strip()
        if not content:
            raise AdminError(400, "merged content must not be empty")
        category = self._call(self.policy.normalize, str(draft.get("category") or records[0].get("category")))
        created = self._call(store.remember, content, category, visibility=str(records[0].get("scope") or "agent"),
                             status=draft.get("status") or None, source=draft.get("source") or None)
        new_id = str(created["id"])
        done: list[dict[str, Any]] = []
        try:
            verification = draft.get("verification")
            if verification and verification != "unknown":
                self._call(store.update, new_id, content, verification=verification)
            for record in records:
                self._call(store.update, str(record["id"]), str(record.get("content") or ""),
                           lifecycle="superseded", superseded_by=new_id)
                done.append(record)
        except Exception as exc:
            # Compensate through the same API so no half-applied supersession remains.
            leftovers = []
            for record in reversed(done):
                previous = record.get("lifecycle") or "active"
                try:
                    store.update(str(record["id"]), str(record.get("content") or ""),
                                 lifecycle=previous if previous != "superseded" else "active")
                except Exception:
                    leftovers.append(f"{record['id']} still superseded_by {new_id}")
            try:
                store.forget(new_id)
            except Exception:
                leftovers.append(f"merged memory {new_id} still exists")
            if leftovers:
                raise AdminError(500, "merge failed and rollback is incomplete: " + "; ".join(leftovers)) from exc
            if isinstance(exc, AdminError):
                raise AdminError(exc.status, f"merge rolled back: {exc}") from exc
            raise AdminError(500, f"merge rolled back: {type(exc).__name__}") from exc
        finally:
            self._scan_cache = None
        return {"ok": True, "merged_id": new_id, "superseded": [str(r["id"]) for r in records]}

    # ---------- scanner ----------

    def _memory_fingerprint(self) -> tuple[int, int, int]:
        # Cheap change detector (also catches writes by MCP callers or other windows).
        stats = [p.stat() for p in self._reader._iter_memory_paths()]
        return len(stats), max((s.st_mtime_ns for s in stats), default=0), sum(s.st_size for s in stats)

    def duplicates(self, owner: str | None = None, *, run: bool = True) -> dict[str, Any]:
        """Run the read-only scan, or return the cached result of the last run.

        The cache is derived and in-memory only; any mutation or file change voids it.
        """
        from memory_duplicates import scan

        key = (owner or None, self._memory_fingerprint())
        if not run:
            cached = self._scan_cache
            if cached is not None and cached[0] == key:
                return {**cached[1], "cached": True}
            return {"status": "not_scanned", "groups": [], "distribution": {}}
        report = scan(self.root, owner=owner or None, include_content=True)
        self._scan_cache = (key, report)
        return {**report, "cached": False}


# ---------- HTTP (shared by the standalone server and the unified memory UI) ----------

UI_HEADER = "X-Memory-UI"
TOKEN_HEADER = "X-Manager-Token"

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Manager</title>
<style>
:root{--bg:#f6f4ef;--fg:#222;--muted:#666;--line:#ddd;--card:#fff;--accent:#7a3b2e;--warn:#a33;--ok:#2f6b3a}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a19;--fg:#eee;--muted:#aaa;--line:#3a3836;--card:#262422;--accent:#e0a090;--warn:#f77;--ok:#8fd19e}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0 8px 0 0}.embedded header h1,.embedded .tab{display:none}
.notice{font-size:12px;color:var(--muted)}
button,select,input,textarea{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
button{cursor:pointer}button[disabled]{opacity:.5;cursor:default}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button.danger{color:var(--warn);border-color:var(--warn)}
.tab.on{border-color:var(--accent);color:var(--accent)}main{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:12px;padding:12px 16px}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.filters input[name=q]{flex:1 1 180px}
table{width:100%;border-collapse:collapse;background:var(--card)}td,th{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top;font-size:13px}
tr.row:hover,tr.row.sel{background:rgba(127,127,127,.1);cursor:pointer}
.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;align-self:start;position:sticky;top:8px}
.muted{color:var(--muted);font-size:12px}label{display:block;margin:8px 0 2px;color:var(--muted);font-size:12px}
textarea{width:100%}textarea.body{min-height:320px;resize:vertical}input.wide{width:100%}
.meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 12px}.meta select,.meta input{width:100%}
.group{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:10px}.group.resolved{opacity:.55}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:8px}.pair>div{border:1px solid var(--line);border-radius:6px;padding:8px;white-space:pre-wrap;word-break:break-word}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}#msg{margin-left:auto;color:var(--accent)}.ok{color:var(--ok)}.err{color:var(--warn)}
@media (max-width:860px){main{grid-template-columns:1fr}.panel{position:static}.pair,.meta{grid-template-columns:1fr}td:nth-child(3),th:nth-child(3),td:nth-child(6),th:nth-child(6){display:none}}
</style></head><body class="__BODY_CLASS__">
<header><h1>Memory Manager</h1><button class="tab" data-tab="list">记忆管理</button><button class="tab" data-tab="dups">重复检查</button>
<span class="notice">此页面的操作会修改长期记忆。</span><span id="msg"></span></header>
<main><section id="left"></section><section id="right" class="panel"><div class="muted">选择一条记忆查看与编辑。</div></section></main>
<script nonce="__NONCE__">
const API = "__API_BASE__", INITIAL = "__INITIAL_TAB__";
let TOKEN = null, OPT = null, LAST_SCAN = null;
const resolved = new Set();
const $ = (s, el=document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function session() {
  const res = await fetch(API + "/session", {headers: {"X-Memory-UI": "1"}, credentials: "same-origin"});
  if (!res.ok) throw new Error("session " + res.status); TOKEN = (await res.json()).token;
}
async function api(path, body) {
  if (!TOKEN) await session();
  const res = await fetch(API + path, {method: body ? "POST" : "GET", credentials: "same-origin",
    headers: {"X-Memory-UI": "1", "X-Manager-Token": TOKEN, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined});
  const data = await res.json().catch(() => ({})); if (!res.ok) { const e = new Error(data.error || res.status); e.status = res.status; throw e; } return data;
}
const say = (t, cls) => { const m = $("#msg"); m.textContent = t; m.className = cls || ""; setTimeout(() => { if (m.textContent === t) m.textContent = ""; }, 6000); };
const sel = (name, values, value, blank) => `<select name="${name}">${blank ? `<option value=""></option>` : ""}${values.map(v => `<option ${v === value ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
async function showList() {
  const f = Object.fromEntries([...document.querySelectorAll(".filters [name]")].map(e => [e.name, e.value]));
  const rows = await api("/memories?" + new URLSearchParams(f));
  $("#rows").innerHTML = rows.map(r => `<tr class="row" data-id="${esc(r.id)}"><td title="${esc(r.id)}">${esc(r.id.slice(0, 8))}</td><td>${esc(r.owner)}</td><td>${esc(r.category)}</td><td>${esc(r.preview)}</td><td>${esc(r.lifecycle)}<br><span class="muted">${esc(r.verification)} · ${esc(r.source || "")} · ${esc(r.status || "")} · ev ${r.evidence_ref_count}</span></td><td class="muted">${esc((r.updated_at || "").slice(0, 10))}</td></tr>`).join("");
  $("#count").textContent = rows.length + " 条";
  document.querySelectorAll("tr.row").forEach(tr => tr.onclick = () => showDetail(tr.dataset.id));
}
function listView() {
  $("#left").innerHTML = `<div class="filters"><input name="q" placeholder="搜索正文 / id / 分类">${sel("owner", OPT.owners, "", true)}${sel("category", OPT.categories, "", true)}${sel("lifecycle", OPT.lifecycles, "", true)}${sel("verification", OPT.verifications, "", true)}${sel("source", OPT.sources, "", true)}${sel("status", OPT.statuses, "", true)}<span id="count" class="muted"></span></div><table><thead><tr><th>id</th><th>owner</th><th>分类</th><th>预览</th><th>状态</th><th>更新</th></tr></thead><tbody id="rows"></tbody></table>`;
  document.querySelectorAll(".filters [name]").forEach(e => e.oninput = showList); showList();
}
async function showDetail(id, status) {
  const m = await api("/memory/" + encodeURIComponent(id));
  $("#right").innerHTML = `<div class="muted">${esc(m.id)} · owner <b>${esc(m.owner)}</b> · ${esc(m.scope)}<br>创建 ${esc(m.created_at)} · 更新 ${esc(m.updated_at)}</div>${status || ""}
  <label>正文</label><textarea class="body" name="content">${esc(m.content)}</textarea>
  <div class="meta"><div><label>分类</label>${sel("category", OPT.categories, m.category)}</div><div><label>source</label>${sel("source", OPT.sources, m.source || "", true)}</div>
  <div><label>status</label>${sel("status", OPT.statuses, m.status || "", true)}</div><div><label>lifecycle</label>${sel("lifecycle", OPT.lifecycles, m.lifecycle)}</div>
  <div><label>verification</label>${sel("verification", OPT.verifications, m.verification)}</div><div><label>superseded_by</label><input name="superseded_by" value="${esc(m.superseded_by || "")}"></div></div>
  <label>evidence_refs（每行一个 receipt:… / archive:…）</label><textarea name="evidence_refs" style="min-height:56px">${esc(m.evidence_refs.join("\n"))}</textarea>
  <label>以哪个 owner 身份操作（必须等于这条记忆的 owner）</label>${sel("acting_owner", OPT.owners, m.owner)}
  <div class="actions"><button class="primary" id="save">保存</button><button class="danger" id="del">删除…</button></div>`;
  $("#save").onclick = async () => {
    const v = n => $(`[name=${n}]`, $("#right")).value;
    try {
      const r = await api("/memory/" + encodeURIComponent(id) + "/update", {acting_owner: v("acting_owner"), expected_version: m.version, changes: {content: v("content"), category: v("category"), source: v("source"), status: v("status"), lifecycle: v("lifecycle"), superseded_by: v("superseded_by"), verification: v("verification"), evidence_refs: v("evidence_refs").split("\n").map(s => s.trim()).filter(Boolean)}});
      showDetail(id, `<div class="ok">已保存 · 更新于 ${esc(r.updated_at)} · receipt ${r.receipt === "recorded" ? "已记录" : "未记录（变更已保存）"}</div>`);
      if ($("#rows")) showList();
    } catch (e) {
      if (e.status === 409) say("这条记忆已在别处被修改，请刷新后再保存。", "err"); else say("保存失败：" + e.message, "err");
    }
  };
  $("#del").onclick = () => confirmDelete(m, $("[name=acting_owner]", $("#right")).value, () => { $("#right").innerHTML = ""; if ($("#rows")) showList(); });
}
async function confirmDelete(m, owner, after) {
  if (!confirm(`确认删除这条记忆？\n\nid: ${m.id}\nowner: ${m.owner}\n预览: ${(m.preview || m.content || "").slice(0, 120)}\n\n删除走 forget，不能撤销。`)) return false;
  try { await api("/memory/" + encodeURIComponent(m.id) + "/forget", {acting_owner: owner || m.owner, confirm_id: m.id, expected_version: m.version}); say("已删除", "ok"); after(); return true; }
  catch (e) { say(e.status === 409 ? "这条记忆已在别处被修改，请刷新后再删除。" : "删除失败：" + e.message, "err"); return false; }
}
function dupView() {
  $("#left").innerHTML = `<div class="filters">${sel("dup_owner", OPT.owners, "", true)}<button class="primary" id="scan">扫描</button><span id="scaninfo" class="muted"></span></div><div id="groups" class="muted"></div>`;
  $("#scan").onclick = () => renderDups(true); $("[name=dup_owner]").onchange = () => renderDups(false); renderDups(false);
}
async function renderDups(run) {
  const owner = $("[name=dup_owner]").value;
  if (run) { $("#groups").textContent = "扫描中…（只读）"; resolved.clear(); }
  const r = await api("/duplicates?" + new URLSearchParams({owner, run: run ? "1" : "0"}));
  if (r.status === "not_scanned") { $("#scaninfo").textContent = ""; $("#groups").textContent = "尚未扫描，或记忆已有变动。点击“扫描”运行只读检查。"; return; }
  LAST_SCAN = r;
  $("#scaninfo").textContent = `${r.memory_count} 条 · 语义 ${r.semantic.status} · ${r.timings_seconds.total}s${r.cached ? " · 上次结果" : ""} · ` + Object.entries(r.distribution).map(([k, v]) => `${k} ${v}`).join(" · ");
  $("#groups").innerHTML = r.groups.length ? r.groups.map(g => `<div class="group ${resolved.has(g.group_id) ? "resolved" : ""}" data-gid="${g.group_id}"><b>${esc(g.relation)}</b> · ${esc(g.confidence)} · cos ${g.signals.cosine ?? "-"} · jaccard ${g.signals.jaccard ?? "-"} · ${esc(g.owners.join("/"))}${g.cross_owner ? " · 跨 owner" : ""}${resolved.has(g.group_id) ? " · 已处理" : ""}<div class="muted">${esc(g.reason)}</div>
    <div class="pair">${g.members.map(m => `<div><div class="muted">${esc(m.id.slice(0, 8))} · ${esc(m.owner)} · ${esc(m.category)} · ${esc(m.lifecycle)} · ${esc(m.verification)}<br>创建 ${esc((m.created_at || "").slice(0, 10))} · 更新 ${esc((m.updated_at || "").slice(0, 10))}</div>${esc(m.content)}<div class="actions"><button data-act="edit" data-id="${esc(m.id)}">编辑</button><button class="danger" data-act="del" data-id="${esc(m.id)}">删除…</button></div></div>`).join("")}</div>
    <div class="actions"><button data-act="keep">保留，不处理</button><button data-act="later">稍后处理</button>${g.members.length === 2 && !g.cross_owner ? `<button data-act="supersede">较旧的一条标记为被较新的取代…</button><button data-act="merge">合并…</button>` : ""}${g.relation === "conflict" || g.relation === "uncertain" ? `<button data-act="review">标记需要确认</button>` : ""}</div></div>`).join("") : "没有候选。";
  document.querySelectorAll(".group").forEach(el => {
    const g = r.groups.find(x => x.group_id === el.dataset.gid);
    el.querySelectorAll("button").forEach(b => { b.disabled = resolved.has(g.group_id); b.onclick = () => groupAction(g, b.dataset.act, b.dataset.id); });
  });
}
async function groupAction(g, act, id) {
  const [a, b] = g.members; const owner = g.owners[0];
  const older = (a.created_at || "") <= (b.created_at || "") ? a : b, newer = older === a ? b : a;
  try {
    if (act === "keep" || act === "later") { resolved.add(g.group_id); document.querySelector(`[data-gid="${g.group_id}"]`).classList.add("resolved"); document.querySelectorAll(`[data-gid="${g.group_id}"] button`).forEach(x => x.disabled = true); return; }
    if (act === "edit") return showDetail(id);
    if (act === "del") { const m = await api("/memory/" + encodeURIComponent(id)); if (await confirmDelete(m, m.owner, () => {})) finish(g); return; }
    if (act === "supersede") { if (!confirm(`将 ${older.id.slice(0, 8)}（较旧）标记为 superseded_by ${newer.id.slice(0, 8)}（较新）？`)) return;
      await api("/memory/" + encodeURIComponent(older.id) + "/supersede", {acting_owner: owner, superseded_by: newer.id}); say("已标记", "ok"); return finish(g); }
    if (act === "review") { for (const m of g.members) await api("/memory/" + encodeURIComponent(m.id) + "/review_needed", {acting_owner: m.owner}); say("已标记 review_needed", "ok"); return finish(g); }
    if (act === "merge") return mergeEditor(g);
  } catch (e) { say("操作失败：" + e.message, "err"); }
}
function finish(g) {
  resolved.add(g.group_id);
  const el = document.querySelector(`[data-gid="${g.group_id}"]`);
  if (el) { el.classList.add("resolved"); el.querySelectorAll("button").forEach(x => x.disabled = true); el.querySelector("b").insertAdjacentText("afterend", " · 已处理（重新扫描后刷新）"); }
}
async function mergeEditor(g) {
  const ids = g.members.map(m => m.id); const p = await api("/merge/preview", {memory_ids: ids});
  $("#right").innerHTML = `<b>合并</b><div class="err">${p.problems.map(esc).join("<br>")}</div><label>新正文（自行编辑；不会自动改写）</label><textarea class="body" name="content">${esc(p.draft.content)}</textarea>
  <div class="meta"><div><label>分类</label>${sel("category", OPT.categories, p.draft.category)}</div><div><label>source</label>${sel("source", OPT.sources, p.draft.source || "", true)}</div><div><label>status</label>${sel("status", OPT.statuses, p.draft.status || "", true)}</div><div><label>verification</label>${sel("verification", OPT.verifications, p.draft.verification)}</div></div>
  <label>将创建一条新记忆，并把以下记忆标记为 superseded（由新记忆取代）：</label><ul>${p.will_supersede.map(m => `<li>${esc(m.id.slice(0, 8))} · ${esc(m.preview)}</li>`).join("")}</ul>
  <div class="actions"><button class="primary" id="domerge" ${p.executable ? "" : "disabled"}>确认合并（以 ${esc(p.owner || "?")} 身份）</button></div>`;
  $("#domerge").onclick = async () => { const v = n => $(`[name=${n}]`, $("#right")).value;
    if (!confirm("确认执行合并？")) return;
    try { const r = await api("/merge/execute", {memory_ids: ids, acting_owner: p.owner, confirm: true, draft: {content: v("content"), category: v("category"), source: v("source"), status: v("status"), verification: v("verification")}});
      say("已合并为 " + r.merged_id.slice(0, 8), "ok"); finish(g); showDetail(r.merged_id); } catch (e) { say("合并失败：" + e.message, "err"); } };
}
function open(tab) { document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x.dataset.tab === tab)); tab === "dups" ? dupView() : listView(); }
document.querySelectorAll(".tab").forEach(t => t.onclick = () => open(t.dataset.tab));
session().then(() => api("/options")).then(o => { OPT = o; open(INITIAL); }).catch(e => say("无法连接：" + e.message, "err"));
</script></body></html>"""


def render_page(api_base: str, *, initial_tab: str = "list", embedded: bool = False) -> tuple[bytes, str]:
    """The manager page and its CSP. The admin token is never part of the page."""
    nonce = secrets.token_urlsafe(16)
    page = (PAGE.replace("__NONCE__", nonce).replace("__API_BASE__", api_base)
            .replace("__INITIAL_TAB__", "dups" if initial_tab == "dups" else "list")
            .replace("__BODY_CLASS__", "embedded" if embedded else ""))
    ancestors = "'self'" if embedded else "'none'"
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; "
           f"base-uri 'none'; form-action 'none'; frame-ancestors {ancestors}")
    return page.encode("utf-8"), csp


def allowed_hosts_for(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def request_problem(headers: Any, port: int) -> tuple[int, str] | None:
    """Host (DNS rebinding) and Origin (cross-site) checks for every request."""
    hosts = allowed_hosts_for(port)
    if headers.get("Host", "") not in hosts:
        return 421, "unexpected host"
    origin = headers.get("Origin")
    if origin and origin not in {f"http://{h}" for h in hosts}:
        return 403, "cross-origin request refused"
    return None


class ManagerApi:
    """Routes for the admin API, mounted under a prefix by either server.

    Every API call needs the custom UI header (which a cross-site page cannot send
    without a CORS preflight that is never granted) and, except /session, the
    per-process token. The token is handed out by /session to same-origin script
    and kept only in that page's memory: never in a URL, storage, a log or HTML.
    """

    def __init__(self, admin: MemoryAdmin, token: str | None = None) -> None:
        self.admin = admin
        self.token = token or secrets.token_urlsafe(32)

    def handle(self, method: str, subpath: str, query: dict[str, str], headers: Any,
               read_body) -> tuple[int, Any]:
        if headers.get(UI_HEADER) != "1":
            return 403, {"error": "missing UI header"}
        if subpath == "session" and method == "GET":
            return 200, {"token": self.token}
        if not secrets.compare_digest(headers.get(TOKEN_HEADER, ""), self.token):
            return 403, {"error": "missing or invalid manager token"}
        admin = self.admin
        try:
            if method == "GET":
                if subpath == "options":
                    return 200, admin.options()
                if subpath == "memories":
                    return 200, admin.list(query)
                if subpath.startswith("memory/") and subpath.count("/") == 1:
                    return 200, admin.get(subpath.split("/", 1)[1])
                if subpath == "duplicates":
                    return 200, admin.duplicates(query.get("owner") or None, run=query.get("run", "1") != "0")
                return 404, {"error": "not found"}
            body = read_body()
            parts = subpath.split("/")
            if len(parts) == 3 and parts[0] == "memory":
                memory_id, action = parts[1], parts[2]
                owner = body.get("acting_owner", "")
                expected = body.get("expected_version")
                if action == "update":
                    return 200, admin.update(memory_id, owner, body.get("changes") or {}, expected)
                if action == "forget":
                    return 200, admin.forget(memory_id, owner, body.get("confirm_id", ""), expected)
                if action == "review_needed":
                    return 200, admin.mark_review_needed(memory_id, owner)
                if action == "supersede":
                    return 200, admin.supersede(memory_id, str(body.get("superseded_by") or ""), owner)
            if subpath == "merge/preview":
                return 200, admin.merge_preview(list(body.get("memory_ids") or []), body.get("base_id"))
            if subpath == "merge/execute":
                return 200, admin.merge_execute(list(body.get("memory_ids") or []), body.get("acting_owner", ""),
                                                body.get("draft") or {}, body.get("confirm") is True)
            return 404, {"error": "not found"}
        except AdminError as exc:
            return exc.status, {"error": str(exc)}


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_BODY_BYTES:
        raise AdminError(413, "request too large")
    if "application/json" not in (handler.headers.get("Content-Type") or ""):
        raise AdminError(415, "JSON body required")
    data = json.loads(handler.rfile.read(length) or b"{}")
    if not isinstance(data, dict):
        raise AdminError(400, "JSON object required")
    return data


SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def send(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str,
         extra: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
    send(handler, status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
         {"X-Frame-Options": "DENY"})


def dispatch_api(handler: BaseHTTPRequestHandler, api: ManagerApi, method: str, prefix: str) -> None:
    url = urlparse(handler.path)
    subpath = url.path[len(prefix):].strip("/")
    query = {k: v[0] for k, v in parse_qs(url.query).items()}

    def body() -> dict[str, Any]:
        return read_json_body(handler)

    try:
        status, payload = api.handle(method, subpath, query, handler.headers, body)
    except AdminError as exc:
        status, payload = exc.status, {"error": str(exc)}
    except (json.JSONDecodeError, UnicodeDecodeError):
        status, payload = 400, {"error": "invalid JSON"}
    send_json(handler, status, payload)


class _BodyReader:
    """rfile wrapper that counts what was read of the request body, so the rest can be drained."""

    DRAIN_LIMIT = 1 << 20

    def __init__(self, raw: Any, content_length: str | None) -> None:
        self.raw = raw
        try:
            self.remaining = max(int(content_length or 0), 0)
        except ValueError:
            self.remaining = 0

    def read(self, size: int = -1) -> bytes:
        size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.raw.read(size) if size else b""
        self.remaining -= len(data)
        return data

    def drain(self) -> bool:
        if self.remaining > self.DRAIN_LIMIT:
            return False
        try:
            while self.remaining > 0:
                if not self.read(min(self.remaining, 65536)):
                    return False
        except OSError:
            return False
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


def make_handler(api: ManagerApi, port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MemoryManager"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep content and paths out of logs
            return

        def _guarded(self) -> bool:
            problem = request_problem(self.headers, port)
            if problem:
                send_json(self, problem[0], {"error": problem[1]})
                return False
            return True

        def do_GET(self) -> None:
            if not self._guarded():
                return
            path = urlparse(self.path).path
            if path in ("/", "/duplicates"):
                page, csp = render_page("/api", initial_tab="dups" if path == "/duplicates" else "list")
                send(self, 200, page, "text/html; charset=utf-8",
                     {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})
                return
            if path.startswith("/api/"):
                dispatch_api(self, api, "GET", "/api")
                return
            send_json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            # A refused request's unread body must still be consumed, or closing the socket
            # resets the connection and can discard the reply.
            body = _BodyReader(self.rfile, self.headers.get("Content-Length"))
            self.rfile = body
            try:
                if not self._guarded():
                    return
                if urlparse(self.path).path.startswith("/api/"):
                    dispatch_api(self, api, "POST", "/api")
                    return
                send_json(self, 404, {"error": "not found"})
            finally:
                if not body.drain():
                    self.close_connection = True
                self.rfile = body.raw

    return Handler


def bind_server(host: str, port: int, handler_factory) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS:
        raise ValueError("the memory UI only binds to localhost")
    server = ThreadingHTTPServer((host, port), handler_factory(port))
    if port == 0:
        server.RequestHandlerClass = handler_factory(server.server_address[1])
    return server


def create_server(root: str | Path, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                  admin_owners: tuple[str, ...] | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Standalone manager (debugging/compatibility). The unified UI is memory_ui.py."""
    api = ManagerApi(MemoryAdmin(root, admin_owners))
    return bind_server(host, port, lambda p: make_handler(api, p)), api.token


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Standalone local memory manager (the unified UI is memory_ui.py).")
    parser.add_argument("--root", default=str(data_root()))
    parser.add_argument("--host", default="127.0.0.1", help="localhost only (127.0.0.1, localhost or ::1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--owners", default="",
                        help="comma-separated owners this local admin may manage (default: owners found in memory)")
    args = parser.parse_args(argv)
    owners = tuple(o.strip().lower() for o in args.owners.split(",") if o.strip()) or None
    server, _ = create_server(args.root, host=args.host, port=args.port, admin_owners=owners)
    print(f"Memory Manager (standalone): http://{args.host}:{server.server_address[1]}/  (localhost only; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
