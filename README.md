# 视频下载流水线

本地 / NAS / Docker 自托管的 HLS 视频下载工具，带一个零依赖的单页 Web 控制台。
核心能力：**列表页自动翻页 → 播放页自动找 m3u8 → AES-128 解密 → ffmpeg 合成 MP4**，
外加基于 VDF（video duplicate finder）的「画面内容查重」。

> 设计原则：不含任何站点专用逻辑。所有站点适配都靠**通用策略链（静态正则 → 加密
> blob 试解 → 无头浏览器兜底）**完成；**规则全部外置到 `app/rules/*.yaml`**，换站只改
> YAML 或新增一个解析器文件，不改核心代码；站点换域名只改界面上的链接。

---

## 1. 目录结构

| 路径 | 职责 |
| --- | --- |
| `main.py` | FastAPI 入口：路由、参数校验、lifespan（孤儿 chromium 清理、崩溃日志）、静态页 |
| `index.html` | 单页控制台（原生 JS，无构建步骤） |
| `app/core/models.py` | Pydantic 模型：`JobConfig` / `VideoItem` / `JobSnapshot` / `DedupConfig` 等 |
| `app/core/parsing.py` | 解析器接口（`ListParser` / `Resolver`）+ 运行期上下文 `ParserContext`；解析器在 `Job` 里显式实例化 |
| `app/core/rules.py` | **规则引擎 `RuleManager`**：加载并编译 `rules/*.yaml`，解析器/Job 唯一取数入口 |
| `app/core/exceptions.py` | `StopJob` / `JobAborted` 流水线专用异常 |
| `app/parsers/` | 解析器实现：`list/static` / `list/browser` / `play/static` / `play/browser` |
| `app/rules/` | **规则配置（单一来源）**：`title_rules.yaml` / `link_rules.yaml` / `pipeline_rules.yaml` |
| `app/services/job.py` | **任务编排 `Job`**：组合列表解析、单视频 worker、HLS、浏览器/域名探测 |
| `app/services/job_state.py` | **任务状态 `JobState`**：状态/进度/快照/视频队列/日志缓冲/熔断计数 |
| `app/services/list_runner.py` | **列表流水线 `ListRunner`**：候选域名 → 列表页并发解析 → 有序入队派发 |
| `app/services/video_worker.py` | **单视频 `VideoWorker`**：播放页解析、下载、重试、跳过、失败记录 |
| `app/services/hls.py` | **HLS 下载器 `HlsDownloader`**：密钥/IV、分片池、解密合并、ffmpeg |
| `app/services/browser.py` | **浏览器会话 `BrowserSession`**：Playwright 启动、上下文复用、页面借还 |
| `app/services/domain.py` | **域名探测 `DomainProbe`**：直连/30x/CNAME/发布页跳转、运行期重探；播放页提示的新域名需能打开列表页才采纳 |
| `app/services/dedup_service.py` | VDF 查重与清理状态机 `DedupService`（永久删除有 dry-run 门禁 + 文件未变化校验） |
| `app/toolkit/` | 工具：`codec` / `files`（文件与异步日志）/ `http`（限速+私网拦截）/ `m3u8` / `segments` / `probe`（ffprobe 成品校验）/ `title` / `url` / `dns` |
| `tests/` | 单测（纯函数 + Job 状态机 + 列表失败页 + HLS 完整性 + 查重安全门禁 + 持久化）与端到端加密 HLS 实测 |
| `Dockerfile` / `docker-compose.yml` | 镜像：系统 ffmpeg、VDF CLI、预置 AI 组件、chromium |
| `.github/workflows/ci.yml` | CI：ruff check / ruff format --check / mypy / pytest |

运行期产物：

- 输出目录 `<outdir>/`（默认 `OUTDIR` 或仓库根 `downloads/`）
  - `NNN_标题.mp4` — 成品（写完先用 ffprobe/ffmpeg 校验容器与时长，坏片会删掉重试）
  - `NNN_标题.mp4.tmp` — ffmpeg 合成中的临时成品；成功后 `os.replace` 原子改名，
    失败/停止时删除（避免把半截 MP4 误判为已完成）
  - `NNN_标题.tmp.ts` — 解密合并中的中间流；失败时保留为 `.ts` 便于手工转封装
  - `NNN_标题.tmp.ts.parts/` — 分片缓存（断点续传用，成功后删除；指纹含域名/媒体序列号/ENDLIST）
  - `manifest_web.json` — 本次解析出的视频清单
  - `_job_state.json` — 任务状态快照（进程重启后界面仍能展示上次任务，并支持一键重试）
  - `_seen_web.json` — 历史已下载清单（「增量下载」勾选后只下清单外的新视频）
  - `_web_log.txt` — 任务日志（追加写，超过 5MB 自动轮转为 `.1`）
  - `_failed_web.txt` — 失败清单（供人工排查/导入；「一键重试」用的是内存里的任务状态）
  - `_vdf_results.json` / `_vdf_db/` — 查重结果与 VDF 帧哈希库
- 日志目录 `<outdir>/_logs/`（可用 `YS_LOG_DIR` 改）
  - `_server_dbg.log` — 未捕获异常
  - `_faulthandler.log` — faulthandler 崩溃栈（不再写进源码目录 `app/`）

> 停止与重启：uvicorn 收到 SIGTERM 时会走 lifespan 收尾——置停止信号、杀 ffmpeg 子进程、
> 回收分片线程池与浏览器；启动时会清理上次残留的 chromium（只按本项目启动标记匹配，
> 不再全机扫描 `ms-playwright`，不会误杀其它项目的 Playwright）。

---

## 2. 端到端流程

```mermaid
flowchart TD
    A[POST /api/start] --> B[_build_job_config<br/>校验链接/页码/输出目录]
    B --> C[新建 Job + 后台线程]
    C --> D[候选列表链接探测<br/>直连 → 域名兜底 → 发布页跟随]
    D --> E[逐页解析列表<br/>static_list → browser_list 兜底]
    E --> F[add_items: 去重入队 + 立刻派发]
    F --> G[_work 单个视频]
    G --> H{MP4 已存在?}
    H -- 是 --> I[跳过 s=4]
    H -- 否 --> J[磁盘余量检查]
    J --> K[播放页解析<br/>static_resolver → browser_resolver 兜底]
    K --> L[master 列表取最高码率]
    L --> M[解析分片 + AES-128 KEY/IV]
    M --> N[共享线程池下分片<br/>.part → 原子改名]
    N --> O[逐片解密 → 合并 .tmp.ts]
    O --> P[ffmpeg -c copy → MP4]
    P --> Q[成功 s=2 / 失败 s=3]
    Q --> R{连续失败 ≥ streak_limit?}
    R -- 是 --> S[熔断: JobAborted 中止任务]
    R -- 否 --> E
    E --> T[等全部 future 完成 → _finish]
    T --> U[状态收尾: s=1 退回 s=0, 关线程池/浏览器]
```

列表解析与下载是**流水线并行**的：每解析出一页，就立刻把这一页的视频提交给线程池。

---

## 3. 任务状态

### 3.1 视频状态 `video.s`

| 值 | 含义 |
| --- | --- |
| 0 | 排队中 |
| 1 | 进行中 |
| 2 | 完成 |
| 3 | 失败 |
| 4 | 跳过（目标 MP4 已存在） |

### 3.2 视频步骤 `video.step`

`1` 播放页 → `2` 密钥 → `3` 分片（带 `sd/sn` 进度）→ `4` 合成 → 完成态直接显示全亮。

### 3.3 全局阶段 `stages`

| 阶段 | 关键字段 | 说明 |
| --- | --- | --- |
| `list` | `pages/pages_total/found/failed/failed_pages` | 列表页解析进度；抓取失败的页单独计数，不会当成“空页”提前收工 |
| `play` | `resolved/total` | 播放页解析成功数 |
| `key` | `got/method/iv` | 密钥获取数、加密方式、IV 前缀 |
| `seg` | `done/skip/current` | 已完成分片总数、跳过数、当前视频 |
| `dec` | `ok/fail` | 解密/合成成功、失败数 |
| `out` | `count/gb/recent` | 产出数量、总体积、最近 8 条 |

进度条两段拼成：列表阶段 `2%~10%`，下载阶段 `10%~98%`（按 `ok+fail+skip/total`）。

快照里还有 `warnings`（非致命警告：列表页抓取失败、直播流被跳过、成品校验未通过等），
前端显示在进度条下方；`failed_pages` 也随 `/api/job` 快照返回。

### 3.4 查重状态 `DedupService.st`

`available` / `stage`（`idle/scanning/done/error`）/ `percent` / `current` / `groups`
（按可释放空间降序）/ `last_apply` / `log`。扫描与清理**刻意拆成两步**。

---

## 4. 规则与解析器（换站不用改代码）

### 4.1 规则外置

所有正则、关键词、阈值都收在 `app/rules/*.yaml`，运行期由 `RuleManager` 一次性加载编译：

- `title_rules.yaml`：标题清洗的分离符 / 前后缀 / 站名词 / 集数 / 套话。
- `link_rules.yaml`：列表页链接的排除词 / 扩展名 / 广告特征 / 播放页提示 / id 特征 / 标题块 class。
- `pipeline_rules.yaml`：熔断阈值、分片并发、重探冷却、磁盘下限、不可重试关键词、
  发布页提示、跳转信号、m3u8 正则、加密 blob 形态、浏览器触发/标题候选选择器、
  发布页按钮文字、页面新域名识别正则、换域名后的列表路径候选模板。

### 4.2 解析器策略

列表页与播放页解析都是 `ListParser` / `Resolver` 的实现，在 `Job._ensure_parsers()`
里**显式实例化**四种内置解析器（静态/浏览器 × 列表/播放页），依次「静态优先、浏览器兜底」。

新站点适配：

1. 若只需调整链接/标题规则 → 改 `app/rules/*.yaml`；
2. 若站点的页面结构特殊 → 在 `app/parsers/` 下新增一个解析器文件，继承对应基类并实现
   `parse` / `resolve`（`uses_browser=True` 的可实现 `*_with_page`），
   然后在 `Job._ensure_parsers()` 里登记它。

### 4.3 标题提取（`app/toolkit/title.py`）

**文件名以列表页标题为准**；只有列表页没拿到标题时，才用播放页自动识别的标题兜底。

播放页标题按 **og/twitter:title → meta[name=title] → 内容型 class →
h1 → JSON-LD name → `<title>` 拆段** 收集候选，再用「站名、页面套话、纯数字」过滤。

### 4.4 下载与合成

- `resolve_playlist` 最多下钻 3 层 master 列表，取 BANDWIDTH 最高档。
- **缺少 `#EXT-X-ENDLIST` 的播放列表默认拒绝**（直播/滚动窗口只下当前窗口会被当成完整
  视频永久跳过）；确需下载当前窗口时设 `YS_ALLOW_NO_ENDLIST=1`。
- **成品校验**：MP4 落盘后用 ffprobe（缺失时退回 `ffmpeg -i`）确认容器与时长，
  坏片删除并重试；`YS_VERIFY_MP4=0` 可关闭。
- **分片内容校验**：加密分片要求长度是 AES 块整数倍，明文分片要求首字节是 TS 同步字节，
  返回 HTML 错误页/空响应的分片直接判失败，不再落盘成“合法缓存”。
- `key_schedule` 按分片解析 `#EXT-X-KEY`（属性顺序无关、支持引号内逗号），支持：
  - **密钥轮换**（多个不同 URI 的 KEY，逐片切换）；
  - **中途转明文**（`METHOD=NONE`）；
  - 没有显式 IV 时按 RFC 8216 用**媒体序列号**派生每片 IV（不再全用 0 IV）。
- 分片用固定数量的下载协程消费下标（任务级共享线程池 + **全局 `seg_concurrency` 信号量**，
  所有在跑视频共享总并发，不再按 workers 整除放大），几千片也不会瞬间创建几千个 Task。
- 每片单独 AES-128-CBC 解密（各自 PKCS7），顺序写入 `.tmp.ts`；解密/合并失败会丢弃分片缓存，
  避免每次重试复放同一批坏片；合并流要求首尾都落在 188 字节包边界上。
- 分片缓存指纹 = 分片路径 + 密钥内容 + IV + **来源 host + `EXT-X-MEDIA-SEQUENCE` + ENDLIST**，
  换域名/换一轮直播后不会把两次内容拼成一条流。
- `ffmpeg -c copy -movflags +faststart` 转封装（30 分钟超时，**轮询停止信号，点停止 0.5 秒内杀进程**），
  失败保留 `.ts`。

### 4.5 容错、重试与域名变化

| 层级 | 次数 | 触发 | 行为 |
| --- | --- | --- | --- |
| 单请求 | 3（分片 4） | 网络/HTTP 异常 | 2/4/6s 退避 + 抖动，期间检查停止信号 |
| 单视频 | 3（域名重探成功 +1） | 可重试错误 | 重新解析播放页；连不上先域名重探 |
| 标题预解析 | 2 | 列表页没拿到标题 | 先解析播放页取标题再判断跳过 |
| 熔断 | - | 连续 5 个失败 | 中止任务（成功一次清零计数） |
| 列表页 | 2（失败后降并发） | WAF/限流/解析失败 | 记为 `failed_pages` + warning，继续翻其他页，**不再当成“空页”提前收工** |
| 停止 | 立即 | `/api/stop` | 置停止信号 + 杀 ffmpeg + 取消在跑协程；收尾标记「已停止」而不是 100% 完成 |
| 一键重试 | 手动 | 任务已结束/进程重启后 | 复位 `s=3` 与 `s=0` 重新派发；支持失败清单里**单条重试** |

- **不可重试**：`pipeline_rules.yaml` 里的 `non_retryable` 关键词（找不到 m3u8、404、磁盘满、
  ffmpeg 失败、不支持的加密方式等）直接判失败。
- **磁盘保护**：输出目录剩余 < 5GB 立即中止。
- **域名变化**：任务开始与运行中都会按「直连 → HTTP 30x 采用最终域名 → 沿 DoH CNAME 链换
  同站主机名 → 发布页/JS/meta 跳转用浏览器跟随」探测，切域名后 `base`/`Referer`/后续翻页自动生效。
- **发布页/弹窗换域名**：列表入口或播放页出现发布页时，会尝试点击
  `publish_click_texts`；同时监听当前页打开的新标签页/弹窗，从新页面 URL 或
  “最新地址/新域名: xxx”文字里识别 host，切换整个任务的 base，并把已入队的旧域名 URL
  重写到新域名后重试当前视频。
- **发布页只写一行新地址也能跟随**：`xxxx → yyyy（说明页）→ zzzz（新站）` 这种多跳链路，
  会同时探测**列表页本身和首页**（发布提示常挂在列表页上，首页可能已 404），最多跟 5 跳；
  说明页只有一行新地址、既没有“新域名”等关键词也没有链接时，会扫描正文里的域名候选
  （支持 `zzzz。com` / `zzzz点com` 这类写法），逐个用「能否打开列表页」验证后再采纳，
  同域/裸 IP/广告统计 CDN 域名一律过滤；旧列表页返回 200 但只是空壳说明页时也会触发。
- **列表路径修复**：换域名时只换 host 会保留旧路径，而站点经常连列表路由一起改版。
  拿到新域名后会先校验列表页，不可用时按可信度依次探测：
  1. **跟随跳转链**：`旧域名 → 跳转/说明页 → 页面上写的新网址`（最多 5 跳，
     HTTP 跳转、页面文字“最新网址/新域名: …”、正文裸域名、页面链接都认；
     JS 渲染的说明页会用浏览器再读一次）；
  2. 发布页/弹窗里给出的新站点链接（优先带原分类 id 的列表页）；
  3. 新域名首页自己的分类链接与列表路由前缀；
  4. `list_url_patterns` 常见路由（`/vodtype/{cat}-{page}.html`、
     `/vodshow/{cat}--------{page}---.html` 等）；
  5. https/http 互备。

  详情/播放路由不会被误当列表页；命中后替换 `list_url` 并把新模板加入候选，
  只有第 1 页会省略 `?page=1`（`/vodtype/144.html?page={}` → 第 1 页是 `/vodtype/144.html`）。
- **快速失败**：列表路径修不好时直接抛错结束任务（日志给出可照抄的候选格式），
  不再逐页解析、也不再对 404 开浏览器白等；只有 401/403/429/451/5xx 或连不上时才用浏览器重取页面。
- **HTTP 被挡时退回浏览器**：列表页 HTTP 请求失败（换域名后常见的 403/WAF）时，
  改用浏览器打开页面取 HTML 再解析，而不是直接当空页。
- **运行中域名失效**：分片连续失败（或本次下载有分片最终失败）时不等普通视频重试，
  立即强制重探一次域名；切换成功后 `domain.epoch` 递增，所有正在下载的视频会丢弃旧
  m3u8、重新解析播放页（`.parts` 缓存按指纹决定续传或清空）。
- **列表页并发**：按 `pipeline.list_concurrency`（默认 3）同时抓取/解析多页，但结果仍按页码
  顺序入队，视频编号与日志顺序不变；连续两页无新增时取消窗口内剩余请求提前收工。
- **列表页失败不再静默**：单页失败会退避重试一次，仍失败则记录页码、降低后续并发、写入
  `warnings` 与快照里的 `failed_pages`，并在任务结束时汇总提示补跑；只有“解析成功但内容为空”的页
  才会参与“已到分类末尾”判断（连续 3 页全重复内容才算末尾）。
- **URL 翻页无效时自动改点“下一页”**：有些站点忽略 `page=` 参数，或纯 JS 换内容但地址不变，
  第 2 页起会解析出与前面相同的内容或空页。检测到后自动改用浏览器：
  先点 `next_page_selector` / `next_page_texts` 命中的「下一页/加载更多」（含按文字/aria-label 兜底），
  没有翻页按钮时再尝试滚到底触发无限滚动/下拉加载；按逻辑页码继续采集，只触发一次，
  仍然拿不到新条目就按正常逻辑收尾。

### 4.6 翻页链接、增量、持久化与安全

- **翻页链接**：`{}`/`{p}`/`{page}`/`%d` > query 里的 `page=…` > 路径数字。
  单页任务（起始页=结束页）直接使用用户粘贴的地址，所以 `/vodtype/144.html` 不会被改写成
  `/vodtype/1.html`；多页任务但链接既没有占位符也没有 page 参数时，启动响应会带 `warning`
  提示改写成 `?page={}`，避免把分类 ID 当页码。
- **增量下载**：勾选「只下历史清单里没有的新视频」后，命中的条目直接跳过；成功/跳过都会写入
  `<outdir>/_seen_web.json`。配合 cron 每天跑一次即可当追更用。
- **任务持久化**：任务结束（完成/出错/停止）时写 `<outdir>/_job_state.json` 与
  `_logs/_last_job.txt`；服务重启后 `/api/job` 仍返回上次任务，`/api/retry_failed` 可直接重试。
  不会自动续跑（避免重启即抢占带宽）。
- **安全基线（可选，默认不改变原有行为）**：
  - `YS_TOKEN`：设置后除 `/api/health` 外所有 `/api/*` 需要 `X-API-Token` 头；
  - `YS_ALLOWED_ROOTS`：限制输出目录/查重扫描目录必须落在这些根目录内（`os.path.realpath` 校验）；
  - `YS_BLOCK_PRIVATE_MEDIA=1`：拒绝播放列表/密钥/分片指向私网、回环、链路本地地址（SSRF 防护）；
  - `YS_RATE_RPS`：按 host 限制每秒请求数（默认 0 不限速）；
  - TLS 策略统一：`YS_TLS_VERIFY=0` 同时作用于 httpx、DoH/CNAME 探测与 Playwright 上下文；
  - 查重「永久删除」必须先在 10 分钟内用同一策略成功执行一次 dry-run，且清理前校验文件
    大小/mtime/realpath，扫描后被替换的文件不会被删。

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 控制台页面（`Cache-Control: no-store`） |
| GET | `/api/health` | 健康检查（给 Docker healthcheck / 反代探活用） |
| GET | `/api/job` | 任务状态快照；无任务返回 `{"empty": true}` |
| GET | `/api/events` | 任务状态 SSE（状态未变化时不重复推送，15 秒心跳；断开自动退回轮询） |
| POST | `/api/start` | `{list_url,p1,p2,outdir,workers,seg_concurrency,only_new}` 启动任务；返回里带 `warning`（翻页链接可能不可靠时） |
| POST | `/api/stop` | 立即停止当前任务（杀 ffmpeg + 取消在跑协程） |
| POST | `/api/retry_failed` | 重试失败/未完成视频，返回 `count`；进程重启后可重试上次任务 |
| POST | `/api/retry_video` | `{i}` 只重试第 i 条（失败清单单条重试） |
| GET | `/api/dedup/status` | 查重进度、分组、可用性 |
| POST | `/api/dedup/start` | `{dir,percent}` 启动扫描（下载任务运行中会被拒绝） |
| POST | `/api/dedup/stop` | 停止扫描 |
| POST | `/api/dedup/apply` | `{strategy,mode}` 执行清理（下载任务运行中会被拒绝） |

> 下载与查重互斥：任务运行中不允许扫描/清理，扫描/清理进行中也不允许启动/重试下载，
> 避免 VDF 读到正在写入的 MP4、或清理删到正在下载的文件。

> 接口默认无鉴权；设置 `YS_TOKEN` 后除 `/api/health` 外都需要 `X-API-Token`。
> 仍建议只在本机/内网使用，或放在带认证的反向代理后面。

---

## 6. 配置

界面设置存浏览器 `localStorage`（`ys_cfg`）。服务端只有环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OUTDIR` | 仓库根 `downloads/` | 默认输出目录（容器里 `/downloads`） |
| `YS_HOST` | `0.0.0.0` | 监听地址（`python main.py` / 直接读 AppConfig 时生效） |
| `YS_PORT` | `8765` | 监听端口 |
| `YS_LOG_DIR` | `<OUTDIR>/_logs` | 运行期日志目录（`_server_dbg.log` / `_faulthandler.log`） |
| `YS_TLS_VERIFY` | 开 | 默认校验证书；设为 `0/false/no/off` 时关闭（同时作用于 httpx、DoH 探测与 Playwright 上下文） |
| `YS_TOKEN` | 空 | 设置后 `/api/*`（除 `/api/health`）需要 `X-API-Token` 头 |
| `YS_ALLOWED_ROOTS` | 空 | 可写/可扫描目录白名单（多个用系统路径分隔符），空表示不限制 |
| `YS_BLOCK_PRIVATE_MEDIA` | 关 | 设为 `1` 时拒绝播放列表指向私网/回环/链路本地地址（SSRF 防护） |
| `YS_RATE_RPS` | `0` | 每 host 每秒请求上限；0 表示不限速 |
| `YS_VERIFY_MP4` | 开 | 成品 ffprobe 校验；设为 `0/false/no/off` 关闭 |
| `YS_ALLOW_NO_ENDLIST` | 关 | 设为 `1` 才允许下载缺 `#EXT-X-ENDLIST` 的直播/滚动窗口 |
| `VDF_BIN` | 自动找 PATH | 查重可执行文件路径 |
| `VDF_SAMPLES` | `24` | 每视频采样帧数（1~200） |
| `VDF_AI` | 开 | 设为 `0/false/no/off` 关闭 AI 匹配两阶段 |
| `VDF_PARTIAL_MIN_RATIO` | `0.05` | 片段/源片最小时长比 |

`AppConfig.host/port` 默认读取 `YS_HOST` / `YS_PORT`，未设置时为 `0.0.0.0:8765`。

---

## 7. 运行与部署

本地（需 Python ≥ 3.11 与 ffmpeg）：

```bash
uv sync                      # 含 dev 依赖（pytest/ruff/mypy）
uv run uvicorn main:app --host 0.0.0.0 --port 8765 --reload
# 只装运行期依赖（生产）:
uv sync --no-dev
```

容器：

```bash
docker compose up -d --build
```

> Dockerfile 里安装 Playwright 浏览器的版本是硬编码的，必须与
> `pyproject.toml` / `uv.lock` 里的 `playwright` 版本保持一致；升级 `playwright`
> 时要同时改 Dockerfile 那一行，否则镜像里的浏览器与本机驱动版本不匹配。
> compose 已带 `/api/health` 健康检查和 30 秒停止宽限期。

测试：

```bash
uv run python -m unittest discover -s tests -v      # 或
uv run python -m pytest -q                          # pytest 配置在 pyproject.toml
```

- `test_pure.py`：m3u8/密钥计划/URL/编解码纯函数单测（不依赖网络）。
- `test_job_logic.py`：分片解密合并（含轮换密钥、逐片 IV）、一键重试进度、列表页并发有序性、异步日志。
- `test_modules.py`：拆分后的 JobState / DomainProbe / VideoWorker / 列表 API 兜底、
  HLS 缓存指纹与 TS 校验、分片失败触发域名恢复、epoch 中断在跑下载、发布页新域名识别
  与 URL 重写、换域名后列表路径修复、TLS 默认值边界测试；另含列表页失败不再当空页、
  HLS 完整性（缺 ENDLIST 拒绝/分片内容校验/换域名缓存失效）、查重 dry-run 门禁与
  文件变化过滤、任务状态 dump/restore、单条重试、目录白名单等回归测试；
  以及 URL 翻页无效时「点下一页/无限滚动」的兜底（含起始页 >1 只点不收集）用例，
  还有 `xxxx→yyyy→zzzz` 多跳、正文裸域名单行说明页、200 空壳列表页的换域名链路用例。
- `test_domain_popup.py`：用假页面对象验证“播放页弹窗/新标签页提示新域名”的识别，
  以及发布页正文裸域名候选的提取（全角点/“点”写法、第三方域名过滤）。
- `test_dedup.py`：VDF 结果解析、保留项标记、文件名匹配、扫描视频枚举。
- `test_hls_integration.py`：现场用 ffmpeg 生成 AES-128 加密 HLS，起本地 HTTP 服务走完整
  `_hls_to_mp4` 链路；缺 ffmpeg/依赖时自动跳过。
- `test_pipeline_integration.py`：本地列表页 + 播放页 + 加密 HLS，跑完整 `Job.run()`
  （列表解析 → worker → 下载解密 → ffmpeg → 产出 MP4）。

### 代码质量

`pyproject.toml` 里已经配好 `[tool.ruff]` / `[tool.pytest.ini_options]` / `[tool.mypy]`，
并把 pytest/ruff/mypy 放进 `[dependency-groups] dev`（`uv sync` 默认安装；具体版本由
`uv.lock` 锁定，当前 ruff 0.17.5 / mypy 2.4.1 / pytest 9.2.0）：

```bash
uv run ruff check .            # 当前零告警
uv run ruff format --check .   # 当前全部已格式化
uv run mypy                    # 当前零错误
uv run python -m pytest -q     # 94 个测试全部通过
```

`.github/workflows/ci.yml` 会在 push/PR 时跑上面四项；
升级 ruff/mypy 后若新增告警，需要同步修代码或调整规则，别让 README 的“零告警”漂移。

---

## 8. 重构与优化要点

1. **规则单一来源**：散落在 `parser_*.py` / `job.py` / `utils.py` 里的正则、关键词、阈值全部
   收敛到 `app/rules/*.yaml`，由 `RuleManager` 统一编译；解析器不再硬编码规则。
2. **解析器显式组装**：`Job._ensure_parsers()` 直接实例化四种内置解析器，新站点在 `app/parsers/`
   新增解析器文件并在此登记即可。
3. **依赖倒置**：`main.py` 只依赖 `app.services` 与 `app.core`，不再 import 旧模块。
4. **完整流水线**：新 `Job` 补齐浏览器兜底、域名探测/重探、一键重试、熔断、磁盘检查、日志落盘。
5. **前端契约不变**：`JobSnapshot` 的字段与 `index.html` 约定完全对齐，控制台无需改动即可工作。
6. **旧代码清理**：删除 `app/job.py` / `parser_*.py` / `state.py` / `dns.py` / `dedup.py` /
   `utils.py` / `hls.py` / `config.py` 与 `main_legacy.py`。
7. **性能**：列表页并发抓取（有序入队）、加密 SPA API 捕获仅作兜底、浏览器上下文整任务复用、
   分片用固定协程池消费、静态列表解析放线程、日志落盘/打屏移到后台线程、VDF 进度行用文件名索引匹配。
8. **健壮性**：HLS 密钥轮换/逐片 IV、分片缓存指纹（密钥/播放列表变化自动失效）、
   解密后 TS 结构校验（密钥/IV 错误提前失败）、发布页/弹窗新域名识别与 URL 重写、
   分片失败立即强制重探域名并以 epoch 让所有在跑视频重新解析、
   一键重试进度按视频状态重算、同时跟踪所有 ffmpeg 子进程（停止时全部回收）、
   `/api/dedup/apply` 不再阻塞事件循环、修复 Windows 孤儿 chromium 清理脚本的 PowerShell 花括号错误。
9. **前端**：优先使用 SSE 推送任务状态（失败自动退回自适应轮询）、日志按 `log_seq` 增量追加
   （不再在 400 行后冻结）、日志 DOM 只保留最近 800 行、查重面板后台不轮询、补充可访问性、
   移动端与 reduced-motion 样式。
10. **职责拆分**：`job.py` 从 1042 行降到约 340 行，只保留任务编排；状态、列表流水线、
    单视频 worker、HLS、浏览器、域名各自独立成模块，`Job` 对外接口保持不变。
11. **停止即时生效**：`/api/stop` 现在会杀 ffmpeg、取消在跑协程，并在收尾时把状态标记为
    「已停止」而不是 100% 完成；ffmpeg 改为 0.5 秒轮询停止信号，浏览器等待也逐段检查。
12. **结果可信**：列表页失败不再被当成空页（记 `failed_pages` + warning、降并发、可补跑）；
    缺 ENDLIST 的直播流默认拒绝；成品用 ffprobe 校验；解密/合并失败会丢弃分片缓存。
13. **断点续传安全**：分片指纹加入 host / 媒体序列号 / ENDLIST；分片落盘前校验内容形态；
    换域名或换一轮直播后不会复用旧分片。
14. **清理安全**：查重永久删除有 dry-run 门禁，删除前校验文件 realpath/大小/mtime；
    apply 纳入 busy 互斥，结果文件原子回写。
15. **安全基线**：可选 Token 鉴权、目录白名单、私网地址拦截、按 host 限速；TLS 策略统一到
    `YS_TLS_VERIFY`，DoH 改用可校验证书的域名端点；播放页提示的新域名必须能打开列表页才采纳。
16. **任务可恢复 + 增量**：任务结束落盘 `_job_state.json`，重启后仍能展示与重试；
    `_seen_web.json` + 「增量下载」支持 cron 追更。
17. **工程化**：CI（ruff check / ruff format --check / mypy / pytest）、`ruff format`、
    dev 版本锁定；compose 生产启动去掉 `--reload`。
18. **URL 翻页无效兜底**：检测到 page 参数被忽略/纯 JS 换内容（第 2 页内容重复或为空）时，
    自动切到浏览器点「下一页/加载更多」，没有按钮再尝试无限滚动，避免只下到第 1 页。
19. **换域名链路增强**：同时探测列表页与首页；发布页只写一行裸域名（含全角点/“点”写法）
    时逐个用列表页验证后采纳；旧列表页返回 200 空壳说明页也能触发跟随；跨域目录链接
    不再被误当成“新站点”。

## 9. 已知限制

- **fMP4（`#EXT-X-MAP` / `.m4s`）不支持**：现在会直接报「fMP4(EXT-X-MAP) 播放列表暂不支持」，
  不会再把几百个分片下完才在 ffmpeg 阶段报合成失败。
- 分片按 TS 处理，`EXT-X-BYTERANGE` 不支持；SAMPLE-AES 不支持（会明确报错）。
- **缺 `#EXT-X-ENDLIST` 的直播/滚动窗口默认拒绝**（可用 `YS_ALLOW_NO_ENDLIST=1` 放行当前窗口）。
- 任务状态只在**任务结束/重试结束时**落盘；进程被强杀（SIGKILL/断电）会丢失本轮进度，
  但 `_failed_web.txt` 与输出目录里的成品仍在。
- 查重依赖外部 `vdf` CLI；Windows 下需自行放置可执行文件。
- 查重永久删除必须先 dry-run（10 分钟内有效）；扫描后被替换/改名的文件会被跳过。
- 大列表全部保留在内存中，前端最多渲染 300 行；单任务仍只允许一个（多任务队列未实现）。
- 翻页链接没有 `{}`/`page=` 时按“路径最后一个数字是页码”推断，多页任务会有 `warning` 提示；
  站点用分类 ID 作为路径数字时请显式写 `?page={}`。
- URL 翻页无效（page 参数被忽略/纯 JS 换内容）时会尝试浏览器点「下一页」或无限滚动兜底；
  如果站点用的是自定义翻页控件/纯手势翻页，需要把选择器或按钮文字加进
  `pipeline_rules.yaml` 的 `next_page_selector` / `next_page_texts`，否则兜底可能点不到。
  无限滚动每页最多滚 4 次、每次等 1.2 秒，懒加载很慢的站点仍可能漏条目。
- 换域名弹窗识别依赖页面出现“新域名/最新地址/发布页”等提示，或弹窗带 `opener`
  可关联到当前页；完全无提示的广告式跳转、`noopener` 弹窗不一定能识别。
  播放页提示的新域名会先用列表页校验，打不开的不采纳。
- 列表路径修复只覆盖 `list_url_patterns` 里的常见路由；站点用了很特殊的路径时
  需要把模板加进 YAML，或手工改任务里的列表链接。
- 运行中强制重探仍只探测列表 URL / 候选域名；旧域名仍可访问、页面也没有任何换域名提示时，
  程序无法自动发现新站，需要把新域名填进候选链接或重新运行任务。
- 发布页把新地址做成**图片/视频/Canvas** 时无法识别（没有 OCR），需要在页面文字里出现域名
  或链接，或手动把新域名填进候选链接。
- `.tmp.ts.parts/` 分片缓存有指纹校验但不会自动过期；长期运行后仍建议手工清理。
- `YS_BLOCK_PRIVATE_MEDIA` 默认关闭（内网/NAS 站点常是私网地址），需要面对不可信站点时再开。