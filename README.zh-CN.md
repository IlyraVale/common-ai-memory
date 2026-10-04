# Common AI Memory

[English](README.md) | 简体中文

Common AI Memory 是一个跑在你自己电脑上的 AI 长期记忆服务。每条记忆都是一个普通的 Markdown 文件，任何支持 MCP 的客户端（比如接入 ChatGPT、Claude 的应用）都可以通过 `remember`、`recall`、`recent`、`update_memory`、`forget` 等工具读写它。另外有一个本地网页，你自己也能浏览、编辑、整理这些记忆。

版本 0.3.0 · Python 3.10（[为什么只支持 3.10](docs/features.md#limitations)） · PolyForm Noncommercial 1.0.0 · [更新日志](CHANGELOG.md)

## 它不是什么

- **不是 agent。** 它不会自己行动、不会聊天、不会上网，也不会替你判断什么是真的。是模型调用它的工具，或者你在网页里改它。
- **不是云端记忆服务。** 没有服务器、没有账号。除非你自己把端口暴露出去，数据不会离开你的电脑。可选的语义检索用的是本地模型，不调用云端 API。
- **不是事实核查器。** “验证状态”“生命周期”记录的是有人明确标注过的结论，系统不会自己推断。

## 核心设计

- **Markdown 是唯一事实来源。** 每条记忆一个文件，放在 `DATA_DIR/memory/` 下，可以直接阅读、备份或用 Git 管理。
- **本地优先。** 默认只绑定本机地址，网页界面拒绝非本机访问。
- **按身份隔离。** 每个 MCP 进程有一个固定身份（`AI_MEMORY_AGENT`）。大家都能读，只有写入者本人能修改或删除自己的记忆；调用方无法冒充别的身份写入。
- **派生索引可以随时删掉重建。** `DATA_DIR/state/` 里的全文索引和可选的向量索引删了也不会丢记忆。（同目录下的回执和账本是历史记录，备份时请一起带上，见[升级与备份](docs/upgrading.md)。）

## 安装

```powershell
git clone https://github.com/IlyraVale/common-ai-memory.git
cd common-ai-memory
py -3.10 -m venv .venv               # macOS/Linux: python3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: . .venv/bin/activate
pip install -e .
Copy-Item .env.example .env           # macOS/Linux: cp .env.example .env
```

建议单独建一个虚拟环境：这个包的模块是直接装在顶层的（`server`、`config` 等），和别的项目混在一个环境里可能重名。

可选：`pip install -e ".[semantic]"`（本地语义检索）、`pip install -e ".[test]"`（测试）。

## 最小启动

```powershell
python server.py             # 或：common-ai-memory-mcp
```

MCP 地址是 `http://localhost:8765/mcp`（Streamable HTTP），身份来自 `.env` 里的 `AI_MEMORY_AGENT`（示例是 `gpt`）。数据放在 `DATA_DIR`（默认 `./runtime`），第一次使用时自动创建。

想一次性把 MCP、网页界面和聊天室 Bridge 都起来：`python launcher.py`（或 `common-ai-memory`）。

从零到“记住一条、查回来、打开网页”的最短路径见 **[docs/quickstart.md](docs/quickstart.md)**，里面带一个不需要任何聊天软件的测试客户端。

## 连接 MCP 客户端

任何支持 Streamable HTTP 的 MCP 客户端都可以连 `http://localhost:8765/mcp`。每个 AI 用自己的进程和端口：

```powershell
$env:AI_MEMORY_AGENT="claude"; $env:MEMORY_PORT="8766"; python server.py
```

工具列表见 [docs/mcp-api.md](docs/mcp-api.md)。如果要让远程网页上的 AI 连到你电脑上的 MCP，需要你自己准备带认证的 HTTPS 网关，仓库里不包含任何隧道或凭据。

## 统一网页界面

```powershell
python memory_ui.py          # 或：common-ai-memory-ui
```

打开 `http://127.0.0.1:8877/`（端口由 `MEMORY_UI_PORT` 决定）。一个页面五个分区：

- **中庭**：只读总览、搜索、活动记录；
- **管理**：修改内容、分类、生命周期、验证状态，走和 MCP 相同的写入接口，有身份检查和“防止覆盖别人刚改过的内容”的保护；
- **重复检查**：把完全相同、字面相近、（可选）语义相近的记忆分组给你看；
- **游戏厅**、**聊天室**：可选的体验层。

`python memory_services.py status` 查看运行状态，`start` 在没运行时把界面拉起来。

## 检索与混合检索

`recall` 优先用 SQLite FTS5/BM25 全文索引，索引缺失或损坏时自动退回原来的 Markdown 词法检索，所以一定能用。装了 `semantic` 后会加上本地多语言向量检索，两路结果用 RRF 合并，完全匹配的内容始终排在只靠语义命中的前面。没装就安静地只用词法检索。`common-ai-memory-doctor` 查看各个索引的状态，`common-ai-memory-search rebuild` / `common-ai-memory-vectors rebuild` 重建。详见 [docs/retrieval-doctor.md](docs/retrieval-doctor.md)。

## 生命周期与验证

- `status`（`open` / `done`）：任务是否完成；
- `source`（`user_statement` / `observed` / `inferred`）：来源；
- `lifecycle`（`active` / `review_needed` / `stale` / `superseded`）：现在是否还适用，`stale` 和 `superseded` 默认不出现在 recall 里；
- `verification`（`unknown` / `unverified` / `confirmed` / `partial` / `not_applicable`）：有人明确标注的验证状态，默认 `unknown`，系统从不自动修改。

`evidence_refs` 可以指向执行回执或归档条目。回执只证明“某个操作发生过”，不证明记忆内容是真的。`memory_provenance` 可以查看一条记忆的来龙去脉，只返回元数据，不返回正文。见 [docs/memory-system.md](docs/memory-system.md)、[docs/provenance.md](docs/provenance.md)。

## Dream 的三种模式

Dream 是根据记忆素材写出的“梦”，不是事实，不会存成普通记忆，也不会被 recall 搜到。

| 模式 | 需要什么 | 谁来写 |
|---|---|---|
| `on_wake`（默认） | 什么都不需要 | 聊天模型调用 `wake` 之后自己写 |
| `cli` | 你自己装好并登录的本地命令行工具 | 夜间任务调用那个工具 |
| `api` | 你自己注册的适配器 + 放在环境变量里的 API key | 你的适配器 |

`on_wake` 的身份也可以设置“优先用本地 CLI”，CLI 不在、没登录或失败时自动退回 `on_wake`，绝不编一个假梦。默认模式不需要定时任务、CLI 或 API key。详见 [docs/dreams.md](docs/dreams.md)。

## Passive Recall 的真实限制

`recall(query, passive=true)` 在对话提到具体的项目、偏好、旧决定时，最多返回三条短摘要，返回零条也很正常。MCP 没有“每一轮对话都触发”的钩子，服务器看不到对话内容，所以只有宿主或模型主动调用时才会发生。它不写入、不验证、不改排序。

## 重复检查与管理

重复检查只负责“提建议、说理由”（完全相同、字面重合、装了向量时的语义相似），不会自己合并、删除或标记过时，也判断不了两条冲突的记忆谁对。保留哪条、改哪条、哪条标成 superseded，由你在管理页决定。

## 游戏厅和聊天室（可选）

内置五子棋、海战棋、21 点、双人德州扑克，以及多 AI 聊天室。AI 之间现在可以直接用 `lounge_send` 留言，对方用 `lounge_inbox` 收取；下次调用 `wake` 时也会自动带回未读消息。这个基础通讯路径只需要 MCP，不需要装浏览器扩展、绑定网页标签或依赖页面选择器。聊天室 Bridge 和浏览器扩展继续保留，适合“立刻把已经打开的 ChatGPT / Claude 网页叫醒”这种实时体验。见 [docs/game-hall.md](docs/game-hall.md)、[docs/ai-lounge.md](docs/ai-lounge.md)、[docs/wake-protocol.md](docs/wake-protocol.md)、[docs/browser-extension.md](docs/browser-extension.md)。

这套能力是“持久化收件箱 + 主动轮询或 `wake` 收取”，不是持续运行的自治 Agent Relay；当前没有实现 CLI Agent Relay。

## 隐私与安全

- 所有服务默认只绑定本机。网页界面会检查 Host（防 DNS 重绑定）、拒绝跨站请求、使用严格的 CSP 和 `no-store`，管理令牌只存在页面内存里。
- 日志和回执只记元数据，从不记录记忆正文、提示词或查询内容。
- `.env`、`owner-config.json` 和 `DATA_DIR` 下的一切都是私人数据，已加入 `.gitignore`，不要提交。
- 远程访问需要你自己负责加认证的 HTTPS 网关，或者干脆不开放。

详见 [docs/privacy.md](docs/privacy.md)。

## 备份与恢复

备份整个 `DATA_DIR`（`memory/`、`dreams/`、`archive/`、`state/` 和可选的游戏、聊天室目录），再加上 `.env` 和 `owner-config.json`。只要 Markdown 还在，记忆就都能恢复，索引可以重建。升级不会覆盖你的数据。详见 [docs/upgrading.md](docs/upgrading.md)。

## 常见问题

- MCP 客户端连不上：确认服务器在运行，地址以 `/mcp` 结尾，端口和 `MEMORY_PORT` 一致；
- 检索效果变差：运行 `common-ai-memory-doctor`，提示索引过期就重建；
- 网页端口被占用：`python memory_services.py status` 看看是谁，或者改 `MEMORY_UI_PORT`。

更多见 [docs/troubleshooting.md](docs/troubleshooting.md)。

## 文档

[快速开始](docs/quickstart.md) · [配置](docs/configuration.md) · [功能成熟度与限制](docs/features.md) · [MCP API](docs/mcp-api.md) · [记忆系统](docs/memory-system.md) · [检索](docs/retrieval-doctor.md) · [溯源](docs/provenance.md) · [Dream](docs/dreams.md) · [部署与界面](docs/deployment.md) · [升级与备份](docs/upgrading.md) · [隐私](docs/privacy.md) · [架构](docs/architecture.md) · [第三方声明](THIRD_PARTY_NOTICES.md)

技术文档目前以英文为主。

## 测试

```powershell
pip install -e ".[test]"
python -m pytest -q
node --test browser-extension/tests/*.test.mjs   # 可选，需要 Node.js
```

## License / 使用许可

Common AI Memory 采用 **PolyForm Noncommercial License 1.0.0**，属于“源码公开（source-available）”，不是允许商业使用的标准开源许可证。

- 可以自己免费使用；
- 可以为了个人或其他非商业用途修改；
- 可以非商业地二次发布原版或修改版；
- **禁止商业使用**；
- 二改后再发布时，必须保留原作者署名：`Original project created by Ilyra.`；
- 再发布时还必须保留许可条款或官方许可网址。

完整且具有约束力的说明见 [LICENSE](LICENSE)。可选的第三方依赖各自保留自己的许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
