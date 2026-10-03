# Third-party notices

Common AI Memory itself is licensed under the PolyForm Noncommercial License 1.0.0 (see [LICENSE](LICENSE)). It does not vendor or copy third-party source code. The packages below are installed separately by `pip` and keep their own licenses; their terms apply to them, not the PolyForm terms.

## Required dependency

| Package | License | Use |
|---|---|---|
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp`) and its dependencies | MIT (see each package's metadata) | MCP server and client transport |

## Optional dependencies

| Package / asset | License | When |
|---|---|---|
| [FastEmbed](https://github.com/qdrant/fastembed) (`fastembed`) and its dependencies (ONNX Runtime, tokenizers, Hugging Face Hub client, NumPy, …) | Apache-2.0 for FastEmbed; see each dependency's metadata | Only with the `semantic` extra |
| Embedding model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, fetched by FastEmbed as the quantized ONNX conversion `qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q` from Hugging Face | Apache-2.0 (original model by the sentence-transformers authors; the Qdrant ONNX conversion declares Apache-2.0 on its model card) | Downloaded at runtime into `DATA_DIR/state/models/` when you run a vector rebuild. Model weights are **not** included in this repository or its packages. |
| [pytest](https://pytest.org) | MIT | Only with the `test` extra |

## Optional tools you may configure

Local command-line tools used by the optional CLI Dream runner (for example Claude Code or Codex CLI), browsers used with the extension, and any API provider you connect through an adapter are third-party products under their own terms. They are not distributed with Common AI Memory and are never required.

To review the exact licenses in your environment: `pip show <package>` or `pip-licenses` (if installed).
