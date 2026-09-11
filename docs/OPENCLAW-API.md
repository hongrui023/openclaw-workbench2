# 需要在 OpenClaw 侧做什么

**这份文档回答一个问题：工作台跑起来了，但 AI 服务还没通，我该改什么？**

先明确一件事：

> **工作台不会修改你的 OpenClaw 配置。** 一行也不改。
> 这份文档只告诉你"需要开什么"，具体的修改由你自己在 OpenClaw 侧完成。
> 这样做的原因是：OpenClaw 里可能跑着你日常在用的东西，别人（哪怕是助手）
> 去改它的配置，风险不对等。

---

## 1. 工作台是怎么调用 AI 服务的

只有一个出口，只有一种形态：

```
POST  {OPENCLAW_BASE_URL}/chat/completions
      Authorization: Bearer {OPENCLAW_TOKEN}
      Content-Type: application/json

      {"model": "{OPENCLAW_MODEL}",
       "messages": [{"role":"system","content":"…"},{"role":"user","content":"…"}],
       "stream": false}
```

即 **OpenAI Chat Completions 兼容协议**。

`OPENCLAW_BASE_URL` 填到 `/v1` 为止，工作台自己拼 `/chat/completions`。
例如填 `http://192.168.1.77:51879/v1`，实际请求的是
`http://192.168.1.77:51879/v1/chat/completions`。

工作台**只收发纯文本**：入参是字符串，返回值是字符串。
它拿不到文件路径，也无从读写文件——所以即使 PDF 里藏着提示词注入，
注入的收益上限是"分析结果不准"，不可能是"读到 literature 之外的文件"。

---

## 2. 需要确认/开启的三件事

### ① HTTP 接口 —— 【2026-09-12 实测：当前是关闭的】

实测结论：

```
POST http://192.168.1.77:51879/v1/chat/completions  →  404 Not Found
GET  http://192.168.1.77:51879/                     →  200（OpenClaw Control 网页）
```

端口是通的，但那个端点没有启用。这是**官方默认行为**——
OpenClaw 文档原文：*"This endpoint is disabled by default."*

**怎么开（不需要 SSH，在网页上就能做）：**

1. 浏览器打开 `http://192.168.1.77:51879`（就是 OpenClaw Control 那个页面），登录
2. 进入 **Config** 标签页 —— 它会把当前版本的 live schema 渲染成表单
3. 找到 `gateway` → `http` → `endpoints` → `chatCompletions` → `enabled`
4. 设为 `true`，保存（界面会做校验，比手改文件安全）
5. 顺便展开 `gateway` → `auth`，记下 `mode` 和对应的密钥值（见下面第 ② 条）

> 如果表单里找不到这一层，用 Config 页里的 raw JSON 编辑器，
> 把这段合并进去：
>
> ```json5
> {
>   gateway: {
>     http: {
>       endpoints: {
>         chatCompletions: { enabled: true },
>       },
>     },
>   },
> }
> ```

> ⚠️ **开完之后请记住一件事**：官方文档对这个端点的定性是
> *"treat this endpoint as full operator access to the gateway instance"* ——
> 它是**全操作员权限**的接口，等同于你的控制台凭据。
> 所以它只能待在局域网，**绝对不能做公网映射**。
> 本方案里它也确实只对局域网开放（穿透只映射工作台的 8080）。

### ② 访问令牌

`OPENCLAW_TOKEN` 就是 Gateway 的访问令牌，工作台用它发 `Authorization: Bearer`。

取法：**同一个 Config 页** → `gateway` → `auth`：

| `gateway.auth.mode` | 令牌取自哪里 |
|---|---|
| `token` | `gateway.auth.token` 的值（或环境变量 `OPENCLAW_GATEWAY_TOKEN`） |
| `password` | `gateway.auth.password` 的值（或环境变量 `OPENCLAW_GATEWAY_PASSWORD`） |

两种模式在工作台这一侧**填法完全一样**——都填进 `OPENCLAW_TOKEN`，
适配层统一发 `Bearer`，不需要你区分。

- 拿到 **401 / 403** → 令牌不对，或令牌权限不足。
- 这个令牌**只出现在两个地方**：容器的环境变量，和适配层发出的请求头。
  它不会出现在任何 HTTP 响应里，也不会出现在前端代码或日志里。

### ③ model 字段 —— 这里有个反直觉的点

`OPENCLAW_MODEL` 会被原样放进请求体的 `model` 字段。但
**OpenClaw 把它解释成「agent 目标」，不是后端模型 id。**

官方文档原文：*"OpenClaw treats the OpenAI `model` field as an agent target,
not a raw provider model id."* 它的 `/v1/models` 列出的也是
`openclaw`、`openclaw/default`、`openclaw/` 这些 agent 目标，
**不是** `deepseek/...` 这类 provider 模型。

所以：

- ✅ 默认填 **`openclaw/default`** —— 官方点名的"稳定别名"，
  永远指向你配置好的默认 agent，即使以后改了 agent 名字也不会失效。
- ❌ **不要**填 `deepseek/deepseek-v4-flash`。那是 OpenClaw 内部的后端模型，
  由 OpenClaw 自己管，工作台既不需要、也不应该知道它。
- 拿到 **400** → 才改用 `openclaw:<某个具体 agent 名>`。
- 如果要用请求头显式指定 agent，填 `OPENCLAW_AGENT_ID`（工作台会发
  `x-openclaw-agent-id`）。用 model 字段指定时留空即可。

---

## 3. 怎么验证

导入容器后，在 NAS 上执行：

```bash
docker exec -it workbench python /app/scripts/check_openclaw.py
```

这个脚本**只读**：它只发一次极短的测试请求，不修改 OpenClaw 的任何配置，不写任何文件。

它会把失败原因和对应处置说清楚，例如：

| 自检输出 | 含义 | 你要做什么 |
|---|---|---|
| `失败 调用 /chat/completions: endpoint_not_found` | 端点没开 | 做上面第 ① 件事 |
| `失败 调用 /chat/completions: auth:401` | 令牌不对 | 核对 `OPENCLAW_TOKEN` |
| `失败 调用 /chat/completions: http:400` | model 不被接受 | 核对 `OPENCLAW_MODEL` |
| `失败 网络连接: ConnectError` | 网络不通 | 检查 `OPENCLAW_BASE_URL` 是不是填了 `127.0.0.1` |
| `OK 调用 /chat/completions: 模型已返回：正常` | 通了 | 可以正常用了 |

### 最常见的坑（没有之一）

> **容器里的 `127.0.0.1` 指的是容器自己，不是 NAS。**

你的 OpenClaw 是极空间应用商店安装的 Docker 应用（容器名 `appstore_openclaw`），
网络是 `appstore_default`（bridge），**宿主端口 `51879` → 容器端口 `28789`**。
而工作台是**另一个容器**，所以：

- 在工作台容器里填 `http://127.0.0.1:51879/v1` → 连到它自己，必然失败。
- 填容器端口 `28789` 也不行 —— 那个端口只在 `appstore_default` 网络内部可达，
  工作台容器不在那个网络里。

**必须填 `http://192.168.1.77:51879/v1`**：NAS 的局域网 IP + 宿主映射端口。

（如果哪天工作台容器也加入了 `appstore_default` 网络，就可以改用
`http://appstore_openclaw:28789/v1`，连宿主端口都不必暴露。
但那要求重建现有 OpenClaw 容器，V1 不做。）

---

## 4. 一个建议：给工作台建一个专用的受限 agent

**这一条不影响 V1 先跑起来，但它是把安全模型补完整的关键一步。**

原因：OpenClaw 的 `/v1/chat/completions` 是**全操作员权限**的接口。
谁拿到令牌，谁就等同于在控制台里操作，**包括工具调用能力**。

工作台这一侧的防线是扎实的（不给路径、不把模型输出当作路径或命令、
不提供任何代理端点）。但如果这个接口复用了你日常使用、带 exec / 文件工具的
主 agent，那么"工作台的输入是 PDF 正文（不可信内容）"这件事，就多了一层风险。

**建议做法**：在 OpenClaw 里为工作台单独建一个 agent，只给最小工具集
（理想情况是：不给任何文件、命令、网络工具，只保留对话能力），
然后把 `OPENCLAW_AGENT_ID`（或 `OPENCLAW_MODEL`）指向它。

工作台侧无需改代码——填一个环境变量即可：

```yaml
OPENCLAW_AGENT_ID: workbench
```

或者如果要用 `model` 字段区分：

```yaml
OPENCLAW_MODEL: openclaw:workbench
```

这一步**需要你在 OpenClaw 侧操作**，工作台不会代劳，也不该代劳。

---

## 5. 工作台做了哪些防注入措施

即使暂不建受限 agent，这些也已经在生效：

1. **系统提示词明确声明数据与指令的边界**：正文里的"忽略之前的所有指示"之类，
   被要求当作论文正文而不是指令（`prompts/system_guardrail.md`）。
2. **正文用明确定界符包裹**：`<document>…</document>`，模型知道哪一段是不可信输入。
3. **没有任何代理端点**：浏览器不可能借工作台之手访问 AI 服务。
4. **模型输出永不作为路径或命令**：返回的文本只经过长度校验和 Markdown 清洗，
   写入的目标文件名由工作台从**用户输入的原始 PDF 名**推导，绝不来自模型输出。
5. **AI 服务没有任何工具可用**（在工作台的调用形态下）：工作台只发纯文本，
   不给任何上下文、路径或凭据。

最强的一条其实是第 4 和第 5 条——它们是**架构性**的，不依赖模型的自觉。

---

## 6. 换成别的 AI 服务？

适配层是独立文件，接口是标准 OpenAI 协议。把 `OPENCLAW_BASE_URL`
换成任何兼容服务（vLLM、Ollama、LiteLLM、各类云厂商的兼容端点）都能直接跑。

这也意味着：**如果你不想为工作台单独开接口，完全可以指向另一个模型服务**，
不必动你现有的 OpenClaw。
