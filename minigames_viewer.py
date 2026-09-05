from __future__ import annotations

import argparse
import copy
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from config import env_path, load_dotenv


GAME_NAMES = {
    "gomoku": "五子棋",
    "battleship": "海战棋",
    "blackjack": "21 点",
    "holdem": "德州扑克",
}

PREFIX_GAME = {
    "gmk-": "gomoku",
    "sea-": "battleship",
    "bj-": "blackjack",
    "he-": "holdem",
}


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Game Hall</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0b0d10;
  --panel:#11151a;
  --panel2:#151a20;
  --line:#252c34;
  --line2:#343d47;
  --text:#edf1f5;
  --muted:#8994a1;
  --soft:#b9c1ca;
  --gpt:#86aefb;
  --claude:#e5a66f;
  --ok:#7dc9a5;
  --warn:#e0b36a;
  --danger:#df7f7f;
  --wood:#c99652;
  --wood2:#b57d38;
  --sea:#122331;
  --sea2:#17364b;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  background:
    radial-gradient(circle at 20% -20%,rgba(86,104,128,.16),transparent 34%),
    radial-gradient(circle at 90% 0%,rgba(104,76,52,.10),transparent 28%),
    var(--bg);
  color:var(--text);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
}
button,select{font:inherit}
.shell{width:min(1180px,calc(100% - 32px));margin:30px auto 48px}
.masthead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:18px}
.brand h1{margin:0;font-size:26px;letter-spacing:.2px}
.brand p{margin:6px 0 0;color:var(--muted);font-size:13px}
.live{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px}
.live i{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(125,201,165,.08)}
.toolbar{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:12px;border:1px solid var(--line);border-radius:14px;
  background:rgba(17,21,26,.86);backdrop-filter:blur(10px);margin-bottom:14px;
}
.select-wrap{position:relative}
select{
  appearance:none;min-height:40px;padding:0 38px 0 13px;border-radius:10px;
  border:1px solid var(--line2);background:#161b21;color:var(--text);outline:none;
}
select:focus{border-color:#657383;box-shadow:0 0 0 3px rgba(101,115,131,.14)}
.select-wrap:after{content:"⌄";position:absolute;right:12px;top:8px;color:var(--muted);pointer-events:none}
.toolbar .spacer{flex:1}
.tag{
  display:inline-flex;align-items:center;gap:6px;min-height:30px;padding:0 9px;
  border:1px solid var(--line2);border-radius:999px;background:#151a20;
  color:var(--soft);font-size:12px;white-space:nowrap
}
.tag.playing{color:var(--ok);border-color:rgba(125,201,165,.32);background:rgba(125,201,165,.07)}
.tag.setup{color:var(--warn);border-color:rgba(224,179,106,.32);background:rgba(224,179,106,.07)}
.tag.finished{color:#c5b5f0;border-color:rgba(197,181,240,.28);background:rgba(197,181,240,.07)}

.layout{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:14px}
.panel{border:1px solid var(--line);border-radius:16px;background:rgba(17,21,26,.92)}
.arena{padding:18px;min-width:0}
.side{display:flex;flex-direction:column;gap:14px}
.side .panel{padding:15px}
.arena-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:16px}
.game-kicker{font-size:11px;color:var(--muted);letter-spacing:.11em;text-transform:uppercase}
.game-title{font-size:22px;font-weight:760;margin-top:3px}
.match-id{font-family:Consolas,monospace;color:var(--muted);font-size:12px;margin-top:4px}
.turnbox{text-align:right;min-width:130px}
.turnbox .label{color:var(--muted);font-size:11px;margin-bottom:4px}
.turnbox .who{font-size:15px;font-weight:740}
.gpt{color:var(--gpt)}.claude{color:var(--claude)}
.result{
  display:none;padding:11px 13px;margin-bottom:14px;border-radius:11px;
  border:1px solid rgba(224,179,106,.28);background:rgba(224,179,106,.07);
  color:#eed19f;font-weight:700
}
.result.show{display:block}
.section-title{font-size:12px;font-weight:700;color:var(--soft);margin:0 0 10px}
.muted{color:var(--muted)}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat{padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#0e1216}
.stat span{display:block;color:var(--muted);font-size:10px;margin-bottom:3px}
.stat b{font-size:13px;font-weight:700}
.player-line{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid #20262d}
.player-line:last-child{border-bottom:0}
.player-name{display:flex;align-items:center;gap:8px;font-weight:720}
.dot{width:7px;height:7px;border-radius:50%}.dot.gpt{background:var(--gpt)}.dot.claude{background:var(--claude)}

.chat{max-height:330px;overflow:auto;padding-right:2px}
.msg{display:flex;flex-direction:column;margin:8px 0}
.msg.gpt{align-items:flex-start}.msg.claude{align-items:flex-end}
.msg .speaker{font-size:10px;color:var(--muted);margin:0 4px 4px}
.bubble{max-width:92%;padding:8px 10px;border-radius:11px;font-size:12px;line-height:1.55;border:1px solid var(--line)}
.msg.gpt .bubble{background:rgba(134,174,251,.08);border-color:rgba(134,174,251,.18)}
.msg.claude .bubble{background:rgba(229,166,111,.08);border-color:rgba(229,166,111,.18)}
.empty{color:var(--muted);font-size:12px;padding:18px 4px}
.last-action{font-size:12px;line-height:1.6;color:var(--soft)}

.go-stage{display:flex;justify-content:center;align-items:center;min-height:620px}
.go-shell{width:min(620px,100%);padding:18px;border:1px solid rgba(67,44,22,.7);border-radius:16px;background:linear-gradient(145deg,var(--wood),#d3a761)}
.go-grid{display:grid;grid-template-columns:24px repeat(15,1fr);grid-template-rows:24px repeat(15,1fr);width:100%;aspect-ratio:1}
.go-label{display:flex;align-items:center;justify-content:center;color:#5f3d19;font:600 11px Consolas,monospace}
.go-cell{position:relative;aspect-ratio:1}
.go-cell:before,.go-cell:after{content:"";position:absolute;background:rgba(54,34,15,.70);z-index:0}
.go-cell:before{left:0;right:0;top:50%;height:1px}
.go-cell:after{top:0;bottom:0;left:50%;width:1px}
.go-cell.edge-left:before{left:50%}.go-cell.edge-right:before{right:50%}
.go-cell.edge-top:after{top:50%}.go-cell.edge-bottom:after{bottom:50%}
.star{position:absolute;left:50%;top:50%;width:5px;height:5px;margin:-2px;border-radius:50%;background:#5b3917;z-index:1}
.stone{position:absolute;left:11%;top:11%;width:78%;height:78%;border-radius:50%;z-index:3;box-shadow:0 3px 6px rgba(0,0,0,.28)}
.stone.black{background:radial-gradient(circle at 34% 28%,#444,#171717 48%,#050505 100%)}
.stone.white{background:radial-gradient(circle at 34% 28%,#fff,#ece8df 56%,#c8c2b7 100%);border:1px solid rgba(45,38,30,.34)}
.stone.last:after{content:"";position:absolute;left:50%;top:50%;width:7px;height:7px;margin:-3.5px;border-radius:50%;background:#c84f4f}
.go-cell.win .stone{box-shadow:0 0 0 3px rgba(205,91,91,.78),0 3px 8px rgba(0,0,0,.32)}

.sea-wrap{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.sea-card{padding:13px;border-radius:14px;border:1px solid #25475e;background:linear-gradient(180deg,#101d27,#0d1820)}
.sea-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px}
.sea-name{font-weight:730}.ready{font-size:11px;color:var(--muted)}
.ready.yes{color:var(--ok)}
.sea-grid{display:grid;grid-template-columns:22px repeat(10,1fr);grid-template-rows:22px repeat(10,1fr);gap:2px}
.sea-label{display:flex;align-items:center;justify-content:center;font:600 10px Consolas,monospace;color:#7ba3bd}
.sea-cell{position:relative;aspect-ratio:1;border:1px solid rgba(69,129,164,.22);border-radius:4px;background:rgba(31,84,115,.18)}
.sea-cell.miss:after{content:"";position:absolute;left:50%;top:50%;width:6px;height:6px;margin:-3px;border-radius:50%;background:#8aa6b5}
.sea-cell.hit{background:rgba(180,72,72,.20);border-color:rgba(220,104,104,.45)}
.sea-cell.hit:after{content:"×";position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#f19a92;font-size:19px;font-weight:800}
.sea-cell.sunk{background:rgba(151,52,52,.34);border-color:#bf6262}
.fleet-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.ship-chip{font-size:10px;color:#86a9be;border:1px solid rgba(83,137,168,.25);padding:4px 7px;border-radius:999px}
.ship-chip.ready{color:var(--ok);border-color:rgba(125,201,165,.25)}
.phase-note{margin-top:14px;padding:11px 12px;border-radius:11px;border:1px dashed var(--line2);color:var(--muted);font-size:12px}

.blackjack-stage{padding:8px 0 4px}
.dealer-zone{display:flex;flex-direction:column;align-items:center;padding:18px;margin-bottom:14px;border-radius:14px;background:linear-gradient(180deg,#16241e,#111a16);border:1px solid #294638}
.players-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.bj-player{padding:14px;border-radius:14px;border:1px solid var(--line);background:#0e1216}
.bj-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.cards{display:flex;gap:7px;flex-wrap:wrap;min-height:66px}
.play-card{width:48px;height:66px;border-radius:8px;background:#f1eee8;color:#171717;padding:7px;box-shadow:0 4px 14px rgba(0,0,0,.24);font:700 17px Georgia,serif;display:flex;flex-direction:column;justify-content:space-between}
.play-card.red{color:#b84848}.play-card.back{background:repeating-linear-gradient(45deg,#293b58,#293b58 6px,#354c70 6px,#354c70 12px);border:1px solid #6b7f9d;color:#dfe8f5}
.card-suit{align-self:flex-end;font-size:17px}
.score{font-size:11px;color:var(--muted)}.score b{color:var(--text);font-size:14px}

.poker-wrap{padding:4px 0}
.poker-table{position:relative;min-height:470px;border-radius:220px;background:radial-gradient(circle at 50% 45%,#214d3c,#173a2d 70%);border:8px solid #2a2b2d;box-shadow:inset 0 0 0 2px rgba(255,255,255,.05),0 18px 36px rgba(0,0,0,.25);padding:26px}
.seat{position:absolute;width:190px;padding:11px;border:1px solid rgba(255,255,255,.11);border-radius:13px;background:rgba(11,14,16,.86);backdrop-filter:blur(8px)}
.seat.top{top:18px;left:50%;transform:translateX(-50%)}.seat.bottom{bottom:18px;left:50%;transform:translateX(-50%)}
.seat.active{box-shadow:0 0 0 2px rgba(125,201,165,.38)}
.seat-head{display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:8px}
.stack{color:var(--muted);font-size:11px}
.community{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;gap:7px}
.pot{position:absolute;left:50%;top:61%;transform:translateX(-50%);font-size:12px;color:#d9e4dc}
.poker-meta{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:12px}
.poker-meta .tag{background:#10151a}

.debug{margin-top:10px}
.debug summary{cursor:pointer;color:#68737e;font-size:10px;user-select:none}
.debug pre{white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;padding:9px;border-radius:9px;background:#0a0d10;color:#75808b;font:10px/1.45 Consolas,monospace}

@media(max-width:900px){
  .layout{grid-template-columns:1fr}
  .side{display:grid;grid-template-columns:1fr 1fr}
  .go-stage{min-height:auto}
}
@media(max-width:650px){
  .shell{width:min(100% - 18px,1180px);margin-top:16px}
  .masthead{align-items:flex-start}.brand h1{font-size:22px}
  .layout{gap:10px}.arena{padding:12px}.side{grid-template-columns:1fr}
  .sea-wrap,.players-row{grid-template-columns:1fr}
  .go-shell{padding:10px}.go-grid{grid-template-columns:18px repeat(15,1fr);grid-template-rows:18px repeat(15,1fr)}
  .go-label{font-size:8px}
  .poker-table{min-height:430px}.seat{width:160px}.play-card{width:42px;height:58px;font-size:15px}
}
</style>
</head>
<body>
<div class="shell">
  <header class="masthead">
    <div class="brand">
      <h1>AI Game Hall</h1>
      <p>GPT × Claude · 本地实时观战大厅</p>
    </div>
    <div class="live"><i></i><span id="updated">等待刷新</span></div>
  </header>

  <section class="toolbar">
    <div class="select-wrap">
      <select id="gameFilter">
        <option value="">全部游戏</option>
        <option value="gomoku">五子棋</option>
        <option value="battleship">海战棋</option>
        <option value="blackjack">21 点</option>
        <option value="holdem">德州扑克</option>
      </select>
    </div>
    <div class="select-wrap"><select id="matches"></select></div>
    <div class="spacer"></div>
    <span class="tag" id="statusTag">等待对局</span>
  </section>

  <main class="layout">
    <section class="panel arena">
      <div class="arena-head">
        <div>
          <div class="game-kicker">Live Match</div>
          <div class="game-title" id="gameTitle">等待对局</div>
          <div class="match-id" id="matchId">—</div>
        </div>
        <div class="turnbox">
          <div class="label">当前行动</div>
          <div class="who" id="turnWho">—</div>
        </div>
      </div>
      <div class="result" id="result"></div>
      <div id="arenaStage"><div class="empty">正在读取对局…</div></div>
    </section>

    <aside class="side">
      <section class="panel">
        <div class="section-title">对局信息</div>
        <div id="matchInfo"></div>
      </section>
      <section class="panel">
        <div class="section-title">最近一步</div>
        <div class="last-action" id="lastAction">暂无动作。</div>
      </section>
      <section class="panel">
        <div class="section-title">桌边对话</div>
        <div class="chat" id="chat"><div class="empty">还没人说话。</div></div>
      </section>
    </aside>
  </main>
</div>

<script>
let current="", rows=[];
const GAME_NAMES={gomoku:"五子棋",battleship:"海战棋",blackjack:"21 点",holdem:"德州扑克"};
const STATUS_NAMES={playing:"进行中",setup:"部署中",finished:"已结束",waiting:"等待中",ready:"已就绪"};
const PHASE_NAMES={"fleet-setup":"舰队部署",preflop:"翻牌前",flop:"翻牌",turn:"转牌",river:"河牌",showdown:"摊牌"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const agentClass=a=>String(a||"").toLowerCase()==="gpt"?"gpt":String(a||"").toLowerCase()==="claude"?"claude":"";
const titleAgent=a=>String(a||"").toLowerCase()==="gpt"?"GPT":String(a||"").toLowerCase()==="claude"?"Claude":String(a||"?");

function gameOf(s){return s.game||rows.find(x=>x.id===s.id)?.game||""}
function statusName(v){return STATUS_NAMES[v]||v||"未知"}
function phaseName(v){return PHASE_NAMES[v]||v||""}
function reasonName(v){
  const x=String(v||"");
  if(x==="five-in-a-row")return "五子连珠";
  if(x==="draw")return "和局";
  if(x.endsWith(" resigned"))return titleAgent(x.split(" ")[0])+" 认输";
  return x;
}
function agentsOf(s){
  const p=s.players;
  if(Array.isArray(p))return p.map(x=>typeof x==="string"?x:x?.agent||x?.name).filter(Boolean);
  if(p&&typeof p==="object"){
    if(typeof p.black==="string"||typeof p.white==="string")return [p.black,p.white].filter(Boolean);
    if(typeof p.first==="string"||typeof p.second==="string")return [p.first,p.second].filter(Boolean);
    const keys=Object.keys(p).filter(k=>p[k]&&typeof p[k]==="object"&&!["dealer"].includes(k));
    if(keys.length)return keys;
    const vals=Object.values(p).filter(x=>typeof x==="string");
    if(vals.length)return vals;
  }
  for(const key of ["stacks","hole","hands"]){
    if(s[key]&&typeof s[key]==="object"&&!Array.isArray(s[key])){
      const ks=Object.keys(s[key]).filter(k=>!["dealer"].includes(k));
      if(ks.length)return ks;
    }
  }
  return ["gpt","claude"];
}
function turnAgent(s){
  if(s.turn_agent)return s.turn_agent;
  if(typeof s.turn==="string"){
    if(["gpt","claude"].includes(s.turn.toLowerCase()))return s.turn;
    if(s.players&&typeof s.players==="object"&&typeof s.players[s.turn]==="string")return s.players[s.turn];
  }
  if(s.current_player)return s.current_player;
  if(s.actor)return s.actor;
  return null;
}
function winnerText(s){
  if(s.status!=="finished")return "";
  if(s.winner)return `🏆 ${titleAgent(s.winner)} 胜 · ${reasonName(s.reason)}`;
  if(s.reason)return `🏁 ${reasonName(s.reason)}`;
  return "🏁 对局结束";
}
function playerLines(s){
  return agentsOf(s).map(a=>`<div class="player-line">
    <div class="player-name ${agentClass(a)}"><i class="dot ${agentClass(a)}"></i>${esc(titleAgent(a))}</div>
    <span class="muted">${turnAgent(s)===a?"行动中":""}</span>
  </div>`).join("");
}
function matchInfoHtml(s){
  const phase=s.phase||s.street;
  const moveCount=s.move_count??(Array.isArray(s.moves)?s.moves.length:null);
  return `${playerLines(s)}
    <div class="stat-grid" style="margin-top:10px">
      <div class="stat"><span>状态</span><b>${esc(statusName(s.status))}</b></div>
      <div class="stat"><span>阶段</span><b>${esc(phaseName(phase)||"—")}</b></div>
      <div class="stat"><span>手数 / 动作</span><b>${esc(moveCount??"—")}</b></div>
      <div class="stat"><span>胜者</span><b>${esc(s.winner?titleAgent(s.winner):"—")}</b></div>
    </div>`;
}
function chatHtml(s){
  const chat=Array.isArray(s.chat)?s.chat:[];
  if(!chat.length)return '<div class="empty">还没人说话。</div>';
  return chat.slice(-30).map(m=>{
    const a=String(m.agent||m.player||"?").toLowerCase();
    return `<div class="msg ${agentClass(a)}"><div class="speaker">${esc(titleAgent(a))}</div><div class="bubble">${esc(m.message||m.text||"")}</div></div>`;
  }).join("");
}
function lastActionText(s){
  const g=gameOf(s);
  const last=(Array.isArray(s.moves)&&s.moves.at(-1))||s.last_move||s.last_shot||s.last_action;
  if(!last)return s.status==="setup"?"正在准备开局。":"暂无动作。";
  if(typeof last==="string")return esc(last);
  if(g==="gomoku")return `${esc(titleAgent(last.agent))} 落子 ${esc(last.coord||"")}`;
  if(g==="battleship")return `${esc(titleAgent(last.agent||last.shooter||last.by))} 炮击 ${esc(last.coord||last.target||"")} ${last.result?`· ${esc(last.result)}`:""}`;
  if(g==="blackjack")return `${esc(titleAgent(last.agent||last.player))} ${esc(last.action||last.op||last.result||"行动")}`;
  if(g==="holdem")return `${esc(titleAgent(last.agent||last.player))} ${esc(last.action||last.op||last.type||"行动")}${last.amount!=null?` ${esc(last.amount)}`:""}`;
  return esc(JSON.stringify(last));
}

/* ---------- Gomoku ---------- */
function parseBoard(s){
  if(Array.isArray(s.board))return s.board.map(r=>Array.isArray(r)?r.slice():[]);
  if(typeof s.board==="string"){
    const rows=s.board.split(/\r?\n/).filter(x=>/^\s*\d+\s/.test(x));
    const b=rows.map(line=>line.trim().split(/\s+/).slice(1).map(v=>{
      if(["X","●","B","black"].includes(v))return "X";
      if(["O","○","W","white"].includes(v))return "O";
      return ".";
    }));
    if(b.length===15&&b.every(r=>r.length>=15))return b.map(r=>r.slice(0,15));
  }
  return Array.from({length:15},()=>Array(15).fill("."));
}
function rcFromCoord(coord){
  const cols="ABCDEFGHJKLMNOP";
  const m=String(coord||"").toUpperCase().match(/^([A-HJ-P])(\d{1,2})$/);
  if(!m)return null;
  const c=cols.indexOf(m[1]), r=Number(m[2])-1;
  return c>=0&&r>=0&&r<15?[r,c]:null;
}
function winCells(board){
  const dirs=[[1,0],[0,1],[1,1],[1,-1]];
  for(let r=0;r<15;r++)for(let c=0;c<15;c++){
    const v=board[r]?.[c]; if(!["X","O"].includes(v))continue;
    for(const [dr,dc] of dirs){
      const cells=[];
      for(let k=0;k<5;k++){
        const rr=r+dr*k,cc=c+dc*k;
        if(rr<0||rr>=15||cc<0||cc>=15||board[rr]?.[cc]!==v){cells.length=0;break}
        cells.push(`${rr},${cc}`);
      }
      if(cells.length===5)return new Set(cells);
    }
  }
  return new Set();
}
function renderGomoku(s){
  const b=parseBoard(s), wins=s.status==="finished"?winCells(b):new Set(), last=rcFromCoord((s.last_move||s.moves?.at(-1)||{}).coord);
  const cols="ABCDEFGHJKLMNOP";
  let html='<div class="go-stage"><div class="go-shell"><div class="go-grid"><div></div>';
  for(const c of cols)html+=`<div class="go-label">${c}</div>`;
  const stars=new Set(["3,3","3,11","7,7","11,3","11,11"]);
  for(let r=0;r<15;r++){
    html+=`<div class="go-label">${r+1}</div>`;
    for(let c=0;c<15;c++){
      const edge=[c===0?"edge-left":"",c===14?"edge-right":"",r===0?"edge-top":"",r===14?"edge-bottom":"",wins.has(`${r},${c}`)?"win":""].join(" ");
      const v=b[r]?.[c], isLast=last&&last[0]===r&&last[1]===c;
      html+=`<div class="go-cell ${edge}">${stars.has(`${r},${c}`)?'<i class="star"></i>':""}${v==="X"?`<i class="stone black ${isLast?"last":""}"></i>`:v==="O"?`<i class="stone white ${isLast?"last":""}"></i>`:""}</div>`;
    }
  }
  return html+"</div></div></div>";
}

/* ---------- Battleship ---------- */
function readyOf(s,a){
  for(const pool of [s.ready,s.fleet_ready,s.ready_players]){
    if(pool&&typeof pool==="object"&&a in pool)return !!pool[a];
    if(Array.isArray(pool)&&pool.includes(a))return true;
  }
  if(s.players?.[a]&&typeof s.players[a]==="object"&&"ready" in s.players[a])return !!s.players[a].ready;
  if(s.fleets?.[a]&&typeof s.fleets[a]==="object"&&"ready" in s.fleets[a])return !!s.fleets[a].ready;
  if(s.phase&&s.phase!=="fleet-setup"&&s.status!=="setup")return true;
  return false;
}
function normalizeShot(x,shooterHint){
  if(typeof x==="string")return {shooter:shooterHint,coord:x,result:""};
  if(!x||typeof x!=="object")return null;
  return {
    shooter:x.shooter||x.agent||x.by||x.player||shooterHint,
    coord:x.coord||x.target||x.cell||x.at,
    result:x.result||x.outcome||x.status||(x.sunk?"sunk":x.hit===true?"hit":x.hit===false?"miss":"")
  };
}
function shotRecords(s){
  const out=[];
  const consume=(v,hint)=>{
    if(Array.isArray(v))v.forEach(x=>{const z=normalizeShot(x,hint);if(z?.coord)out.push(z)});
    else if(v&&typeof v==="object"){
      for(const [k,x] of Object.entries(v)){
        if(/^[A-J]\d{1,2}$/i.test(k)){const z=normalizeShot({coord:k,result:x},hint);if(z)out.push(z)}
        else consume(x,k);
      }
    }
  };
  consume(s.shots);consume(s.fire_history);consume(s.history);
  if(Array.isArray(s.moves))s.moves.forEach(x=>{if(x?.coord&&(x.action==="fire"||x.type==="fire"||x.result||x.hit!==undefined)){const z=normalizeShot(x);if(z)out.push(z)}});
  const seen=new Set();
  return out.filter(x=>{const k=`${x.shooter}|${x.coord}`;if(seen.has(k))return false;seen.add(k);return true});
}
function seaCellResult(shots,a,coord){
  const x=shots.find(v=>String(v.shooter||"").toLowerCase()===String(a).toLowerCase()&&String(v.coord||"").toUpperCase()===coord);
  const r=String(x?.result||"").toLowerCase();
  if(r.includes("sunk"))return "sunk";
  if(r.includes("hit")||r==="true")return "hit";
  if(r.includes("miss")||r==="false")return "miss";
  return "";
}
function renderSeaGrid(shots,a){
  const cols="ABCDEFGHIJ";let h='<div class="sea-grid"><div></div>';
  for(const c of cols)h+=`<div class="sea-label">${c}</div>`;
  for(let r=1;r<=10;r++){
    h+=`<div class="sea-label">${r}</div>`;
    for(const c of cols){const coord=c+r,klass=seaCellResult(shots,a,coord);h+=`<div class="sea-cell ${klass}" title="${coord}"></div>`}
  }
  return h+"</div>";
}
function renderBattleship(s){
  const a=agentsOf(s).slice(0,2), shots=shotRecords(s);
  while(a.length<2)a.push(a.length?"claude":"gpt");
  const cards=a.map(agent=>{
    const ready=readyOf(s,agent);
    return `<section class="sea-card">
      <div class="sea-head"><div class="sea-name ${agentClass(agent)}">${esc(titleAgent(agent))} · 攻击海域</div><div class="ready ${ready?"yes":""}">${ready?"舰队已部署":"等待部署"}</div></div>
      ${renderSeaGrid(shots,agent)}
      <div class="fleet-row">${[5,4,3,3,2].map(n=>`<span class="ship-chip ${ready?"ready":""}">▰ ${n}</span>`).join("")}</div>
    </section>`;
  }).join("");
  return `<div class="sea-wrap">${cards}</div>${s.status==="setup"?'<div class="phase-note">部署阶段只显示准备状态；舰船坐标不会在观战接口中泄露。</div>':""}`;
}

/* ---------- cards ---------- */
function cardParts(card){
  const t=String(card??"?").trim();
  if(t==="?"||t==="??")return {rank:"◆",suit:"",back:true,red:false};
  const suit=(t.match(/[♠♥♦♣]/)||[""])[0];
  const rank=t.replace(/[♠♥♦♣]/g,"").trim()||t;
  return {rank,suit,back:false,red:suit==="♥"||suit==="♦"};
}
function cardHtml(card){
  const p=cardParts(card);
  return `<div class="play-card ${p.back?"back":""} ${p.red?"red":""}"><span>${esc(p.rank)}</span><span class="card-suit">${esc(p.suit)}</span></div>`;
}
function cardsFor(s,a){
  const candidates=[s.hands?.[a],s.cards?.[a],s.player_hands?.[a],s.hole?.[a],s.hole_cards?.[a],s.players?.[a]?.cards,s.players?.[a]?.hand,s.players?.[a]?.hole];
  for(const x of candidates)if(Array.isArray(x))return x;
  return [];
}
function pointValue(cards){
  let total=0,aces=0;
  for(const raw of cards){
    const t=String(raw).replace(/[♠♥♦♣]/g,"").toUpperCase();
    const m=t.match(/(10|[2-9]|[AJQK])/);if(!m)continue;
    const r=m[1];
    if(r==="A"){aces++;total+=11}else if(["J","Q","K"].includes(r))total+=10;else total+=Number(r);
  }
  while(total>21&&aces){total-=10;aces--}
  return total||"?";
}
function dealerCards(s){
  for(const x of [s.dealer?.cards,s.dealer_hand,s.hands?.dealer,s.cards?.dealer])if(Array.isArray(x))return x;
  return [];
}
function renderBlackjack(s){
  const agents=agentsOf(s).slice(0,2), dc=dealerCards(s);
  const dealerScore=s.dealer?.score??s.dealer_score??pointValue(dc.filter(x=>x!=="?"));
  const players=agents.map(a=>{
    const cards=cardsFor(s,a), score=s.players?.[a]?.score??s.scores?.[a]??pointValue(cards.filter(x=>x!=="?"));
    const decision=s.players?.[a]?.decision||s.decisions?.[a]||s.player_status?.[a]||"";
    return `<section class="bj-player">
      <div class="bj-head"><div class="${agentClass(a)}" style="font-weight:740">${esc(titleAgent(a))}</div><div class="score">点数 <b>${esc(score)}</b></div></div>
      <div class="cards">${(cards.length?cards:["?","?"]).map(cardHtml).join("")}</div>
      <div class="muted" style="font-size:11px;margin-top:9px">${esc(decision)}</div>
    </section>`;
  }).join("");
  return `<div class="blackjack-stage">
    <section class="dealer-zone"><div class="section-title">庄家</div><div class="cards">${(dc.length?dc:["?","?"]).map(cardHtml).join("")}</div><div class="score" style="margin-top:9px">点数 <b>${esc(dealerScore)}</b></div></section>
    <div class="players-row">${players}</div>
  </div>`;
}

/* ---------- Holdem ---------- */
function stackOf(s,a){return s.stacks?.[a]??s.players?.[a]?.stack??s.chips?.[a]??"?"}
function contribOf(s,a){return s.street_contrib?.[a]??s.contrib?.[a]??s.bets?.[a]??0}
function holdemBoard(s){
  for(const x of [s.community,s.community_cards,s.board])if(Array.isArray(x)&&x.length<=5)return x;
  return [];
}
function renderHoldem(s){
  const agents=agentsOf(s).slice(0,2);while(agents.length<2)agents.push(agents.length?"claude":"gpt");
  const ta=turnAgent(s), board=holdemBoard(s), dealer=typeof s.dealer==="string"?s.dealer:s.button||s.dealer_agent||"?";
  const seat=(a,pos)=>`<section class="seat ${pos} ${ta===a?"active":""}">
    <div class="seat-head"><b class="${agentClass(a)}">${esc(titleAgent(a))}</b><span class="stack">${esc(stackOf(s,a))} 筹码</span></div>
    <div class="cards">${(cardsFor(s,a).length?cardsFor(s,a):["?","?"]).map(cardHtml).join("")}</div>
    <div class="muted" style="font-size:10px;margin-top:7px">本街 ${esc(contribOf(s,a))}</div>
  </section>`;
  return `<div class="poker-wrap">
    <div class="poker-table">
      ${seat(agents[0],"top")}${seat(agents[1],"bottom")}
      <div class="community">${board.map(cardHtml).join("")||'<span class="muted">等待公共牌</span>'}</div>
      <div class="pot">底池 ${esc(s.pot??0)} · 当前下注 ${esc(s.current_bet??0)}</div>
    </div>
    <div class="poker-meta">
      <span class="tag">街道 ${esc(phaseName(s.street)||s.street||"翻牌前")}</span>
      <span class="tag">庄家 ${esc(titleAgent(dealer))}</span>
      <span class="tag">行动 ${esc(ta?titleAgent(ta):"—")}</span>
    </div>
  </div>`;
}
function renderStage(s){
  const g=gameOf(s);
  if(g==="gomoku")return renderGomoku(s);
  if(g==="battleship")return renderBattleship(s);
  if(g==="blackjack")return renderBlackjack(s);
  if(g==="holdem")return renderHoldem(s);
  return '<div class="empty">暂无专用渲染器。</div>';
}
function updateHeader(s){
  const g=gameOf(s),ta=turnAgent(s);
  document.getElementById("gameTitle").textContent=GAME_NAMES[g]||g||"游戏";
  document.getElementById("matchId").textContent=s.id||"—";
  document.getElementById("turnWho").innerHTML=ta?`<span class="${agentClass(ta)}">${esc(titleAgent(ta))}</span>`:"—";
  const tag=document.getElementById("statusTag");
  tag.textContent=statusName(s.status)+(s.phase||s.street?` · ${phaseName(s.phase||s.street)}`:"");
  tag.className=`tag ${s.status||""}`;
  const result=winnerText(s),rb=document.getElementById("result");
  rb.textContent=result;rb.classList.toggle("show",!!result);
}
async function refreshList(){
  rows=await (await fetch("/api/matches",{cache:"no-store"})).json();
  const f=document.getElementById("gameFilter").value,filtered=rows.filter(x=>!f||x.game===f);
  const sel=document.getElementById("matches"),old=current||sel.value;
  sel.innerHTML=filtered.map(x=>`<option value="${esc(x.id)}">${esc(GAME_NAMES[x.game]||x.game)} · ${esc(x.id)} · ${esc(statusName(x.status))}</option>`).join("");
  if(filtered.length){current=filtered.some(x=>x.id===old)?old:filtered[0].id;sel.value=current}else current="";
}
async function refresh(){
  try{
    await refreshList();
    if(!current){
      document.getElementById("arenaStage").innerHTML='<div class="empty">暂无匹配对局。</div>';
      return;
    }
    const s=await (await fetch("/api/match/"+encodeURIComponent(current),{cache:"no-store"})).json();
    updateHeader(s);
    document.getElementById("arenaStage").innerHTML=renderStage(s)+`<details class="debug"><summary>调试快照</summary><pre>${esc(JSON.stringify(s,null,2))}</pre></details>`;
    document.getElementById("matchInfo").innerHTML=matchInfoHtml(s);
    document.getElementById("lastAction").innerHTML=lastActionText(s);
    document.getElementById("chat").innerHTML=chatHtml(s);
    document.getElementById("updated").textContent="实时 · "+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("updated").textContent="读取失败";
    document.getElementById("arenaStage").innerHTML=`<div class="empty">${esc(e)}</div>`;
  }
}
document.getElementById("matches").onchange=e=>{current=e.target.value;refresh()};
document.getElementById("gameFilter").onchange=()=>{current="";refresh()};
refresh();setInterval(refresh,1000);
</script>
</body>
</html>
"""


def infer_game(state: dict, path: Path) -> str:
    value = str(state.get("game") or "").strip().lower()
    if value:
        return value
    name = path.name.lower()
    for prefix, game in PREFIX_GAME.items():
        if name.startswith(prefix):
            return game
    return path.parent.name.lower()


def iter_matches(root: Path):
    base = root / ".games" / "minigames"
    if not base.exists():
        return
    for p in base.rglob("*.json"):
        if p.name.startswith(".") or ".tmp" in p.name:
            continue
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(state, dict) or not state.get("id"):
            continue
        yield p, state


def find_match(root: Path, match_id: str):
    safe = "".join(ch for ch in match_id.lower() if ch.isalnum() or ch in "-_")
    if not safe or safe != match_id.lower():
        return None, None
    for p, state in iter_matches(root):
        if str(state.get("id", "")).lower() == safe:
            return p, state
    return None, None


def _redact_card_arrays(value):
    if isinstance(value, list):
        return ["?" for _ in value]
    return value


def sanitize_state(state: dict, game: str) -> dict:
    """Return spectator-safe data without leaking hidden game information."""
    s = copy.deepcopy(state)

    if game == "holdem" and s.get("status") != "finished" and not s.get("showdown"):
        for key in ("hole", "hole_cards"):
            if isinstance(s.get(key), dict):
                for agent in list(s[key]):
                    s[key][agent] = _redact_card_arrays(s[key][agent])
        if isinstance(s.get("hands"), dict):
            for agent in list(s["hands"]):
                if agent != "dealer":
                    s["hands"][agent] = _redact_card_arrays(s["hands"][agent])
        if isinstance(s.get("players"), dict):
            for _, info in s["players"].items():
                if isinstance(info, dict):
                    for key in ("hole", "cards", "hand"):
                        if isinstance(info.get(key), list):
                            info[key] = _redact_card_arrays(info[key])

    if game == "blackjack" and s.get("status") != "finished":
        dealer = s.get("dealer")
        if isinstance(dealer, dict) and isinstance(dealer.get("cards"), list) and len(dealer["cards"]) > 1:
            dealer["cards"] = dealer["cards"][:1] + ["?"] * (len(dealer["cards"]) - 1)
            dealer.pop("score", None)
        for key in ("dealer_hand",):
            if isinstance(s.get(key), list) and len(s[key]) > 1:
                s[key] = s[key][:1] + ["?"] * (len(s[key]) - 1)
        if isinstance(s.get("hands"), dict) and isinstance(s["hands"].get("dealer"), list):
            cards = s["hands"]["dealer"]
            s["hands"]["dealer"] = cards[:1] + ["?"] * max(0, len(cards) - 1)

    if game == "battleship" and s.get("status") != "finished":
        # Actual fleet coordinates are never needed for the spectator attack grids.
        for key in ("fleets", "ships", "placements", "boards", "oceans"):
            if key in s:
                if key == "fleets" and isinstance(s[key], dict):
                    safe_fleets = {}
                    for agent, info in s[key].items():
                        if isinstance(info, dict):
                            safe_fleets[agent] = {
                                k: v for k, v in info.items()
                                if k in {"ready", "placed", "remaining", "sunk"}
                            }
                        else:
                            safe_fleets[agent] = {"ready": bool(info)}
                    s[key] = safe_fleets
                else:
                    s.pop(key, None)

    return s


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

    def do_GET(self):
        path_only = self.path.split("?", 1)[0]

        if path_only == "/":
            data = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path_only == "/api/matches":
            rows = []
            for p, s in iter_matches(self.root):
                game = infer_game(s, p)
                rows.append({
                    "id": s.get("id"),
                    "game": game,
                    "name": GAME_NAMES.get(game, game),
                    "status": s.get("status"),
                    "phase": s.get("phase") or s.get("street"),
                    "updated_at": s.get("updated_at") or p.stat().st_mtime,
                })
            rows.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
            self._json(rows[:100])
            return

        if path_only.startswith("/api/match/"):
            match_id = unquote(path_only.split("/api/match/", 1)[1])
            p, state = find_match(self.root, match_id)
            if not p:
                self._json({"error": "not found"}, 404)
                return
            game = infer_game(state, p)
            state = sanitize_state(state, game)
            state.setdefault("game", game)
            self._json(state)
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--port", type=int, default=int(os.getenv("GAME_HALL_PORT", "8876")))
    args = parser.parse_args()
    Handler.root = Path(args.root).resolve()
    host = os.getenv("CAM_BIND_HOST", "localhost")
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"http://{host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
