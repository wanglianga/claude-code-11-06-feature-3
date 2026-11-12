# CarScout · 二手车多平台采集与归并服务

常驻运行的采集服务：在四类结构完全不同的二手车平台上按平台维护抓取范围、频次、
分页方式与增量游标，把列表页与详情页拆成两次请求抽取字段，对异常响应分类、退避、
熔断并留存原始 HTML，最后把同一台车在多个平台的重复挂牌归并成一个车源，
对外提供近 30 天按车系的挂牌与报价分布导出，以及 CSV 形式的人工确认回路。

交付物**只有后端服务与命令行入口**，没有任何管理界面。人工确认通过
「导出 CSV → 修改 `decision` 列 → 回传接口」完成，确认结果自动沉淀为归并规则。

---

## 1. 目录结构

```
carscout/
  cli.py              命令行入口（serve / scheduler / run / merge / review / export / status …）
  scheduler.py        常驻调度：按平台各自频次触发，单平台异常不影响其它平台
  config.py           全部配置项（环境变量驱动，见 .env.example）
  parsers.py          四个平台的字段解析与异常分类
  store.py            挂牌落库：增量、价格变动、图片哈希
  matching.py         跨平台归并：VIN 直判 → 特征召回 → 打分 → 灰区 → 规则复用（参数全部来自规则版本，输出证据级解释）
  rules.py            归并规则版本生命周期：版本 CRUD、离线回放审批、按平台灰度放量、回滚（不自动拆回）
  ingest.py           原始响应留档、失败分类、平台健康与熔断
  images.py           远端图片抓取 + pHash
  crawl/
    engine.py         每个平台一个 Scrapy spider，列表/详情状态机、游标、重试、熔断
    handlers.py       四个平台的传输差异（静态 HTML / 滚动 XHR / 反爬 / 签名 App 接口）
    runner.py         平台级进程隔离、并发上限、跑完统一归并
    worker.py         单平台 worker 进程入口
    settings.py       Scrapy 与 Playwright 配置
  api/
    app.py            HTTP API（OpenAPI 3 + Swagger UI 自动生成）
    export.py         导出、单车档案、人工确认 CSV 读写
  fixture/
    dataset.py        伪造的四平台样本数据（12 台车 + 灰区样本 + 已下架样本）
    server.py         伪造平台服务（含反爬、签名、改版、限流开关）
  db/schema.sql       全部表结构（幂等，可重复执行）
tests/                单元 + 端到端测试（含 SIGKILL 续采、看门狗）
scripts/              demo.sh（联调演示）、verify.sh（一键跑全部验证）
Dockerfile / docker-compose.yml
```

---

## 2. 一条命令启动

```bash
docker compose up -d --build        # postgres + redis + fixture + api + scheduler
curl -s localhost:8000/healthz
# {"ok":true,"dependencies":{"postgres":true,"redis":true},"version":"1.0.0"}
```

首次启动后立刻跑一轮完整采集（四个平台并发、进程隔离、跑完自动归并）：

```bash
docker compose --profile tools run --rm crawl
```

| 服务 | 端口 | 说明 |
|---|---|---|
| `api` | 8000 | HTTP API + Swagger UI（`/docs`、`/openapi.json`） |
| `scheduler` | — | 常驻调度：A 每 30 分钟、B 45、C 60、D 15 分钟各跑一轮 |
| `fixture` | 8088 | 伪造的四平台（联调用，生产可指向真实平台） |
| `postgres` | 5432 | 车源历史、归并关系、人工确认、游标 |
| `redis` | 6379 | 请求指纹（URL 归一化 + 关键字段哈希），用于增量判重 |

**测试账号 / 凭证**：本服务无登录与用户体系（对外只有 API 与 CLI），因此没有
账号密码。数据库与 Redis 使用 compose 内置的本地账号 `carscout / carscout`；
伪造平台 D 的签名密钥是 `carscout-demo-secret-v1`（可在 `.env` 里改
`FIXTURE_SECRET`）。人工确认不需要登录，`reviewer` 字段由调用方传入（默认
`operator`），写在 `match_rules.source` 与 `manual_review_queue.reviewer` 里可审计。

停止并清空：`docker compose down -v`（会一并清掉归档卷）。

---

## 3. 本地开发（不用 Docker）

依赖：Python 3.11、PostgreSQL 15、Redis 7、Playwright 的 Chromium。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
playwright install chromium           # 平台 B / D 需要浏览器上下文

export CARSCOUT_DATABASE_URL=postgresql://carscout:carscout@127.0.0.1:5432/carscout
export REDIS_URL=redis://127.0.0.1:6379/0
export FIXTURE_BASE_URL=http://127.0.0.1:8088
export DATA_DIR=./data

carscout init-db                      # 建表 + 初始化平台
carscout fixture --port 8088 &        # 伪造四平台
carscout run --date 2026-11-12        # 采集 + 归并
carscout serve                        # API: curl localhost:8000/healthz
```

常用命令：

```bash
carscout run -p A -p B                       # 只跑指定平台（并发、进程隔离）
carscout run --date 2026-11-12 --mode rerun  # 指定日期重跑（强制重新抽详情）
carscout run --resume                        # 断点续采：从最后一个完成页继续
carscout merge --rebuild                     # 清空归并结果并从零重聚类
carscout status                              # 各平台游标、熔断状态、近 7 天异常
carscout export --series 凯美瑞 --days 30     # 报价分布（json）
carscout export --series 凯美瑞 --format csv  # 报价分布（csv）
carscout review --export --out review.csv     # 导出灰区候选
carscout review --import-file review.csv      # 回传确认结果并沉淀规则
carscout archive-grep "行驶里程" -p A --date 2026-11-12   # 离线复盘归档原文
carscout rules list                           # 规则版本与放量比例（完整子命令见 §13.4）
carscout rules replay --version-id 2          # 新规则历史存量离线回放
carscout rules explain --a 12 --b 18          # 两条车源的证据级判定解释
```

---

## 4. 每个平台怎么抓

| 平台 | 传输特点 | 采集方式 | 增量/分页 |
|---|---|---|---|
| A | 静态 HTML 列表 + 分页 | Scrapy HTTP，`?page=` 翻页 | `page`/`data-total`；到最后一页停 |
| B | 必须滚动才加载，详情走 XHR | Playwright 页面内调用站点的滚动处理器，捕获 `/pb/api/list` 的 XHR；详情用**同一个浏览器上下文**里的 `fetch` | `hasMore` 游标，逐页滚动 |
| C | 反爬：连续请求返回 200 但列表为空 | Scrapy HTTP + 限速（并发 1、延迟 1.2s） | 结构校验见下节 |
| D | 只提供 App 接口，参数带 HMAC 签名、几分钟过期 | Playwright 打开 App shell，**在页面上下文里**调用接口（复用会话 Cookie 与签名密钥）；403 时用新时间戳重签重试 | `hasMore` 翻页 |

要点：

- **列表与详情始终是两次请求**：列表页只落轻量字段（标题/价格/城市/里程/链接），
  详情页单独请求并抽取品牌车系、上牌年月、表显里程、排量、变速箱、过户次数、
  所在城市、挂牌价、挂牌时间、卖家类型、车况描述、车架号。
- **请求指纹**：`URL 归一化（去 tracking/签名/时间参数、排序 query）+ 关键字段哈希`
  落 Redis（默认 45 天 TTL）。列表指纹未变且详情上次成功 → 本次跳过详情请求
  （`details_skipped_unchanged` 计数可见）；`--mode rerun` 强制重抽。
- **速率控制**：每平台的抓取范围、频次、分页方式、并发与延迟都写在
  `platforms.config`（数据库行，启动时由 `seed_platforms` 写入，可直接 UPDATE
  调整而无需改代码）；调度器读 `interval_minutes`，spider 读 `rate.concurrent` /
  `rate.delay`。Scrapy AutoThrottle 与 `DOWNLOAD_DELAY` 作为兜底，**平台并发上限**
  由 `MAX_PARALLEL_PLATFORMS` 控制。

---

## 5. 「真没车源」 vs 「被拦截」怎么分

页面/接口返回都先分类，再决定是推进游标还是退避重试：

| 分类 | 判定 | 处理 |
|---|---|---|
| `ok` | 结构完整、有行 | 落库、推进游标 |
| `empty_confirmed` | 200，容器与页脚/总数标记齐全，确实 0 条 | **正常结束**，推进游标 |
| `empty_unverified` | 200，但容器缺页脚/总数标记，或总数>0 却 0 行（典型反爬） | 指数退避重试；计入平台失败 |
| `blocked_signal` | 403/429、验证码页关键词、会话缺失 | 退避重试；计入平台失败 |
| `schema_drift` | 容器在，但详情必需字段缺失/被改名（改版） | 退避重试；**不计入反爬失败**，连续多次才停该平台 |
| `signature_expired` | D 平台签名过期/失效 | 重新签名重试 |
| `http_error` / `parser_error` | 传输错误 / 解析异常 | 退避重试；计入平台失败 |

- 连续失败达到 `FAILURE_THRESHOLD`（默认 5）→ **熔断该平台**：`platform_health`
  置 `open` + `cooldown_until`，本轮只停这一个平台，**已采数据保留**，其它平台的
  进程完全不受影响；冷却后半开探测。
- **进程级看门狗**：每个平台跑在独立 OS 进程里。若某平台进程
  `PLATFORM_STALL_TIMEOUT_SECONDS`（默认 300 秒）内没有任何新输出（例如浏览器
  启动卡死），外层 runner 会杀掉该 worker、把运行记为 `aborted` 并继续等其它平台，
  整轮不会被一个平台拖死。
- 每次列表/详情响应（含异常响应体）都写盘留档：
  `data/archive/<日期>/<平台>/<run>/`，含 `.gz` 原文 + `.meta.json`（URL、状态码、
  分类、SHA256），并记 `ingest_archive` / `ingest_anomalies`，可 `archive-grep` 离线复盘。
  失败原因分类可见 `/api/anomalies`。

---

## 6. 断点续采（不从头重扫）

- `crawl_cursors` 保存 `last_completed_page`（整页详情都处理完才推进）与
  `run_date`；`crawl_runs` 保存每次运行的统计与错误。
- 进程被杀（含 SIGKILL）只丢失在途那一页：`carscout run --resume`
  从 `last_completed_page + 1` 继续；Redis 指纹让已入库的详情不再重复请求。
- 单轮页数上限 `MAX_PAGES_PER_RUN` 触顶时，本轮标记未完成（游标不推进），
  下一轮/续采自然接着翻页。
- 只有**完整跑完一轮**（且从第 1 页起）才做「消失车源」软下架，避免半轮误判。

---

## 7. 跨平台归并（本节是重点）

同一台车在不同平台没有统一 ID，流程是：

1. **平台内去重**：DB 唯一键 `(platform, platform_listing_id)`，任何重复请求
   只会命中同一行。
2. **VIN 硬判**：车架号后六位（或全 VIN）一致 → 直接认定同一车源，得分 1.0。
3. **特征召回**：品牌车系（系列不同直接否决）+ 上牌年月（默认 ±9 个月窗口）
   + 里程带（默认 ±9,000 km）+ 城市，从已有车源里召回候选。
4. **打分**（0–1，权重写在 `matching.score_pair`）：
   - 车系相似度 0.34 / 上牌年月 0.16 / 里程 0.14 / 城市 0.08 /
     变速箱 0.04 / 排量 0.03 / 过户次数 0.03
   - 图片感知哈希 0.30（Hamming≤3 记 0.97，≥18 记 0.05）
   - 车况描述文本 0.26（字符 bigram Jaccard + difflib 混合）
   - **证据闸门**：图片距离 ≥12，或描述相似度 <0.25 时，分数被压到
     `GRAY_ZONE_SCORE + 0.17`（默认 0.77，落在灰区内），防止「结构化字段刚好
     一致、但明显是另一台车」被自动合并。
5. **判定**：
   - `≥ AUTO_MERGE_SCORE`（默认 0.82）→ 自动合并
   - `GRAY_ZONE_SCORE ≤ x < AUTO_MERGE_SCORE`（默认 0.60–0.82）→ **人工确认队列**
     （新挂牌不并入对方，而是落成独立「待确认」车源，`/api/vehicles` 用
     `provisional` 标出；候选记录里同时保留对方 listing id，供人工对照）
   - `< 0.60` → 各自独立车源；候选记录留档备查
6. **人工确认 → 规则复用**：CSV 回传 `merge`/`split` 后，写入 `match_rules`
   （键为车系对），下一批次遇到同键组合直接按规则处理并累加 `applied_count`。

**归并结果保留**：每个平台原始挂牌记录、原始链接、每次抓取时间、
`listing_price_events` 里的历次价格（含 `previous_price`/`delta_yuan`）、
首次挂牌时间。单车档案 `GET /api/vehicles/{id}` 直接回答：
「在几个平台挂过、各挂多少钱、降过几次价、首次挂牌距今多久」。

> 上面打分用到的**全部阈值、字段权重、证据闸门、必要条件都不再写死在代码里**，
> 而是作为带版本号的规则（`merge_rule_versions`）管理，支持离线回放审批、
> 按平台灰度放量、随时回滚与证据级解释，完整机制见 **§13**。

---

## 8. 对外接口（Swagger UI: `http://localhost:8000/docs`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 依赖探测 |
| GET | `/api/overview` | 平台游标、熔断、近 7 天异常、临时车源/待确认数 |
| GET | `/api/export/series?series=凯美瑞&days=30` | **近 30 天各平台挂牌与报价分布**（JSON；中文参数需 URL 编码） |
| GET | `/api/export/series/csv` | 同上，CSV |
| GET | `/api/vehicles?series=` | 归并后的车源列表（含 `provisional` 标记） |
| GET | `/api/vehicles/{id}` | 单车档案（跨平台、价格变动、挂牌时长） |
| GET | `/api/vehicles/{id}/dossier.csv` | 单车档案 CSV |
| GET | `/api/review/queue` | 灰区队列预览 |
| GET | `/api/review/export.csv` | 导出人工确认 CSV |
| POST | `/api/review/import` | 回传 CSV（JSON body: `{"csv": "...", "reviewer": "..."}`） |
| POST | `/api/review/import-file` | 回传 CSV（multipart 文件） |
| POST | `/api/review/{candidate_id}/confirm` | 单条确认 |
| GET | `/api/rules` | 已沉淀规则 |
| GET | `/api/rule-versions/default-spec` | 规则 spec 模板（v1 基线参数） |
| GET/POST | `/api/rule-versions` | 规则版本列表 / 创建版本草稿 |
| GET | `/api/rule-versions/{id}` | 单版本详情（spec + 放量比例） |
| POST | `/api/rule-versions/{id}/replay` | 历史存量离线回放与影响预估（只读） |
| GET | `/api/replays` `/api/replays/{id}` | 回放记录与明细样本 |
| POST | `/api/replays/{id}/decision` | 超门槛回放的人工审批（approve/reject） |
| POST | `/api/rule-versions/{id}/activate` | 激活版本（未回放/未审批会被拒绝） |
| POST | `/api/rule-versions/{id}/rollout` | 按平台灰度放量（`{"A":10,"B":0}`） |
| POST | `/api/rule-versions/{id}/rollback` | 回滚：记录转待复核，不自动拆回 |
| GET | `/api/pending-rereview` | 回滚后待复核挂牌（保留原归并关系） |
| GET | `/api/explain?listing_a=&listing_b=` | **证据级解释**：命中证据、权重、判定依据 |
| POST | `/api/runs` | **按平台 / 指定日期重跑**（`{"platforms":["A"],"day":"2026-11-12","mode":"rerun"}`，`wait=true` 同步返回汇总） |
| GET | `/api/runs` `/api/anomalies` `/api/archive` | 运行轨迹、失败分类、留档索引 |

人工确认 CSV 形态（`decision` 列填 `merge` 或 `split`，`note` 可备注）：

```
candidate_id,listing_a,listing_b,score,city_a,city_b,mileage_a,mileage_b,reg_a,reg_b,price_a,price_b,phash_distance,text_similarity,decision,note
23,D:pd-4003,B:pb-2013,0.7528,苏州,苏州,72000,78500,2018-09,2018-09,159000,155000,14.0,0.0402,,
```

---

## 9. 数据模型（PostgreSQL）

`platforms` / `crawl_runs` / `crawl_cursors` / `platform_health` /
`vehicles` / `listings` / `listing_price_events` / `image_hashes`（独立表，按
`(listing_id, image_url)` 唯一，支持相似度检索）/ `match_candidates`（含
`rule_version_id` 与证据级 `evidence`）/ `manual_review_queue` / `match_rules` /
`merge_rule_versions` / `merge_rule_rollouts` / `merge_rule_replays`（规则版本化
三件套，见 §13.5）/ `ingest_archive` / `ingest_anomalies`。
`db/schema.sql` 幂等，服务启动时自动执行并补齐列与基线规则版本。

---

## 10. 伪造样本（联调数据）

`carscout.fixture` 生成 **12 台真实的「车」**，在 4 个平台上共 36 条挂牌
（A 10 条 / B 9 条 / C 9 条 / D 8 条，含 1 条已下架）：

- 4 平台重复挂牌的车（宝马 3 系 2018、比亚迪 汉）：**VIN 后六位一致 → 直判合并**
- 3 平台重复的车：宝马 3 系 2019、奥迪 A4L、丰田凯美瑞、奥迪 Q5L
  （部分平台公开 VIN 尾号，未公开的走特征召回打分）
- **灰区样本（不得自动合并）**：
  `pb-2013`（A4L，里程被改、第二组照片）；`pc-3013`（凯美瑞，里程 4.4 万、异地挂牌、
  第二组照片）；`pa-1014`（C 级，上牌年月差两个月、另一组照片）
- 已下架样本：`pa-1015`（雅阁，50 天前挂牌、9 天前下架）——用于 30 天窗口与软下架
- 价格历史：多台车带降价记录（如 `pa-1001` 19.90 → 19.20 → 18.80 万）
- 标题/里程/上牌/描述写法每平台不同；图片按车辆重新压缩（pHash 仍近似）

伪造服务可在线切换故障，用于验证分类与熔断（`POST /_fixture/control`）：

```bash
# 让 C 平台对所有请求返回 200 + 空列表（模拟被拦截）
curl -s -X POST localhost:8088/_fixture/control -H 'content-type: application/json' \
     -d '{"c_force_blocked": true}'
# 让 C 平台真实无车源（应判定 empty_confirmed 并正常推进游标）
curl -s -X POST localhost:8088/_fixture/control -d '{"c_force_empty": true}'
# 模拟 A 平台改版（字段改名/丢失）
curl -s -X POST localhost:8088/_fixture/control -d '{"drift": {"A": true}}'
curl -s -X POST localhost:8088/_fixture/reset     # 全部复原
```

---

## 11. 验证方式（以下均为实际执行过的验证）

### 11.1 自动化测试

```bash
# 需要 PostgreSQL / Redis；Playwright 需要 Chromium
export CARSCOUT_DATABASE_URL=postgresql://carscout:carscout@127.0.0.1:5432/carscout
export PLAYWRIGHT_BROWSERS_PATH=$HOME/.cache/ms-playwright   # 冒烟端到端必需

bash scripts/verify.sh                             # 全部（约 14 分钟，Playwright 端到端为主）
# 或分开跑：
pytest tests -q --ignore=tests/test_e2e_crawl.py \
                  --ignore=tests/test_e2e_review.py --ignore=tests/test_e2e_crash.py   # 仅单元/接口（约 20 秒）
```

已执行结果（本批次在隔离工作区实测）：**全量 76 项全部通过**。

- 快速套件 59 项（约 20 秒；含本特性新增 `test_rule_versions.py` 13 项：
  规则校验、必要证据降级、回放预估与审批拦截、灰度分桶、回滚不拆回、
  rebuild 版本戳、证据解释、HTTP 全生命周期）；
- 端到端 `test_e2e_review.py` 3 项（130.57s）、
  `test_e2e_crawl.py` + `test_e2e_crash.py` 14 项（510.91s，真实
  Scrapy + Playwright + PG + Redis 四平台链路）。

> 本机环境适配（如实说明）：开发机系统库自带的 PostgreSQL 无 `carscout`
> 角色且不可登录，本次在用户目录用 `initdb` 起了一个等价的 PostgreSQL 15
> 集群（端口 5433、trust 认证），通过 `CARSCOUT_DATABASE_URL` 指向它执行，
> 应用代码与 SQL 完全一致；Python 依赖装在项目内 `.venv`；Playwright
> Chromium 缺失的 12 个系统库以用户态 deb 解包 + `LD_LIBRARY_PATH` 补齐
> （Docker 镜像内由 Dockerfile 的 apt 清单直接提供，无需此处理）。

规则版本化相关新增断言：

| 用例 | 断言 |
|---|---|
| `test_baseline_version_seeded_with_legacy_parameters` | v1 自动种子；同一对挂牌走旧兜底与走 v1 RuleSpec 的总分/分项完全一致 |
| `test_create_version_validates_thresholds_and_required_evidence` | 阈值倒挂、非法必要证据、缺权重的 spec 被拒绝 |
| `test_required_vin_evidence_downgrades_auto_merge_to_grey_zone` | 配 `vin_match` 必要条件后，无 VIN 的高分配对从自动合并降为灰区；双方 VIN 一致时照常合并 |
| `test_required_image_evidence_blocks_when_photo_disagrees` | 图片不一致且被列为必要证据时 `required_block=image_agreement`，进人工队列 |
| `test_replay_estimates_impact_and_blocks_activation_until_approved` | 回放给出新增合并/拆散人工确认数；超门槛未审批（含审批被拒）禁止激活，审批通过后放行 |
| `test_replay_within_threshold_activates_without_approval` | 影响在门槛内可直接激活 |
| `test_simulate_clustering_matches_live_partition` | 离线模拟聚类与线上真实分区一致 |
| `test_rollout_percentage_uses_stable_hash_buckets` | 分桶稳定可复现、比例生效、平台精确配置优先于 `*` 通配 |
| `test_rollback_flags_listings_pending_rereview_without_splitting` | 回滚后记录转 `pending_rereview`、保留 `vehicle_id`、车源数不变、自动归并不再触碰、进入复核队列且可人工 split |
| `test_rebuild_restamps_listings_using_active_version_rollout` | `merge --rebuild` 按 active 版本重新打版本戳并保留证据 |
| `test_explain_pair_reports_every_evidence_with_weight` | explain 输出每条证据的命中/权重/贡献/细节，并能反映必要证据缺失 |
| `test_full_rule_lifecycle_via_http` | API 走完建版本→回放→审批→激活→放量→解释→回滚全链路 |

| 用例 | 断言 |
|---|---|
| `test_each_platform_crawls_its_own_transport` | A/B/C/D 各自 9/9/9/8 条全部 `detail_status=ok`，游标 `completed=true`，列表与详情留档分开 |
| `test_idempotent_incremental_second_run_skips_details` | 第二轮 `details_skipped_unchanged >= 9`，行数不变（增量判重生效） |
| `test_rerun_mode_forces_detail_refetch` | `--mode rerun` 跳过数为 0，全部重抽 |
| `test_platform_c_blocked_uses_backoff_then_circuit_breaker` | 200+空列表被记 `empty_unverified`，退避后熔断（`circuit_state=open`），游标未完成，新任务被拒；同时 A 平台照常 `finished` |
| `test_platform_c_legit_empty_is_completed_not_blocked` | 真·无车源判 `empty_confirmed`，游标完成、熔断保持关闭 |
| `test_schema_drift_is_classified_and_does_not_trip_breaker` | 改版被判 `schema_drift` 且不误触反爬熔断 |
| `test_signed_api_expiry_is_retried` | D 平台签名过期后重签成功，8 条详情齐全 |
| `test_resume_continues_from_last_page_not_from_scratch` | 首轮只跑第 1 页（游标 `completed=false`），续采 `start_page=2` 并完成 |
| `test_sigkill_resumes_from_committed_page` | 第 1 页提交后 `kill -9` worker：游标保留 `last_completed_page=1` 且 `completed=false`；续采从第 2 页继续，无重复挂牌 |
| `test_watchdog_kills_a_stalled_platform_and_keeps_others` | 平台进程长时间无输出时被看门狗杀掉，运行记为 `aborted`，其余平台不受影响 |
| `test_full_run_merges_cross_platform_duplicates` | 12 台真车聚成 12 个车源；两台 4 平台车正确合并；灰区样本未被自动合并 |
| `test_price_history_records_cuts` | 改价 + 重跑后记录 `previous_price` 与负 `delta_yuan` |
| `test_review_csv_roundtrip_merges_and_learns_rule` | CSV 回传 `merge` → 合并生效、`match_rules` 落库、队列转 `confirmed_merge` |
| `test_learned_rule_applies_to_the_next_batch` | `split` 决策沉淀为规则 |
| `test_api_surface` | 概览/分车系分布/车源列表/单车档案/灰区队列/CSV/规则/异常/运行轨迹全部可用 |

### 11.2 手工验证（本机实际执行）

```bash
carscout init-db
carscout fixture --port 8088 &
carscout run -p A --date 2026-10-20 --no-merge    # 先采一轮历史快照，制造价格变动
carscout run --date 2026-11-12
# A finished 9/9 · B finished 9/9 · C finished 9/9 · D finished 8/8（items_seen/detail_ok）
# merge: {"created_vehicle": 12, "merged": 20, "queued_for_review": 3}

curl -s "localhost:8000/api/export/series?series=凯美瑞&days=30" | jq
curl -s localhost:8000/api/vehicles/1 | jq '.platform_count, .price_min, .price_max, .days_since_first_listing'
curl -s "localhost:8000/api/review/export.csv" > review.csv
# 修改 review.csv 中某一行的 decision 为 merge，然后：
curl -s -X POST localhost:8000/api/review/import-file \
     -F file=@review.csv -F reviewer=qa
```

实测导出（`series=凯美瑞`，30 天窗口）按平台给出 A 13.8 万、B 14.0 万、
C 13.5 万（灰区未并入）、D 13.9 万。
实测价格变动：先用 `--date 2026-10-20` 采一轮，再用 `--date 2026-11-12` 采一轮，
`pa-1001` 记录 `previous_price=199000 → 188000`（delta=-11000）；宝马 3 系 2018 的
单车档案返回「4 个平台 / 各挂 18.5–18.9 万 / 首次挂牌 45 天前 / 各平台挂牌 32–45 天」。
（上表概述里写的 `merged: 20 / queued_for_review: 3` 是同日单轮采集的结果；
两轮采集后 A 平台多出 1 条当日新上架车源，灰区队列会相应变化。）

> 验证边界（如实说明）：
> - 开发环境中**没有安装 Docker**，因此 `docker compose up` 未实际执行；
>   `Dockerfile` / `docker-compose.yml` 经过 YAML 解析与服务依赖/健康检查复核，
>   Chromium 系统库清单来自本机实测缺失库（同一 Debian 12 基础镜像）。
> - 其余验证（自动化测试共 76 项，见 §11.1；CLI 全流程、API、价格变动、人工确认回传、
>   SIGKILL 续采、看门狗）均在本机以**与容器内相同的代码路径**
>   （`carscout run` → `crawl.worker` 子进程 → Scrapy/Playwright → PG/Redis）
>   实际执行并记录结果。本批次新增的规则版本化 CLI/API 全链路冒烟同样在本机实测，
>   环境适配方式见 §11.1 说明。

---

## 12. 约束与取舍

- **无前端、无管理界面**：人工确认只走 CSV，服务只对接口与数据一致性负责。
- 平台 D 的签名密钥属于「客户端参数」范畴；服务在浏览器上下文里复用站点自身
  的签名逻辑，不伪造或破解签名算法，过期自动重签（真实接入时按平台授权方式配置）。
- `ROBOTSTXT_OBEY=False` 仅针对本仓库自带的伪造联调服务；接入真实平台前需按
  合作/授权约定调整频次与范围（配置项已集中在 `config.py`）。

---

## 13. 归并规则配置化、灰度上线与证据级解释

### 13.1 一切判定参数都是带版本的规则

`merge_rule_versions` 每一行是一份**不可变的完整规则快照**（`spec` JSONB），
首版 `v1` 在 `init-db` 时自动写入，参数与历史硬编码完全等价（因此老行为零变化）。
`spec` 覆盖打分链路上的每一个旋钮：

```json
{
  "thresholds": {"auto_merge": 0.82, "gray_zone": 0.60},
  "weights": {"series": 0.34, "reg_month": 0.16, "mileage": 0.14, "city": 0.08,
              "transmission": 0.04, "displacement": 0.03, "transfer": 0.03,
              "image": 0.30, "text": 0.26},
  "recall": {"reg_months": 9, "mileage_km": 9000, "candidate_limit": 40},
  "gates": {"image_hamming": 12, "text_similarity": 0.25, "cap_offset": 0.17},
  "required_evidence": ["vin_match"],
  "scoring": {"series_min": 0.70, "vin_conflict_penalty": -0.10, "...": "其余数值参数"}
}
```

- **相似度阈值**：`thresholds.auto_merge / gray_zone`。
- **字段权重**：`weights` 九项（车系/上牌年月/里程/城市/变速箱/排量/过户/图片/描述）。
- **必要证据**：`required_evidence` 可选 `vin_match`（必须 VIN 一致才允许自动合并）、
  `image_agreement`（图片必须一致）、`text_agreement`（描述必须一致）。
  必要条件不满足时**不会直接拆散**，而是把分数压到灰区、强制人工确认；
  创建版本时对取值范围与字段完整性做校验（`rules.validate_spec`）。
- 每次判定把使用的版本 id 写到 `listings.rule_version_id`、
  `match_candidates.rule_version_id` 与 `vehicles.rule_version_id`，可逐条审计。

### 13.2 上线流程：离线回放 → 超门槛人工审批 → 灰度放量 → 可回滚

```
draft ──replay──▶ 影响预估 ──未超门槛或已审批──▶ active（按平台放量）
                    │ 超过审批门槛且未审批 => 禁止激活
active ──rollback──▶ rollback（记录转 pending_rereview，保留 vehicle_id，不自动拆回）
```

1. **创建草稿**：`carscout rules default-spec` 取模板，改完用
   `rules create --spec-file v2.json`（或 `POST /api/rule-versions`）提交为 `draft`。
2. **离线回放**：`rules replay --version-id N`（或 `POST .../replay`）。
   服务在内存里分别用「新规则」和「当前基线」对**全量存量挂牌**重新聚类
   （`matching.simulate_clustering`，只读、不改任何存量），输出：
   - `extra_merges`：新规则会**新增多少组合并**（基线判不同车、新规则判同车）；
   - `split_confirmed`：会**拆散多少组已人工确认的组合**（`manual_confirmed`）；
   - `extra_gray / extra_rejects / unchanged`：判定漂移统计；
   - `report.samples`：最多 50 条带证据明细的差异样本。
3. **审批门槛**：`REPLAY_MAX_EXTRA_MERGES`（默认 10）与
   `REPLAY_MAX_SPLIT_CONFIRMED`（默认 0）任一被突破，`exceeds_threshold=true`，
   此时激活被拒绝，必须 `rules approve --replay-id M`（或
   `POST /api/replays/{id}/decision`）人工审批后才能继续；审批拒绝同样阻断激活。
4. **灰度放量**：激活后用 `rules rollout --set A=10 --set B=0`
   （或 `POST .../rollout`）按平台调百分比。分桶键是
   `md5(platform:listing_id) % 100`，**同一条挂牌永远落在同一桶**，
   调大/调小比例不会让单条记录在新旧规则间反复横跳；未命中放量的流量继续走
   v1 基线。支持 `*` 通配平台，平台精确配置优先于通配。
5. **回滚**：`rules rollback --reason "..."`（或 `POST .../rollback`）立即切回
   上一版本。所有「按被回滚版本合并」且状态为 `auto/ruled` 的挂牌被置为
   `pending_rereview` 并写入原因，但**保留 `vehicle_id`、绝不自动拆回**——
   车源数、报价分布等历史统计不会跳变。受影响配对以 `rollback-vN` 批次进入
   既有人工确认队列（`GET /api/pending-rereview`、`carscout rules rereview`），
   人工通过原 CSV/confirm 回路复核；自动归并批次会跳过这些挂牌。

### 13.3 证据级解释：为什么判同一台车、命中哪几条证据、各自权重多少

每条候选在 `match_candidates.evidence` 留存证据数组；也可随时对任意两条挂牌
即时解释：`GET /api/explain?listing_a=1&listing_b=2`（CLI：`carscout rules explain --a 1 --b 2`）：

```json
{
  "score": 0.8721, "decision": "auto_merge",
  "rule_version": {"id": 1, "version": 1, "required_evidence": ["vin_match"]},
  "required_block": null,
  "gates": {"capped": false, "image_conflict": false, "text_disagreement": false},
  "evidence": [
    {"code": "series", "label": "品牌车系相似度", "matched": true,
     "weight": 0.34, "contribution": 0.34, "detail": {"raw_similarity": 1.0}},
    {"code": "vin_match", "label": "VIN 后六位一致（必要证据候选）", "matched": true,
     "weight": null, "contribution": 1.0, "detail": {"vin_tail6": "AB12CD"}},
    {"code": "image_agreement", "label": "图片感知哈希(pHash)一致性", "matched": true,
     "weight": 0.30, "contribution": 0.291,
     "detail": {"hamming_distance": 1, "gate_distance": 12, "raw_similarity": 0.97}}
  ]
}
```

每条证据给出：证据代码与中文名、是否命中、该字段权重、实际加权贡献、
判定细节（原始相似度/距离/差值/闸门阈值）。`decision` 取值
`auto_merge / manual_review / gray / separate / reject`；
`required_block` 标明是哪条必要证据未满足，`gates` 标明是否被图片/描述闸门压分。

### 13.4 完整操作示例（CLI）

```bash
carscout rules default-spec > v2.json          # 取 v1 模板
# 编辑 v2.json：例如 thresholds 改 0.80、required_evidence 加 "vin_match"
carscout rules create --name "VIN必要条件版" --spec-file v2.json --changelog "收紧自动合并"
carscout rules replay --version-id 2           # 只读回放，看新增合并/拆散预估
carscout rules approve --replay-id 1           # 仅超门槛时需要
carscout rules activate --version-id 2 --rollout A=10   # 先在 A 平台放 10%
carscout rules rollout --version-id 2 --set A=50 --set B=20   # 观察后逐步放量
carscout rules explain --a 12 --b 18           # 证据级解释某一对
carscout rules rollback --reason "A 平台误合并增多"        # 一键回滚
carscout rules rereview                        # 看待复核清单（不自动拆回）
```

### 13.5 对应的数据模型

新增 `merge_rule_versions`（版本与 spec、状态机 draft/active/inactive/rollback、
审批与激活时间戳；回放审批结论挂在 `merge_rule_replays.status`）、
`merge_rule_rollouts`（版本 × 平台放量比例）、
`merge_rule_replays`（回放预估、门槛、审批结论、差异样本）；`listings` /
`match_candidates` / `vehicles` 增加 `rule_version_id`，`match_candidates` 增加
`evidence`，`listings` 增加 `pending_rereview_reason`，`match_status` 增加
`pending_rereview` 状态。所有 DDL 幂等，服务启动 `init-db` 自动迁移并补种子。
