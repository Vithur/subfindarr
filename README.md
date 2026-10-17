# subfindarr

与 arr 栈配套的中文字幕下载与整理服务。挂在 Sonarr / Radarr 的 Webhook 上，
影片下载或导入后自动寻找简体中文单语字幕，解压、转码、按规范改名落到影片同目录，
再通知 Emby 精确刷新单条。

```
Seerr 订阅 → Sonarr/Radarr → qBittorrent → arr 导入
                                    ↓ Webhook (Download / Upgrade / Rename)
                              subfindarr
                                    ↓ 搜索 → 选优 → 消歧 → 三跳解析 → 下载 → 校验 → 解压 → 改名
                              影片同目录 *.zh-cn.srt
                                    ↓
                              Emby /Items/{id}/Refresh
```

不碰下载链路，只做事后字幕闭环。

## 功能

- **arr 事件接入**：Sonarr / Radarr 的 `Download` / `Upgrade` / `Rename` 事件
- **多源适配**：目前实现 subhd.tv；适配器接口为三段式（search / resolve / download），
  兜底源按同一接口接入
- **选优**：官方简中单语 > 官译简中 > 简中单语 > 简繁双语 > 纯繁；
  AI 机翻、枪版源、广告水印、图形字幕分别降权或直接排除
- **同名不同片消歧**：按影视页的真实年份比对，标题里的年份只作加分
- **下载完整性**：Content-Length 与 EOCD 双判据，不完整自动 Range 续传，半截文件绝不解包
- **落盘**：`in_place` 直接写影片同目录，或 dry-run 只写独立目录
- **Emby 刷新**：按条目 Id 精确刷新并回查字幕流
- **改名跟随**：arr 重命名或质量升级后，字幕跟着改前缀
- **台账**：SQLite 记录任务、已装字幕、动作流水，降级路径都有专属状态可查

## 快速开始

前提：已有一套运行中的 arr 栈（Sonarr / Radarr / Emby），且与 subfindarr 在同一个 Docker 网络。

```bash
# 1. 准备环境变量
#    MEDIA_PATH 必填（媒体库宿主绝对路径）
#    ARR_NETWORK 默认 arr，若你的栈用别的网络名要改
cp .env.example .env
$EDITOR .env

# 2. 准备配置
mkdir -p config
cp config.example.yaml config/config.yaml

# 3. 启动
docker compose up -d --build

# 4. 探活：健康检查走 8097 端口的 /health
curl -s http://localhost:8097/health
```

`/health` 返回示例：

```json
{
  "ok": true,
  "sources": [{"name": "subhd", "configured": true}],
  "emby": true,
  "cache": {"enabled": true, "files": 0},
  "media_root": "/media",
  "write_in_place": false
}
```

随后在 Sonarr / Radarr 里加 Webhook（见下节）。

## 配置说明

配置文件默认 `config/config.yaml`（容器内路径由 `SUBFINDARR_CONFIG` 指定），
支持 `${VAR:default}` 环境变量展开。

### `naming.preferred`

落地后缀，**第一个元素是写入用名**，其余仅作为「已有字幕」的识别别名：

```yaml
naming:
  preferred: [".zh-cn.srt", ".zh-Hans.srt", ".zh.srt", ".chs.srt", ".zh-CN.srt"]
  aliases: [".zh-cn", ".zh-Hans", ".zh-CN", ".zh", ".chs", ".cht"]
  extensions: [".srt", ".ass", ".ssa", ".sub"]
```

- `preferred[0]` 决定写出去叫什么；`aliases` 决定**哪些名字算已经有字幕**。
  两者分开，是为了对齐媒体库里既有的命名习惯（常见 `.zh-cn` / `.chs` / `.ZH-CN`）。
- 判断「已有字幕」用的是**片名主体模糊匹配**（忽略质量标记与年份），
  所以 arr 改名或质量升级后不会重复下载。
- 字幕内容若是 ASS，落盘扩展名按**内容**决定而非源文件名。

### `write.in_place`

| 值 | 行为 | 适用 |
|---|---|---|
| `false`（默认） | 只写 `paths.subs_out`，媒体库一个字节不动 | 首次上线、灰度、排错 |
| `true` | 直接写影片同目录，再调 Emby 单条刷新 | 验收通过后 |

`write.fallback_to_subs_out` 控制写入失败时是否回退到 `subs_out`。回退**不算成功**：
会打 WARN 日志、台账记 `fallback` 状态、并且**不触发 Emby 刷新**（刷新了但字幕不在
媒体库里是最难查的假成功）。

### `emby.libraries`

库 Id → 容器内路径前缀的映射。**一个库可能有多个前缀，必须都写。**

同一个库里常同时存在两种条目：真文件型（路径在 `/media/...`）与 strm 型
（路径在 `/strm/...`，媒体源是远程 http）。只登记其中一个前缀，另一类条目会永远匹配不到：

```yaml
emby:
  libraries:
    "1":   /media/movies        # 真文件型
    "1s":  /strm/movies         # 同库的 strm 条目（key 用 库Id + 后缀 区分）
    "2":   /media/tv
    "2s":  /strm/tv
```

归属时按**最长前缀**匹配，key 里 `-` 或后缀只用于区分、不作为库 Id 使用。
库 Id 可以从 `GET /Library/VirtualFolders` 或 Emby 管理界面查到。

### `network.use_proxy`

出站策略是三级降级：**直连 HTTPS（带重试）→ 代理 → FlareSolverr**。

```yaml
network:
  retries: 3
  timeout_sec: 45
  use_proxy: true
  proxy: http://your-proxy:7890   # 仅在直连全部失败后才用
```

- 直连优先：很多环境里走代理访问字幕站反而更差，代理只作兜底。
- `no_proxy` 需要包含所有内部服务名（emby / sonarr / radarr / qbittorrent /
  flaresolverr / subfindarr），否则容器内互访会被代理拦。

## 接 Webhook

Sonarr / Radarr → Settings → Connect → Webhook：

- **URL**：`http://subfindarr:8097/webhook/sonarr`、`http://subfindarr:8097/webhook/radarr`
  （各自独立路径；也接受 `/webhook/arr`，接收端会按 payload 自动识别来源）
- **触发事件**：`On Download`、`On Upgrade`、`On Rename`（Sonarr 另有 `On Import Complete`；
  Radarr 没有该事件）
- **Method**：POST
- **鉴权**：加请求头 `Authorization: Bearer <WEBHOOK_TOKEN>`

自定义请求头在 arr 里叫 `headers` 字段；部分版本通过 API 创建通知时需要在 URL 上加
`?forceSave=true`，否则 arr 会先做一次连通性测试、测试失败就拒绝保存。

`server.webhook_require_token` 默认 `true`：token 为空时**一律拒绝**，
不会出现「以为开了其实没配」的静默放行。仅本地调试可临时置 `false`。

接收端是「先入队再回 200」，重活不在响应前做。

## 状态与台账

每次任务会落到以下状态之一，**只有 `done` 算成功**：

| 状态 | 含义 | 是否刷新 Emby |
|---|---|---|
| `done` | 字幕已写入媒体库 | 是（并回查字幕流） |
| `fallback` | 写入媒体库失败，回退到 `subs_out` | 否 |
| `skipped_strm` | strm 型条目，不支持外挂字幕 | 否 |
| `skipped` | 该片已有中文字幕 | 否 |
| `refresh_pending` | 已提交刷新，回查尚未见到字幕流 | 已提交，待回查 |
| `failed` | 处理失败（原因记在 `last_error`） | 否 |

`/stats` 会把各状态分开计数，并提供 `_ok_only_done`（成功数）与 `_degraded`（降级数）两个汇总。

## 手动触发

`/process` 与 Webhook 走同一套鉴权：

```bash
curl -s -X POST "http://localhost:8097/process" \
  -H "Authorization: Bearer $WEBHOOK_TOKEN" \
  --data-urlencode "path=/media/movies/影片名 (2023)/影片名.2023.1080p.mkv" \
  -d "media_type=movie" -d "title=影片名" -d "year=2023"
```

## 自测

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

覆盖：文件名解析与指纹、评分与词表回归（`test_core`）；适配器三段式与语言标签、
三跳重试（`test_subhd`）；Emby 单条目与刷新链（`test_emby`）；strm 守卫与降级终态
（`test_guard`）；下载完整性、续传、zip 文件名编码、图形字幕过滤（`test_integrity`）；
改名跟随与前缀模糊匹配（`test_rename`）。

## 已知限制

- **strm 型条目不支持外挂字幕**。这类条目的媒体在远端，旁挂字幕文件永远不会被扫描到；
  刷新接口仍会返回 204 但字幕流为零。pipeline 会识别并跳过，台账记 `skipped_strm`。
- **同目录存在多个视频版本**（原始 + 升级版）时，改名跟随只按新视频的前缀处理，
  不会同时维护另一版本的对应关系。
- **字幕站对同一资源有短时频控**，会间歇性返回 403。服务内已按跳重试并带退避，
  但热门片候选多、请求量大，处理耗时会明显变长；冷门片成功率高得多。
- 兜底字幕源（assrt / opensubtitles）尚未接入。
- 字幕内容级别校验只做到广告水印与文件命名层面，没有做音轨对齐或 OCR。

## 目录结构

```
app/
  main.py        FastAPI：/webhook/...、/process、/health、/stats
  pipeline.py    主流程：事件 → 查源 → 选优 → 消歧 → 下载 → 解压 → 落盘 → 刷新
  fingerprint.py arr payload → 媒体指纹
  scoring.py     语言档位与黑名单词表评分
  fetcher.py     三级降级取数、下载完整性校验与续传
  pack.py        解压、编码探测、字幕格式嗅探、规范化改名
  db.py          SQLite 台账
  api.py         Sonarr / Radarr / Emby 客户端
  auth.py        Webhook Bearer 鉴权
  cache.py       页面缓存
  sources/       适配器接口与 subhd 实现
config.example.yaml
compose.yaml
Dockerfile
```
