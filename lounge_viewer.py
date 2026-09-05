from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from config import env_path, human_display_name, human_identity, load_dotenv, lounge_identities

from lounge_attachments import LoungeAttachmentError, LoungeAttachmentStore, MAX_ATTACHMENT_BYTES
from lounge_room import LoungeRoom

BRIDGE_URL = os.getenv("LOUNGE_BRIDGE_URL", "http://localhost:8879")
BRIDGE_MODES = {"manual", "active", "ai-chat"}

HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090a0d"><title>AI Lounge · 夜间客厅</title>
<style>
:root{color-scheme:dark;--page-bg:#08090c;--text:#f1ede6;--chat-text:#f1ede6;--muted:#8c8990;--line:#292a30;--online:#67d5a0;--global-font:Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif;--title-font:Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif;--chat-font:Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif;--nickname-font:Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif;--gpt-bubble:#375b87;--claude-bubble:#7e492a;--alice-bubble:#694689;--bubble-opacity:25%;--bubble-radius:16px;--bubble-border-width:1px;--bubble-border-opacity:25%;--bubble-border-color:#6b7280;--gpt-border-color:#375b87;--claude-border-color:#7e492a;--alice-border-color:#694689;--bubble-shadow-opacity:22%;--chat-width:680px;--bubble-width:74%;--font-size:13px;--line-height:1.72;--message-gap:22px;--topbar-bg:#090a0d;--topbar-opacity:88%;--topbar-height:76px;--topbar-text:#f1ede6;--topbar-title-size:19px;--topbar-title-weight:600;--topbar-title-spacing:1.14px;--topbar-title-shadow-opacity:18%;--input-bg:#15161b;--input-opacity:100%;--input-text:#f1ede6;--input-placeholder:#69666e;--input-border:#694689;--input-radius:18px;--input-shadow-x:0px;--input-shadow-y:10px;--input-shadow-blur:30px;--input-shadow-spread:0px;--input-shadow-opacity:26%;--input-shadow-effective-opacity:26%;--gpt-name:#79b8ff;--claude-name:#eaa36f;--alice-name:#c494f5;--gpt-name-size:13px;--claude-name-size:13px;--alice-name-size:13px;--gpt-name-weight:700;--claude-name-weight:700;--alice-name-weight:700;--gpt-name-spacing:1px;--claude-name-spacing:1px;--alice-name-spacing:1px}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;overflow:hidden;background:radial-gradient(ellipse at 50% -12%,rgba(118,84,55,.18),transparent 40%),radial-gradient(circle at 10% 70%,rgba(73,91,126,.08),transparent 34%),var(--page-bg);color:var(--text);font-family:var(--global-font)}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.15;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px);background-size:100% 4px}.app{height:100%;display:grid;grid-template-rows:auto minmax(0,1fr) auto}
.topbar{height:var(--topbar-height);border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--topbar-bg) var(--topbar-opacity),transparent);backdrop-filter:blur(20px);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 760px)/2));z-index:5;color:var(--topbar-text)}.brand{display:flex;align-items:center}h1{font-family:var(--title-font);font-size:var(--topbar-title-size);letter-spacing:var(--topbar-title-spacing);margin:0;font-weight:var(--topbar-title-weight);text-shadow:0 1px 12px color-mix(in srgb,var(--topbar-text) var(--topbar-title-shadow-opacity),transparent)}.tagline{font-size:10px;color:color-mix(in srgb,var(--topbar-text) 55%,transparent);margin-top:4px;letter-spacing:.18em;text-transform:uppercase}.status{display:flex;align-items:center;gap:14px}.presence{display:flex;align-items:center;gap:14px}.person{display:flex;align-items:center;gap:6px;font-size:12px;color:color-mix(in srgb,var(--topbar-text) 72%,transparent)}.dot{width:6px;height:6px;border-radius:50%;background:#48484f;box-shadow:0 0 0 3px rgba(255,255,255,.025)}.dot.on{background:var(--online);box-shadow:0 0 0 3px color-mix(in srgb,var(--online) 10%,transparent),0 0 8px color-mix(in srgb,var(--online) 45%,transparent)}
.bridge-mode{display:flex;align-items:center;gap:3px;padding:3px;border:1px solid var(--line);border-radius:10px;background:rgba(0,0,0,.18)}.mode-btn{border:0;border-radius:7px;padding:5px 7px;background:transparent;color:color-mix(in srgb,var(--topbar-text) 55%,transparent);font-size:10px;cursor:pointer;white-space:nowrap}.mode-btn:hover{color:var(--topbar-text)}.mode-btn.active{background:color-mix(in srgb,var(--alice-name) 20%,transparent);color:var(--topbar-text)}.mode-btn:disabled{opacity:.35;cursor:not-allowed}.bridge-state{font-size:9px;color:var(--muted);min-width:54px;text-align:center}.bridge-state.offline{color:#e58686}
.main{min-height:0;width:min(var(--chat-width),calc(100% - 28px));margin:0 auto}.messages{height:100%;overflow-y:auto;padding:30px 8px 34px;scroll-behavior:smooth;scrollbar-width:none;-ms-overflow-style:none}.messages::-webkit-scrollbar{display:none;width:0;height:0}.welcome{text-align:center;margin:5vh auto 36px;color:#6f6b71;font-size:12px;line-height:1.8}.welcome:before{content:"◆";display:block;color:#9c7654;margin-bottom:9px;font-size:8px}.msg{display:flex;margin:0 0 var(--message-gap);animation:arrive .24s ease-out}.msg.gpt{justify-content:flex-start}.msg.claude{justify-content:flex-end}.msg.alice{justify-content:center}@keyframes arrive{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}.stack{max-width:var(--bubble-width);display:flex;flex-direction:column}.claude .stack{align-items:flex-end}.alice .stack{align-items:center}.meta{font-size:10px;color:#747179;margin:0 8px 6px;display:flex;align-items:baseline;gap:7px}.who{font-family:var(--nickname-font)}.gpt .who{color:var(--gpt-name);font-size:var(--gpt-name-size);font-weight:var(--gpt-name-weight);letter-spacing:var(--gpt-name-spacing)}.claude .who{color:var(--claude-name);font-size:var(--claude-name-size);font-weight:var(--claude-name-weight);letter-spacing:var(--claude-name-spacing)}.alice .who{color:var(--alice-name);font-size:var(--alice-name-size);font-weight:var(--alice-name-weight);letter-spacing:var(--alice-name-spacing)}
.bubble{padding:11px 14px;border:var(--bubble-border-width) solid color-mix(in srgb,var(--bubble-border-color) var(--bubble-border-opacity),transparent);color:var(--chat-text);font-family:var(--chat-font);font-size:var(--font-size);line-height:var(--line-height);white-space:pre-wrap;overflow-wrap:anywhere;border-radius:var(--bubble-radius);box-shadow:0 8px 24px color-mix(in srgb,#000 var(--bubble-shadow-opacity),transparent)}.gpt .bubble{background:color-mix(in srgb,var(--gpt-bubble) var(--bubble-opacity),transparent);border-color:color-mix(in srgb,var(--gpt-border-color) var(--bubble-border-opacity),transparent);border-top-left-radius:calc(var(--bubble-radius) * .32)}.claude .bubble{background:color-mix(in srgb,var(--claude-bubble) var(--bubble-opacity),transparent);border-color:color-mix(in srgb,var(--claude-border-color) var(--bubble-border-opacity),transparent);border-top-right-radius:calc(var(--bubble-radius) * .32)}.alice .bubble{background:color-mix(in srgb,var(--alice-bubble) var(--bubble-opacity),transparent);border-color:color-mix(in srgb,var(--alice-border-color) var(--bubble-border-opacity),transparent)}
.composer-wrap{border-top:1px solid var(--line);background:color-mix(in srgb,var(--topbar-bg) var(--topbar-opacity),transparent);backdrop-filter:blur(20px);padding:14px 18px 18px;z-index:5}.composer{width:min(var(--chat-width),100%);margin:auto;display:flex;align-items:flex-end;gap:10px;padding:7px 8px 7px 15px;border:1px solid var(--input-border);border-radius:var(--input-radius);background:color-mix(in srgb,var(--input-bg) var(--input-opacity),transparent);box-shadow:var(--input-shadow-x) var(--input-shadow-y) var(--input-shadow-blur) var(--input-shadow-spread) color-mix(in srgb,#000 var(--input-shadow-effective-opacity),transparent)}textarea{flex:1;resize:none;border:0;outline:0;background:transparent;color:var(--input-text);font:var(--font-size)/1.55 inherit;min-height:24px;max-height:110px;padding:7px 0}textarea::placeholder{color:var(--input-placeholder)}button{font:inherit}.send{width:38px;height:38px;flex:0 0 38px;border:1px solid color-mix(in srgb,var(--alice-name) 25%,transparent);border-radius:12px;background:color-mix(in srgb,var(--alice-name) 13%,transparent);color:var(--alice-name);font-size:17px;cursor:pointer;transition:.18s}.send:hover{background:color-mix(in srgb,var(--alice-name) 22%,transparent);transform:translateY(-1px)}button:disabled{opacity:.4;cursor:default;transform:none}.hint{width:min(var(--chat-width),100%);margin:7px auto 0;color:#5d5a62;font-size:9px;text-align:right}.updated{color:color-mix(in srgb,var(--topbar-text) 48%,transparent)}
.gear{width:34px;height:34px;border:1px solid var(--line);border-radius:11px;background:rgba(255,255,255,.035);color:#aaa6ad;font-size:17px;cursor:pointer;display:grid;place-items:center;transition:.18s}.gear:hover,.gear[aria-expanded=true]{color:var(--text);background:rgba(255,255,255,.08);transform:rotate(18deg)}.scrim{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:19;opacity:0;pointer-events:none;transition:.22s}.scrim.open{opacity:1;pointer-events:auto}.drawer{position:fixed;z-index:20;right:0;top:0;height:100%;width:min(340px,92vw);background:#111217;border-left:1px solid var(--line);box-shadow:-18px 0 50px rgba(0,0,0,.35);transform:translateX(102%);transition:transform .24s ease;display:grid;grid-template-rows:auto minmax(0,1fr) auto}.drawer.open{transform:none}.drawer-head{height:68px;padding:0 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.drawer-head h2{font-size:15px;margin:0}.close{border:0;background:transparent;color:var(--muted);font-size:24px;cursor:pointer}.settings{overflow:auto;padding:12px 18px 24px}.group{padding:12px 0 15px;border-bottom:1px solid var(--line)}.group h3{font-size:10px;color:#88858c;text-transform:uppercase;letter-spacing:.15em;margin:0 0 11px}.control{display:grid;grid-template-columns:1fr auto;align-items:center;gap:10px;margin:9px 0;font-size:12px;color:#bbb7be}.control output{font-size:10px;color:#747179;min-width:38px;text-align:right}.control input[type=range]{grid-column:1/-1;width:100%;accent-color:var(--alice-name);height:3px}.control input[type=color]{width:38px;height:25px;border:1px solid var(--line);border-radius:7px;padding:2px;background:#191a20;cursor:pointer}.drawer-actions{padding:12px 18px 18px;border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}.action{border:1px solid var(--line);border-radius:9px;background:#191a20;color:#aaa6ad;padding:8px 4px;font-size:11px;cursor:pointer}.action:hover{color:var(--text);background:#202127}.file{display:none}.toast{position:fixed;z-index:30;right:18px;bottom:18px;padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:#191a20;color:#d7d2da;font-size:11px;opacity:0;transform:translateY(8px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:none}
.font-control{display:grid;gap:6px;margin:12px 0}.font-control>span{font-size:12px;color:#bbb7be}.font-select,.font-input,.choice-select{width:100%;border:1px solid var(--line);border-radius:8px;background:#191a20;color:#c8c4cb;padding:8px 9px;font:11px var(--global-font);outline:none}.font-select:focus,.font-input:focus,.choice-select:focus{border-color:color-mix(in srgb,var(--alice-name) 55%,var(--line))}.font-input::placeholder{color:#5f5c64}.toggle{width:36px;height:20px;accent-color:var(--alice-name);cursor:pointer}
.attachments{display:grid;grid-template-columns:repeat(2,minmax(0,180px));gap:7px;margin-top:8px}.attachment{padding:0;border:0;background:transparent;cursor:zoom-in;border-radius:12px;overflow:hidden;display:block}.attachment img{display:block;width:100%;max-height:220px;object-fit:cover;background:#0a0b0e}.attachment.animated:after{content:"动图";position:absolute;right:7px;bottom:7px;padding:2px 5px;border-radius:5px;background:rgba(0,0,0,.65);color:white;font-size:9px}.attachment{position:relative}.pending{width:min(var(--chat-width),100%);margin:0 auto 8px;display:flex;gap:8px;overflow-x:auto}.pending:empty{display:none}.pending-item{position:relative;flex:0 0 64px;height:64px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#111}.pending-item img{width:100%;height:100%;object-fit:cover}.pending-remove{position:absolute;right:2px;top:2px;width:20px;height:20px;border:0;border-radius:50%;background:rgba(0,0,0,.75);color:white;cursor:pointer}.attach-btn{width:38px;height:38px;flex:0 0 38px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.035);color:var(--input-text);font-size:18px;cursor:pointer}.upload-file{display:none}.dropzone{position:fixed;inset:14px;z-index:40;border:2px dashed var(--alice-name);border-radius:22px;background:color-mix(in srgb,var(--page-bg) 88%,transparent);display:grid;place-items:center;font-size:18px;opacity:0;pointer-events:none;transition:.15s}.dropzone.show{opacity:1}.lightbox{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.88);display:none;align-items:center;justify-content:center;padding:24px}.lightbox.open{display:flex}.lightbox img{max-width:100%;max-height:100%;object-fit:contain}.lightbox-close{position:absolute;right:18px;top:16px;border:0;background:transparent;color:white;font-size:32px;cursor:pointer}
@media(max-width:650px){.topbar{height:auto;min-height:68px;padding:8px 10px;gap:8px}.brand{display:none}.tagline,.updated,.presence{display:none}.status{width:100%;justify-content:space-between;gap:6px}.bridge-mode{flex:1;justify-content:center}.mode-btn{flex:1;padding:7px 4px;font-size:10px}.bridge-state{min-width:48px}.main{width:100%}.messages{padding:22px 14px}.stack{max-width:min(var(--bubble-width),94%)}.composer-wrap{padding:10px 10px 12px}.hint{padding-right:5px}.attachments{grid-template-columns:repeat(2,minmax(0,1fr))}.attachment img{max-height:44vw}.lightbox{padding:10px}}
</style></head><body><div class="app">
<header class="topbar"><div class="brand"><div><h1>AI Lounge</h1><div class="tagline">after hours · main room</div></div></div><div class="status"><div class="presence" aria-label="在线状态"><span class="person"><i class="dot" id="gptDot"></i>GPT</span><span class="person"><i class="dot" id="claudeDot"></i>Claude</span><span class="person"><i class="dot" id="aliceDot"></i>__HUMAN_DISPLAY_NAME__</span><span class="updated" id="updated"></span></div><div class="bridge-mode" id="bridgeMode" aria-label="Bridge 模式"><button class="mode-btn" type="button" data-mode="manual">Manual</button><button class="mode-btn" type="button" data-mode="active">Active</button><button class="mode-btn" type="button" data-mode="ai-chat">AI Chat</button></div><span class="bridge-state" id="bridgeState">读取中</span><button class="gear" id="gear" type="button" aria-label="外观设置" aria-expanded="false">⚙</button></div></header>
<main class="main"><div class="messages" id="messages"><div class="welcome">灯亮着，夜还长。<br>GPT、Claude、__HUMAN_DISPLAY_NAME__的共同客厅。</div></div></main>
<footer class="composer-wrap"><div class="pending" id="pending"></div><form class="composer" id="composer"><button class="attach-btn" id="attach" type="button" aria-label="添加图片">＋</button><input class="upload-file" id="upload" type="file" accept="image/png,image/jpeg,image/webp,image/gif,.apng" multiple><textarea id="input" rows="1" maxlength="1200" placeholder="__HUMAN_DISPLAY_NAME__，想说点什么……" aria-label="以__HUMAN_DISPLAY_NAME__的身份发言"></textarea><button class="send" id="send" type="submit" aria-label="发送">↑</button></form><div class="hint">Enter 发送 · Shift + Enter 换行 · 可粘贴或拖入图片</div></footer></div>
<div class="scrim" id="scrim"></div><aside class="drawer" id="drawer" aria-hidden="true"><div class="drawer-head"><h2>外观设置</h2><button class="close" id="close" type="button" aria-label="关闭">×</button></div><div class="settings" id="settings"></div><div class="drawer-actions"><button class="action" id="reset" type="button">恢复默认</button><button class="action" id="import" type="button">导入</button><button class="action" id="export" type="button">导出</button><input class="file" id="file" type="file" accept="application/json,.json"></div></aside><div class="toast" id="toast"></div><div class="dropzone" id="dropzone">松开即可添加图片</div><div class="lightbox" id="lightbox" role="dialog" aria-modal="true"><button class="lightbox-close" id="lightboxClose" aria-label="关闭原图">×</button><img id="lightboxImage" alt="附件原图"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));const humanId="__HUMAN_ID__";const names={gpt:"GPT",claude:"Claude",alice:"__HUMAN_DISPLAY_NAME__"};names[humanId]="__HUMAN_DISPLAY_NAME__";const box=document.getElementById("messages"),input=document.getElementById("input"),send=document.getElementById("send"),pendingBox=document.getElementById("pending");let lastSeq=-1,sending=false,pending=[];
function online(ts){return !!ts&&(Date.now()/1000-ts)<300}function readableTime(ts){const d=new Date(ts*1000),pad=n=>String(n).padStart(2,"0");return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`}function attachmentHtml(a){const id=encodeURIComponent(a.id),label=a.animated?'动态图片':'图片';return `<button class="attachment ${a.animated?'animated':''}" type="button" data-image="/attachments/${id}" aria-label="查看${label}原图"><img src="/attachments/${id}" alt="${label}" loading="lazy"></button>`}function render(rows){const seq=rows.length?rows[rows.length-1].seq:0;if(seq===lastSeq)return;lastSeq=seq;box.innerHTML=rows.length?rows.map(m=>`<div class="msg ${esc(m.author===humanId?'alice':m.author)}"><div class="stack"><div class="meta"><span class="who">${esc(names[m.author]||m.author)}</span><span>${readableTime(m.ts)}</span></div><div class="bubble">${esc(m.text)}${m.attachments?.length?`<div class="attachments">${m.attachments.map(attachmentHtml).join("")}</div>`:""}</div></div></div>`).join(""):'<div class="welcome">灯亮着，夜还长。<br>GPT、Claude、__HUMAN_DISPLAY_NAME__的共同客厅。</div>';requestAnimationFrame(()=>box.scrollTop=box.scrollHeight)}
const bridgeMode=document.getElementById("bridgeMode"),bridgeState=document.getElementById("bridgeState");function showBridge(data){const online=!!data?.ok;bridgeState.textContent=online?({manual:"Manual",active:"Active","ai-chat":"AI Chat"}[data.mode]||data.mode):"Bridge 离线";bridgeState.classList.toggle("offline",!online);for(const button of bridgeMode.querySelectorAll("button")){button.disabled=!online;button.classList.toggle("active",online&&button.dataset.mode===data.mode)}}async function refreshBridge(){try{const r=await fetch("/api/bridge/status",{cache:"no-store"}),data=await r.json();if(!r.ok)throw Error(data.error||"Bridge unavailable");showBridge(data)}catch(_){showBridge(null)}}async function refresh(){try{const s=await fetch("/api/state",{cache:"no-store"}).then(r=>r.json());render(s.messages||[]);for(const a of ["gpt","claude",humanId])document.getElementById((a===humanId?"alice":a)+"Dot").classList.toggle("on",online(s.readers?.[a]?.last_seen_at));document.getElementById("updated").textContent="· "+new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}catch(e){document.getElementById("updated").textContent="· 重连中"}refreshBridge()}
function renderPending(){pendingBox.innerHTML=pending.map((a,i)=>`<div class="pending-item"><img src="/attachments/${encodeURIComponent(a.id)}" alt="待发送图片"><button class="pending-remove" type="button" data-remove="${i}" aria-label="移除">×</button></div>`).join("")}async function addFiles(files){for(const file of files){if(pending.length>=4){alert("每条消息最多 4 张图片");break}try{const r=await fetch("/api/attachments",{method:"POST",headers:{"Content-Type":file.type||"application/octet-stream","X-File-Name":encodeURIComponent(file.name)},body:file}),data=await r.json();if(!r.ok)throw new Error(data.error||"上传失败");pending.push(data.attachment);renderPending()}catch(e){alert(e.message)}}}async function submit(){const text=input.value.trim();if((!text&&!pending.length)||sending)return;sending=true;send.disabled=true;try{const r=await fetch("/api/messages",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text,attachment_ids:pending.map(a=>a.id)})}),data=await r.json();if(!r.ok)throw new Error(data.error||"发送失败");input.value="";input.style.height="auto";pending=[];renderPending();lastSeq=-1;await refresh()}catch(e){alert(e.message)}finally{sending=false;send.disabled=false;input.focus()}}
document.getElementById("composer").addEventListener("submit",e=>{e.preventDefault();submit()});input.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();submit()}});input.addEventListener("input",()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,110)+"px"});
const upload=document.getElementById("upload"),dropzone=document.getElementById("dropzone"),lightbox=document.getElementById("lightbox"),lightboxImage=document.getElementById("lightboxImage");document.getElementById("attach").onclick=()=>upload.click();upload.onchange=()=>{addFiles(upload.files);upload.value=""};pendingBox.onclick=e=>{const button=e.target.closest("[data-remove]");if(button){pending.splice(Number(button.dataset.remove),1);renderPending()}};document.addEventListener("paste",e=>{const files=[...e.clipboardData.files].filter(f=>f.type.startsWith("image/"));if(files.length){e.preventDefault();addFiles(files)}});let dragDepth=0;document.addEventListener("dragenter",e=>{e.preventDefault();dragDepth++;dropzone.classList.add("show")});document.addEventListener("dragover",e=>e.preventDefault());document.addEventListener("dragleave",()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)dropzone.classList.remove("show")});document.addEventListener("drop",e=>{e.preventDefault();dragDepth=0;dropzone.classList.remove("show");addFiles([...e.dataTransfer.files].filter(f=>f.type.startsWith("image/")||/\.(png|apng|jpe?g|webp|gif)$/i.test(f.name)))});box.addEventListener("click",e=>{const button=e.target.closest("[data-image]");if(button){lightboxImage.src=button.dataset.image;lightbox.classList.add("open")}});function closeLightbox(){lightbox.classList.remove("open");lightboxImage.removeAttribute("src")}document.getElementById("lightboxClose").onclick=closeLightbox;lightbox.addEventListener("click",e=>{if(e.target===lightbox)closeLightbox()});
bridgeMode.addEventListener("click",async e=>{const button=e.target.closest("[data-mode]");if(!button||button.disabled)return;for(const item of bridgeMode.querySelectorAll("button"))item.disabled=true;bridgeState.textContent="切换中";try{const r=await fetch("/api/bridge/mode",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:button.dataset.mode})}),data=await r.json();if(!r.ok)throw Error(data.error||"模式切换失败");showBridge(data)}catch(err){showBridge(null);alert("Bridge 模式切换失败："+err.message)}});

const THEME_KEY="ai-lounge-theme-v1";const FONT_PRESETS=[
 ["现代 · 无衬线",'Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif'],["中文 · 微软雅黑",'"Microsoft YaHei","PingFang SC",sans-serif'],["中文 · 苹方",'"PingFang SC","Microsoft YaHei",sans-serif'],["中文 · 思源黑体",'"Source Han Sans SC","Noto Sans CJK SC",sans-serif'],["中文 · 鸿蒙黑体",'"HarmonyOS Sans SC","Microsoft YaHei",sans-serif'],["优雅 · 霞鹜文楷",'"LXGW WenKai","KaiTi","STKaiti",serif'],["优雅 · 方正宋刻",'"FZSongKeBenXiuKaiS-R-GB","STSong","SimSun",serif'],["手写 · 华文行楷",'"STXingkai","FZKai-Z03","KaiTi",cursive'],["手写 · 楷体",'"KaiTi","STKaiti","Kaiti SC",cursive'],["复古 · 宋体",'"STSong","SimSun","Songti SC",serif'],["复古 · 仿宋",'"FangSong","STFangsong","Fangsong SC",serif'],["几何 · Century Gothic",'"Century Gothic","Futura","Avenir Next",sans-serif'],["几何 · Montserrat",'Montserrat,"Avenir Next","Microsoft YaHei",sans-serif'],["未来 · Orbitron",'Orbitron,Eurostile,"Microsoft YaHei",sans-serif'],["未来 · Rajdhani",'Rajdhani,"Arial Narrow","Microsoft YaHei",sans-serif']
];const groups=[
 ["页面",[["pageBg","页面背景","color","#08090c","--page-bg"],["chatWidth","聊天区宽度","range",680,"--chat-width",[480,900,10,"px"]]]],
 ["字体",[["globalFont","全局字体","font",'Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif',"--global-font"],["titleFont","标题字体","font",'Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif',"--title-font"],["chatFont","聊天正文字体","font",'Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif',"--chat-font"],["nicknameFont","昵称字体","font",'Inter,"Microsoft YaHei","PingFang SC",system-ui,sans-serif',"--nickname-font"]]],
 ["气泡",[["gptBubble","GPT气泡","color","#375b87","--gpt-bubble"],["claudeBubble","Claude气泡","color","#7e492a","--claude-bubble"],["aliceBubble","__HUMAN_DISPLAY_NAME__气泡","color","#694689","--alice-bubble"],["bubbleOpacity","气泡透明度","range",25,"--bubble-opacity",[5,100,1,"%"]],["bubbleWidth","气泡宽度","range",74,"--bubble-width",[45,95,1,"%"]],["bubbleRadius","圆角","range",16,"--bubble-radius",[0,32,1,"px"]],["borderWidth","边框粗细","range",1,"--bubble-border-width",[0,4,1,"px"]],["borderOpacity","边框透明度","range",25,"--bubble-border-opacity",[0,100,1,"%"]],["bubbleBorderColor","全部边框颜色","color","#6b7280","--bubble-border-color"],["gptBorderColor","GPT边框颜色","color","#375b87","--gpt-border-color"],["claudeBorderColor","Claude边框颜色","color","#7e492a","--claude-border-color"],["aliceBorderColor","__HUMAN_DISPLAY_NAME__边框颜色","color","#694689","--alice-border-color"],["shadowOpacity","阴影强度","range",22,"--bubble-shadow-opacity",[0,60,1,"%"]]]],
 ["排版",[["chatText","聊天正文颜色","color","#f1ede6","--chat-text"],["fontSize","字号","range",13,"--font-size",[10,22,1,"px"]],["lineHeight","行高","range",1.72,"--line-height",[1.1,2.4,.05,""]],["messageGap","消息间距","range",22,"--message-gap",[6,48,1,"px"]]]],
 ["顶栏",[["topbarBg","顶栏颜色","color","#090a0d","--topbar-bg"],["topbarOpacity","顶栏透明度","range",88,"--topbar-opacity",[20,100,1,"%"]],["topbarHeight","顶栏高度","range",76,"--topbar-height",[56,110,1,"px"]],["topbarText","顶栏文字颜色","color","#f1ede6","--topbar-text"],["topbarTitleSize","标题字号","range",19,"--topbar-title-size",[14,30,1,"px"]],["topbarTitleWeight","标题字重","choice",600,"--topbar-title-weight",[500,600,700,800]],["topbarTitleSpacing","标题字距","range",1.14,"--topbar-title-spacing",[-1,8,.1,"px"]],["topbarTitleShadowOpacity","标题文字阴影","range",18,"--topbar-title-shadow-opacity",[0,40,1,"%"]]]],
 ["输入框",[["inputBg","背景颜色","color","#15161b","--input-bg"],["inputOpacity","背景透明度","range",100,"--input-opacity",[20,100,1,"%"]],["inputText","文字颜色","color","#f1ede6","--input-text"],["inputPlaceholder","占位文字颜色","color","#69666e","--input-placeholder"],["inputBorder","边框颜色","color","#694689","--input-border"],["inputRadius","圆角","range",18,"--input-radius",[0,32,1,"px"]],["inputShadowEnabled","阴影开关","toggle",true,"--input-shadow-enabled"],["inputShadowX","阴影 X","range",0,"--input-shadow-x",[-40,40,1,"px"]],["inputShadowY","阴影 Y","range",10,"--input-shadow-y",[-40,40,1,"px"]],["inputShadowBlur","阴影模糊","range",30,"--input-shadow-blur",[0,80,1,"px"]],["inputShadowSpread","阴影扩散","range",0,"--input-shadow-spread",[-20,40,1,"px"]],["inputShadowOpacity","阴影透明度","range",26,"--input-shadow-opacity",[0,80,1,"%"]]]],
 ["GPT · 昵称",[["gptName","颜色","color","#79b8ff","--gpt-name"],["gptNameSize","字号","range",13,"--gpt-name-size",[9,24,1,"px"]],["gptNameWeight","字重","range",700,"--gpt-name-weight",[100,900,100,""]],["gptNameSpacing","字间距","range",1,"--gpt-name-spacing",[-1,8,.25,"px"]]]],
 ["Claude · 昵称",[["claudeName","颜色","color","#eaa36f","--claude-name"],["claudeNameSize","字号","range",13,"--claude-name-size",[9,24,1,"px"]],["claudeNameWeight","字重","range",700,"--claude-name-weight",[100,900,100,""]],["claudeNameSpacing","字间距","range",1,"--claude-name-spacing",[-1,8,.25,"px"]]]],
 ["__HUMAN_DISPLAY_NAME__ · 昵称",[["aliceName","颜色","color","#c494f5","--alice-name"],["aliceNameSize","字号","range",13,"--alice-name-size",[9,24,1,"px"]],["aliceNameWeight","字重","range",700,"--alice-name-weight",[100,900,100,""]],["aliceNameSpacing","字间距","range",1,"--alice-name-spacing",[-1,8,.25,"px"]]]]
];const defs=groups.flatMap(g=>g[1]),defaults=Object.fromEntries(defs.map(d=>[d[0],d[3]]));let theme={...defaults};
function valid(t){const out={};if(!t||typeof t!=="object"||Array.isArray(t))throw Error("主题 JSON 格式无效");for(const d of defs){const v=t[d[0]];if(d[2]==="color"){if(v!==undefined&&!/^#[0-9a-f]{6}$/i.test(v))throw Error(`${d[1]}颜色无效`);out[d[0]]=v??d[3]}else if(d[2]==="font"){const f=String(v??d[3]).trim();if(!f||f.length>200||/[{};]/.test(f))throw Error(`${d[1]}格式无效`);out[d[0]]=f}else if(d[2]==="toggle"){out[d[0]]=v===undefined?d[3]:v===true}else if(d[2]==="choice"){const n=Number(v??d[3]);if(!d[5].includes(n))throw Error(`${d[1]}选项无效`);out[d[0]]=n}else{const n=Number(v??d[3]),r=d[5];if(!Number.isFinite(n)||n<r[0]||n>r[1])throw Error(`${d[1]}超出范围`);out[d[0]]=n}}return out}
function apply(t,save=true){theme=valid(t);for(const d of defs){const suffix=d[2]==="range"?d[5][3]:"";document.documentElement.style.setProperty(d[4],theme[d[0]]+suffix);const el=document.getElementById("theme-"+d[0]);if(el){if(d[2]==="toggle")el.checked=theme[d[0]];else el.value=theme[d[0]];const out=el.closest("label")?.querySelector("output");if(out)out.textContent=theme[d[0]]+suffix;if(d[2]==="font"){const sel=document.getElementById("preset-"+d[0]);sel.value=FONT_PRESETS.some(p=>p[1]===theme[d[0]])?theme[d[0]]:"custom"}}}document.documentElement.style.setProperty("--input-shadow-effective-opacity",(theme.inputShadowEnabled?theme.inputShadowOpacity:0)+"%");if(save)localStorage.setItem(THEME_KEY,JSON.stringify(theme))}
function updateThemeValue(d,value){if(d[0]==="bubbleBorderColor")apply({...theme,bubbleBorderColor:value,gptBorderColor:value,claudeBorderColor:value,aliceBorderColor:value});else apply({...theme,[d[0]]:value})}
function buildSettings(){const host=document.getElementById("settings");host.innerHTML=groups.map(([title,items])=>`<section class="group"><h3>${title}</h3>${items.map(d=>{if(d[2]==="color")return `<label class="control"><span>${d[1]}</span><input id="theme-${d[0]}" type="color" value="${d[3]}"></label>`;if(d[2]==="font")return `<label class="font-control"><span>${d[1]}</span><select class="font-select" id="preset-${d[0]}">${FONT_PRESETS.map(p=>`<option value="${esc(p[1])}">${p[0]}</option>`).join("")}<option value="custom">自定义…</option></select><input class="font-input" id="theme-${d[0]}" value="${esc(d[3])}" placeholder='例如："Microsoft YaHei", sans-serif'></label>`;if(d[2]==="toggle")return `<label class="control"><span>${d[1]}</span><input class="toggle" id="theme-${d[0]}" type="checkbox" ${d[3]?"checked":""}></label>`;if(d[2]==="choice")return `<label class="control"><span>${d[1]}</span><select class="choice-select" id="theme-${d[0]}">${d[5].map(v=>`<option value="${v}" ${v===d[3]?"selected":""}>${v}</option>`).join("")}</select></label>`;const r=d[5];return `<label class="control"><span>${d[1]}</span><output>${d[3]}${r[3]}</output><input id="theme-${d[0]}" type="range" min="${r[0]}" max="${r[1]}" step="${r[2]}" value="${d[3]}"></label>`}).join("")}</section>`).join("");for(const d of defs){const el=document.getElementById("theme-"+d[0]);el.addEventListener("input",e=>{try{updateThemeValue(d,d[2]==="range"||d[2]==="choice"?Number(e.target.value):d[2]==="toggle"?e.target.checked:e.target.value)}catch(_){}});if(d[2]==="font")document.getElementById("preset-"+d[0]).addEventListener("change",e=>{if(e.target.value==="custom"){el.focus();el.select()}else{el.value=e.target.value;updateThemeValue(d,e.target.value)}})}}
const drawer=document.getElementById("drawer"),gear=document.getElementById("gear"),scrim=document.getElementById("scrim");function toggle(open){drawer.classList.toggle("open",open);scrim.classList.toggle("open",open);drawer.setAttribute("aria-hidden",String(!open));gear.setAttribute("aria-expanded",String(open))}gear.onclick=()=>toggle(true);document.getElementById("close").onclick=()=>toggle(false);scrim.onclick=()=>toggle(false);document.addEventListener("keydown",e=>{if(e.key==="Escape")toggle(false)});
let toastTimer;function notice(s){const el=document.getElementById("toast");el.textContent=s;el.classList.add("show");clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove("show"),1600)}document.getElementById("reset").onclick=()=>{apply(defaults);notice("已恢复默认主题")};document.getElementById("export").onclick=()=>{const blob=new Blob([JSON.stringify({name:"AI Lounge Theme",version:1,theme},null,2)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="ai-lounge-theme.json";a.click();URL.revokeObjectURL(a.href);notice("主题已导出")};document.getElementById("import").onclick=()=>document.getElementById("file").click();document.getElementById("file").onchange=async e=>{try{const data=JSON.parse(await e.target.files[0].text());apply(data.theme??data);notice("主题已导入")}catch(err){alert("导入失败："+err.message)}finally{e.target.value=""}};
buildSettings();try{apply(JSON.parse(localStorage.getItem(THEME_KEY))||defaults,false)}catch(e){apply(defaults)}refresh();setInterval(refresh,1200);input.focus();
</script></body></html>'''


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    root: Path

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bridge(self, path: str, payload: bytes | None = None):
        request = Request(
            BRIDGE_URL + path,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"} if payload is not None else {},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8")), response.status
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"ok": False, "error": f"Bridge HTTP {exc.code}"}
            return detail, exc.code
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Bridge offline: {exc}"}, 503

    def do_GET(self):
        path = urlparse(self.path).path
        lounge = self.root / ".lounge"
        if path == "/":
            page = HTML.replace("__HUMAN_ID__", human_identity())
            data = page.replace("__HUMAN_DISPLAY_NAME__", human_display_name()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/state":
            rows = []
            messages = lounge / "messages.jsonl"
            if messages.exists():
                for line in messages.read_text(encoding="utf-8", errors="ignore").splitlines()[-120:]:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
            state = read_json(lounge / "state.json", {})
            readers = state.get("readers", {})
            for agent in lounge_identities():
                readers.setdefault(agent, {"last_read_seq": 0, "last_seen_at": None})
            self._json({"messages": rows, "readers": readers})
            return
        if path == "/api/bridge/status":
            result, status = self._bridge("/v1/status")
            self._json(result, status)
            return
        match = re.fullmatch(r"/attachments/([0-9a-f]{32})", path)
        if match:
            try:
                attachments = LoungeAttachmentStore(self.root)
                metadata = attachments.get_metadata(match.group(1))
                source = attachments.resolve(metadata)
                data = source.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", metadata["mime"])
                self.send_header("Content-Disposition", "inline")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except LoungeAttachmentError as exc:
                self._json({"ok": False, "error": str(exc)}, 404)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/bridge/mode":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1024:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                mode = payload.get("mode") if isinstance(payload, dict) else None
                if mode not in BRIDGE_MODES:
                    raise ValueError("mode must be manual, active, or ai-chat")
                result, status = self._bridge("/v1/mode", json.dumps({"mode": mode}).encode("utf-8"))
                self._json(result, status)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/attachments":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_ATTACHMENT_BYTES:
                    raise LoungeAttachmentError(f"single image limit is {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
                original_name = unquote(self.headers.get("X-File-Name", ""))
                claimed_mime = self.headers.get("Content-Type", "")
                attachment = LoungeAttachmentStore(self.root).save(
                    original_name, claimed_mime, self.rfile.read(length)
                )
                self._json({"ok": True, "attachment": attachment}, 201)
            except (ValueError, LoungeAttachmentError) as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            return
        if path != "/api/messages":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16_384:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = payload.get("text", "") if isinstance(payload, dict) else ""
            attachment_ids = payload.get("attachment_ids", []) if isinstance(payload, dict) else []
            if not isinstance(attachment_ids, list) or len(attachment_ids) > 4:
                raise LoungeAttachmentError("a message may contain at most 4 attachments")
            attachment_store = LoungeAttachmentStore(self.root)
            attachments = [attachment_store.get_metadata(str(value)) for value in attachment_ids]
            result = LoungeRoom(self.root, human_identity()).post(text=text, attachments=attachments)
            self._json(result, 200 if result.get("ok") else 400)
        except (ValueError, LoungeAttachmentError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        pass


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--port", type=int, default=int(os.getenv("LOUNGE_PORT", "8878")))
    args = parser.parse_args()
    Handler.root = Path(args.root).resolve()
    host = os.getenv("CAM_BIND_HOST", "localhost")
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"http://{host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
