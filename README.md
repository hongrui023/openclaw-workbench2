# openclaw-workbench

**科研 & 生活本地工作台** —— 部署在极空间 NAS 上的独立小应用，提供两个功能：

1. **科研文献分析**：把 `literature/` 里的 PDF 变成结构化的 Markdown 分析报告
2. **生活记录**：待办 / 收支 / 随笔，自动分类后追加到 `life_notes/daily_notes.md`

它**独立运行**，不依赖任何开发期工具。数据全部留在你自己的 NAS 上。

---

## 它不是什么

说清楚边界比说清功能更重要：

- ❌ 不是文献管理软件。没有标签、没有检索、没有引用统计、没有推荐。
- ❌ 不联网同步任何数据。除了调用你 NAS 上那个 AI 服务，它不向外发任何请求。
- ❌ 不能删除任何文件。**代码里根本不存在删除能力**——想删文件请去极空间文件管理器。
- ❌ 不做后台扫描。没有定时任务、没有文件监听，所有动作都由你的点击触发。

---

## 架构一览

```
互联网（手机 / 电脑）
      │  节点小宝内网穿透（唯一的公网入口）
      ▼
工作台 Web 应用（8080）           ← 只有它对外
      │
      ▼
AI 服务（NAS 内部，不对公网暴露）
      │
      ▼
literature/   life_notes/         ← 只有这两个目录可读写
```

四条硬边界：

| 边界 | 含义 |
|---|---|
| AI 服务不作公网入口 | 穿透只映射 8080，AI 服务的端口不做任何映射 |
| 浏览器碰不到 AI 服务 | 没有代理端点，前端代码里不出现任何服务标识或令牌 |
| 令牌只存在于后端 | `OPENCLAW_TOKEN` 只在容器环境变量和一处请求头里出现 |
| 文件访问只有两个目录 | 容器只挂载两个卷，其他路径在内核层面就不存在 |

---

## 快速开始（本地开发，不上 NAS）

先在电脑上把逻辑跑通。**这一步能挡掉绝大部分 bug**——因为"构建 → 传输 → 导入极空间"那条循环很慢。

```bash
# 1) 准备环境（Python 3.12+）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt        # macOS / Linux

# 2) 启动（开发模式：明文口令，自动打开接口文档）
powershell -ExecutionPolicy Bypass -File scripts\dev_run.ps1      # Windows
# bash scripts/dev_run.sh                                          # macOS / Linux

# 3) 打开 http://127.0.0.1:8080
#    口令：启动脚本会随机生成一个 32 位口令并打印在终端里（每次重启都不同）
#    想固定下来：先设好 WORKBENCH_PASSWORD 再运行脚本
```

本地开发时，数据写在仓库内的 `var/dev-data/`，不会碰 NAS。

### 先跑自检（不需要 AI 服务）

```bash
python scripts/selftest.py        # 业务逻辑 + 权限逻辑 + HTTP 层，共 97 项检查
python scripts/smoke_http.py      # 真的起一个 uvicorn，检查对外行为与响应头
python scripts/check_deploy.py    # Dockerfile / ARM64 依赖轮子 / compose / 端口暴露
python scripts/secret_scan.py     # 提交前扫描：机密、私人数据、危险调用
```

自检全部写入临时目录，跑完自动清理。四个脚本各管一段：

| 脚本 | 回答什么问题 |
|---|---|
| `selftest.py` | 逻辑对不对（路径守卫、失败处理、追加语义、分段归并） |
| `smoke_http.py` | 对外行为对不对（响应头、缓存、认证拦截、首屏体积） |
| `check_deploy.py` | 部署配置对不对（**不需要 Docker 也能跑**） |
| `secret_scan.py` | 有没有不该进仓库的东西（提交前必跑） |

---

## 部署到极空间

见 **[docs/DEPLOY-NAS.md](docs/DEPLOY-NAS.md)**（照着点就能做完）。

一句话版本：

```bash
# 在你的电脑上交叉构建（极空间不能 docker build）
powershell -ExecutionPolicy Bypass -File scripts\build-arm64.ps1 -Version 1.0.0
# → 得到 openclaw-workbench-1.0.0.tar
# → 上传到极空间 → Docker → 镜像 → 导入镜像
# → 创建容器：2 个数据挂载 + 8 个环境变量 + 1 个端口
```

---

## ⚠️ 部署前必须先做的一件事：确认 AI 服务可用

工作台通过 **OpenAI 兼容的 HTTP 接口**调用你 NAS 上的 AI 服务。这个接口**默认可能是关闭的**。

```bash
# 导入容器后，在 NAS 上执行（只读检查，不会改任何配置）
docker exec -it openclaw-workbench python /app/scripts/check_openclaw.py
```

它会明确告诉你卡在哪一步、以及该做什么。**需要你手动开启什么，以及为什么工作台不替你改**，都写在 **[docs/OPENCLAW-API.md](docs/OPENCLAW-API.md)**。

---

## 环境变量

全部配置走环境变量，没有配置文件（少一个必须挂载的文件，也避免"模板进 Git、真值忘排除"）。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WORKBENCH_PASSWORD_HASH` | ✅ | — | 登录口令的 scrypt 哈希。`python scripts/make_password_hash.py` 生成 |
| `OPENCLAW_BASE_URL` | ✅ | — | 例 `http://192.168.1.20:18789/v1`。**容器里绝不能填 127.0.0.1** |
| `OPENCLAW_TOKEN` | ✅ | — | 访问令牌，只在后端使用 |
| `OPENCLAW_MODEL` | | `openclaw:main` | 发给 AI 服务的 model 字段 |
| `OPENCLAW_AGENT_ID` | | 空 | 可选，指定专用受限 agent |
| `SESSION_DAYS` | | `30` | 会话有效期 |
| `LOGIN_LOCKOUT_MINUTES` | | `30` | 连续登录失败后的锁定时长 |
| `CHUNK_MAX_CHARS` | | `48000` | 单块字符上限；≤ 此值走直通模式 |
| `CHUNK_OVERLAP_CHARS` | | `800` | 相邻块重叠，避免切断语义 |
| `REDUCE_FAN_IN` | | `6` | 归并分组大小 |
| `MAX_CHUNKS` | | `60` | 分块数上限，超过判定为异常文档 |
| `MAX_PDF_MB` / `MAX_PDF_PAGES` | | `100` / `500` | 单文件上限 |
| `TZ` | | `Asia/Shanghai` | 影响记录时间戳 |
| `WORKBENCH_DEV` | | 空 | **仅本地开发**。设了它才能用明文口令并打开 `/docs` |
| `MARKDOWN_VIEW_MAX_CHARS` | | `40000` | 网页上查看分析结果时的长度上限 |

完整模板见 [`.env.example`](.env.example)。

---

## 目录结构

```
openclaw-workbench/
├─ app/
│  ├─ main.py                  应用入口、中间件、异常处理
│  ├─ config.py                全部配置来自环境变量
│  ├─ errors.py                错误码 → 预定义用户文案（不泄露内部信息）
│  ├─ models.py                请求/响应模型
│  ├─ security/
│  │  ├─ paths.py              ★ 路径守卫：全项目唯一的文件操作通道
│  │  ├─ auth.py               口令校验、会话、登录限速
│  │  └─ sanitize.py           日志脱敏（防内网地址/令牌外泄）
│  ├─ services/
│  │  ├─ openclaw.py           ★ AI 服务适配层：全项目唯一的调用出口
│  │  ├─ pdf_reader.py         PDF → 按页文本（含失败分类）
│  │  ├─ chunker.py            按页边界分块（保留页码定位）
│  │  ├─ literature.py         文献分析编排（直通 / 分段 + 分层归并）
│  │  ├─ notes.py              生活记录分类与追加
│  │  ├─ jobs.py               进程内异步任务队列
│  │  ├─ prompts.py            提示词模板加载
│  │  └─ task_log.py           任务日志追加
│  ├─ api/                     路由（auth / literature / notes / jobs / system）
│  └─ static/                  零外链前端（HTML + CSS + JS，约 30 KB）
├─ prompts/                    提示词模板（建议挂载，改完重启即可生效）
├─ scripts/                    自检、交叉构建、口令哈希、连通性检查
├─ docs/                       部署、安全、接口说明
├─ Dockerfile
├─ docker-compose.yml
└─ requirements.txt
```

---

## 长文献是怎么处理的

**不做简单截断。** 长度决定走哪条路：

```
抽取全文 → 统计字符数
  ├─ ≤ CHUNK_MAX_CHARS → 直通模式（1 次调用，快且省）
  └─ > CHUNK_MAX_CHARS → 分段模式
                         ① 按【页边界】切块（不是按字符数硬切），每块带页码标记
                         ② 逐块抽取六类要点（串行，照顾 4GB 内存）
                         ③ 份数超过 REDUCE_FAN_IN 就分组归并，可多轮
                         ④ 汇总为最终六段式 Markdown
```

最终 Markdown 的每个小节末尾都标注来源页码（`> 来源：原文第 15–24 页`）——这既是给你查证用的，也是防编造的可审计手段。

**失败处理**（对应"不编造、不无限重试、不产出脏文件"）：

| 情况 | 行为 |
|---|---|
| 单块失败 | 网络类错误重试 1 次；仍失败则记为该块缺失，**继续处理其余块** |
| 缺失块 > 30% | 整篇判定失败，**不写任何文件** |
| 缺失块 ≤ 30% | 继续，但在 Markdown 顶部**显著标注缺了哪些页码** |
| PDF 无法解析 | 明确报告原因（加密/无文本层/损坏/超限），**绝不调用模型去猜** |
| 模型输出缺必备小节 | 判定失败，**不写文件**（宁可没有文件，也不要半成品） |

---

## 更新流程

| 改了什么 | 要做什么 |
|---|---|
| 只改提示词（`prompts/`） | 在极空间文件管理器里直接编辑 → **重启容器**。不用重建镜像 |
| 改了代码 | `build-arm64.ps1` → 上传 tar → 导入新 tag → 用同一份配置重建容器 |

---

## V1 明确不做

文献统计、文献数据库、月度报表、推荐系统、后台扫描、OCR（扫描版 PDF 直接报告不支持）、文件删除、多用户与权限体系、PDF 上传下载、图片预览、移动端专门适配。

---

## 安全模型

见 **[docs/SECURITY.md](docs/SECURITY.md)**。其中有一条必须提前知道：**当前外网入口是 HTTP**（节点小宝的 http 服务类型），浏览器到穿透节点这一段是明文。文档里写了缓解措施，以及日后彻底消除该风险的备选方案。
