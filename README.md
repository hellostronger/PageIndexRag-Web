# PageIndex Web

基于 [PageIndex](https://github.com/VectifyAI/PageIndex) 0.2.x 的 Web 可视化 RAG 系统：
上传文档 → 生成树形索引 → 可视化树结构 → 基于树的推理式问答。

> 本仓库的 `pageindex/` 已从上游 0.1.x 升级到 **0.2.10**。

---

## 快速启动

前置：Python 3.10+。

```bash
pip install -r requirements.txt
python -m uvicorn server.main:app --host 0.0.0.0 --port 8001
```

Windows 上也可以直接双击 `start.bat`。

然后打开 **http://localhost:8001** —— 前端已预编译在 `server/static/` 下，
由后端直接托管，**不需要装 Node.js**。改前端时才需要 `cd web && npm install && npm run dev`（开发模式跑在 3000 端口）。

首次使用点右上角 ⚙️ **Settings**，填 API Key、Base URL、模型名，
点 **Test Connection** 验证后保存。

---

## 配置

### 凭据（`.env`）

```ini
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
```

内核通过 **LiteLLM** 调用模型。裸模型名（`gpt-4o`）走 `openai/` provider
并自动遵循 `OPENAI_BASE_URL`，所以任何 OpenAI 规范的中转站、DeepSeek、Kimi、
Qwen 都能直接用，不用改代码。Base URL 留空即官方 OpenAI。
其它 provider 写 `provider/model`，例如 `anthropic/claude-sonnet-4-6`。

### 模型角色（`pageindex/config.yaml`）

| 键 | 作用 |
|---|---|
| `index_model` | 建树 + 写节点摘要。**用便宜的就够**，结构主要来自版式而非模型 |
| `chat_model` | 搜树并回答。**用你最好的** |
| `summary_model` | 缺省继承 `index_model` |

### PDF 处理管线

| 键 | 取值 | 说明 |
|---|---|---|
| `mode` | `flash` / `standard` | `flash` 从版式统计直接出树，模型只写摘要；快且便宜。`standard` 让模型也参与建结构，PDF 没有可用版式结构时需要 |
| `optimize` | `full` / `merge` / `off` | `full` = 合并 + 模型扩展（检索成本最低） |
| `summary` | `true` / `false` | 是否让模型写节点摘要 |
| `use_embedded_toc` | `true` / `false` | 目录书签可信时采用 |

---

## 与上游 Web 版的差异

上游 `PageIndexRag-Web` 打包的是 PageIndex **0.1.x** 内核（`page_index.py`，
直接调 OpenAI SDK，为 DeepSeek 改过）。这里换成了 **0.2.10**，相应地改了这几处：

| 文件 | 改动 |
|---|---|
| `pageindex/` | 整包替换为上游 0.2.10（新增 `flash/`、`page_index_classic.py`、litellm、`ConfigLoader`） |
| `server/services/llm.py` | **新增**。凭据与模型路由的唯一出口，对齐到 LiteLLM 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` |
| `server/services/document_service.py` | PDF 走 `page_index_flash`（或 `page_index_main`）；模型名走 `ConfigLoader`；页文本单独存到 `results/{id}_pages.json` |
| `server/services/tree_service.py` | 新增 `load_pages()` / `attach_node_text()` —— Flash 树只存页码区间，提问时才去取那几页正文 |
| `server/services/chat_service.py` | 从 `AsyncOpenAI` 改为 `litellm.acompletion`；命中节点按需拼接页文本 |
| `server/routers/config.py` | 读写 `index_model` / `chat_model` / `summary_model`；连接测试走真实的 litellm 通路 |
| `server/main.py` | `/api/health` 移到静态挂载之前（原先被 `/` 挂载遮住） |

前端和 API 契约（`/api/documents`、`/api/chat`、`/api/config` 的请求体与 SSE 事件）
保持原样，**无需重新构建**。

---

## 文档是怎么被读���

1. **索引**：Flash 管线从 PDF 版式统计直接抽出层级树（这一步不用 LLM），
   模型只为每个节点写摘要，并把过粗的节点展开。
2. **检索**：把**去掉正文的树骨架** + 用户问题发给 chat 模型，
   让它像人翻目录一样推理出相关节点。
3. **作答**：只取命中节点对应的那几页原文，交给模型生成答案。

所以成本随文档变长而增长得很慢——读的是推理走到的那几个节点，不是整篇文档。
没有向量库，也没有分块。

---

## 支持的文件格式

| 格式 | 处理方式 |
|---|---|
| PDF | 版式 + 模型 → 树 |
| Markdown | 按标题层级 → 树 |
| TXT / JSON / CSV / Word | 先转 Markdown，再走同一条管线 |

---

## 许可

MIT