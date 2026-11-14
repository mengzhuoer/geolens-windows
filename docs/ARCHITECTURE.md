# 架构

## 进程模型

```text
GeoLens.exe                        启动器（PyInstaller windowed，单实例检测）
└── GeoLens.exe --service          后台服务（DETACHED_PROCESS，存活于启动器退出后）
    ├── HTTP 前端   127.0.0.1:8080  单端口代理：SPA + /api + /raster-tiles + /__boot + /__shell.js
    ├── PostgreSQL  127.0.0.1:54329 内置集群，data/pgdata
    ├── FastAPI     127.0.0.1:8000  SelectorEventLoop + subprocess shim
    ├── TiTiler     127.0.0.1:8002  COG 切片
    └── Worker      127.0.0.1:8003  Procrastinate 入库/转换队列（BELOW_NORMAL）
```

- 启动器只负责：单实例检测、端口分配、spawn 后台服务、等待代理可用、打开应用窗口。
- 后台服务负责：数据库、三个子进程、代理、librarian 定时扫描。
- 子进程通过 `GEOLENS_LAUNCHER_PID` 监视后台服务；后台服务退出时子进程自行退出，API 进程还会 `pg_ctl stop`。
- 关闭应用窗口不会停止后台服务；右下角浮条“退出”会请求 `/__desktop/api/quit`，由后台服务统一收尾。

## 单端口代理

`desktop/src/desktop/proxy.py` 复刻官方 nginx 路由：

| 路径 | 处理 |
|---|---|
| `/api/*` | 去掉 `/api` 前缀转发到 FastAPI |
| `/raster-tiles/<id>/tiles/z/x/y.ext` | 重写为 `/tiles/raster-proxy/<id>/z/x/y.ext` |
| `/env-config.js` | 动态输出 `API_BASE_URL`、`TILE_BASE_URL`、`AUTO_LOGIN` |
| `/__boot/` | 启动进度页（轮询 `/__desktop/api/status`，ready 后跳 `/`） |
| `/__shell.js` | 右下角浮条（控制台 / 两段式退出） |
| `/__desktop/*` | 控制台页面与本地控制 API（仅回环客户端） |
| 其他 | SPA 静态文件 + 历史路由回退 |

同源带来两个直接好处：无需 CORS，刷新页面时 httpOnly refresh cookie 仍然有效。

## 启动顺序

1. 读取 `data/config.json`（首次生成 JWT 密钥、管理员密码、端口）；
2. 若 `data/pgdata` 不存在则 `initdb`；
3. `pg_ctl start` 并等待 `pg_isready`；
4. 创建数据库与扩展（PostGIS、pgvector、pg_trgm、unaccent）；
5. 比对 `data/schema-version.txt` 与 `APP_VERSION`，不一致才执行 alembic；
6. **并行** spawn API / TiTiler / Worker，并并发等待健康检查；
7. 启动 librarian 定时扫描；
8. `/__boot/` 轮询到 `phase=ready` 后跳转数据目录。

## 配置

`data/config.json`：

```jsonc
{
  "ports": { "proxy": 8080, "api": 8000, "titiler": 8002, "worker_health": 8003, "postgres": 54329 },
  "server": { "api_host": "127.0.0.1", "auto_login": true },
  "network": { "lan_access": false, "public_host": "" },
  "library": {
    "source_dirs": ["...\\影像导入"],
    "library_dir": "...\\data\\library",
    "single_folder": true,
    "mode": "copy",                 // copy | move | link
    "auto_ingest": true,
    "scan_on_startup": true,
    "scan_interval_seconds": 15
  },
  "ui": { "mode": "browser", "window_width": 1280, "window_height": 820 }
}
```

- 只有新数据目录才会重新分配端口；已有集群的端口写在 `pgdata/pg_hba.conf` 与 `postmaster.pid` 里，不能随意改。
- `server.auto_login=false` 恢复上游登录界面（前端不再请求自动会话）。

## 数据目录

```text
data/
  pgdata/            PostgreSQL 集群
  library/           整理后的影像库（保留源目录相对结构）
  library-index.sqlite3  扫描账本：源路径、指纹、库路径、dataset_id、状态
  storage/rasters/   COG 与切片缓存
  logs/              desktop.log / api.log / worker.log / titiler.log / librarian.log
  config.json
  schema-version.txt
```

## 影像库 librarian

`desktop/src/desktop/librarian.py` 每 15 秒（可配置）执行一轮：

1. 遍历 `source_dirs`（默认只有 `影像导入/`），跳过隐藏/系统目录与超限文件；
2. 计算内容指纹（quick 模式按首尾块 + 大小，full 模式整文件），命中账本就跳过；
3. 按 `mode` 复制/移动/硬链接到 `library/<相对路径>`；
4. ENVI `.img`、JP2、BMP 等先转 GeoTIFF；Shapefile 自动打包成 ZIP；
5. 调本地 API：`upload → preview → commit`，获得 `dataset_id`，写入账本；
6. 失败重试 `retry_limit` 次，状态与消息写进 SQLite，供控制台展示。

## 无登录模式

- 后端仅在 `GEOLENS_AUTO_LOGIN=1` 时注册 `POST /auth/auto-login`（`include_in_schema=False`）。
- 该接口为本地管理员签发 access + refresh，cookie 传输与普通登录一致。
- 前端 `/env-config.js` 输出 `AUTO_LOGIN:"1"`；启动时 `restoreSession()` 优先自动登录（忽略 localStorage 残留的旧 sessionId），首帧渲染前最多等待 60 秒，避免先以匿名身份缓存空目录。
- 过期/服务未就绪（503）按 1.2 秒重试，共 40 次；自动登录被禁用（404）立即停止，回退到普通登录界面。
- 安全边界：只有网页端口可对局域网开放，且默认关闭；控制台始终只接受回环请求。**无登录意味着能访问端口的人都有管理权限**。

## 多时相预览

`app/frontend/src/components/dataset/TemporalPreviewPanel.tsx`：

1. 用当前数据集 bbox 调 `/search/datasets/?bbox=...&limit=100`；
2. 只保留 `raster_dataset`，要求 footprint 与当前重叠 > 40%，且标题去掉月份/年份后相同（或重叠 > 85%）；
3. 从标题解析 `Apr 2023` / `2023-04` 得到时间，按“标题 + 时间”去重（历史重复入库只留一条）；
4. 地图叠加当前时相栅格 `${TILE_BASE}/raster-tiles/<id>/tiles/{z}/{x}/{y}.png?stretch=percentile`，并在 `onLoad` 安装 `buildTileTransformRequest()` 后渲染，保证私有瓦片带 Bearer 头；
5. 点击地图用 `interactiveLayerIds=['temporal-fill']` 收集同一位置的所有时相；
6. `POST /datasets/{id}/reveal` 从数据集摘要解析 `影像库路径`，在资源管理器中 `/select` 该文件（仅 Windows、仅本机）。

## 打包

- `desktop/GeoLens.spec` 只打包启动器（PyInstaller onedir、windowed），并排除 numpy/pandas/fastapi 等，避免把整个运行时塞进 exe。
- 运行时与后端源码放在 exe 旁边的 `runtime/`、`app/`，由 `paths.py` 定位；因此升级业务代码不需要重打 exe。
- NSIS 安装包用户级安装到 `%LOCALAPPDATA%\GeoLens`，卸载默认保留 `data/`。
