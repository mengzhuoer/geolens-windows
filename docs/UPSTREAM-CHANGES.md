# 相对上游的改动

基线：geolens-io/geolens 1.29.2（本仓库 `app/` 为 `mengzhuoer/geolens` 的 main 快照，含上游 1.29.2 之后的提交；产品版本见 `desktop/src/desktop/config.py`）。

## 后端（`app/backend`）

| 文件 | 改动 | 原因 |
|---|---|---|
| `app/processing/ingest/validation.py` | 在 puremagic 之前接受 TIFF/BigTIFF 魔数 `II*\0`、`MM\0*`、`II+\0`、`MM\0+` | BigTIFF 被误判为未知格式，上传 422 |
| `app/processing/raster/cog.py` | `gdaladdo` 增加 `-oo IGNORE_COG_LAYOUT_BREAK=YES` | GDAL 3.13 拒绝给已是 COG 布局的文件追加概览 |
| `app/platform/jobs/worker.py` | 健康检查端口读取 `GEOLENS_WORKER_HEALTH_PORT` | 上游硬编码 8001，与 TiTiler 冲突 |
| `app/processing/ingest/pointcloud_decode.py` | `import resource` 改为 `try/except ImportError` | Windows 没有 `resource` 模块 |
| `app/api/middleware/raster_alias.py` | 新增：把 `/raster-tiles/...` 重写为 `/tiles/raster-proxy/...` | 独立前端直连 API 时保持同一套瓦片路径 |
| `app/api/main.py` | 注册 `RasterAliasMiddleware` | 同上 |
| `app/modules/auth/router.py` | 新增 `POST /auth/auto-login`（仅 `GEOLENS_AUTO_LOGIN=1`） | 桌面版无登录，自动签发本地管理员会话 |
| `app/modules/catalog/datasets/api/router.py` | 新增 `POST /datasets/{id}/reveal` | 多时相预览“在文件夹中定位” |

> `raster_alias.py` 与 `/auth/auto-login` 都使用 `include_in_schema=False`，不改变上游 OpenAPI 合约。

## 前端（`app/frontend`）

| 文件 | 改动 | 原因 |
|---|---|---|
| `src/lib/env.ts` | 新增 `AUTO_LOGIN` 环境字段 | 从 `/env-config.js` 读取桌面版开关 |
| `src/api/auth.ts` | 新增 `autoLogin()`，错误对象带 `status` | 无登录模式调用；404 表示未启用 |
| `src/lib/session-sync.ts` | 自动登录优先、503 重试、60s 首帧预算、退出后重载 `/` | 避免匿名首帧缓存空目录；刷新后保持管理员 |
| `src/components/builder/layer-description.ts` | 瓦片基址为绝对 URL 时不再拼相对前缀 | 前后端分离/独立域名部署 |
| `src/pages/DatasetPage.tsx` | 接入 `TemporalPreviewPanel`；栅格预览追加 `stretch=percentile` | 多时相 + 16 位影像不再全黑 |
| `src/components/dataset/TemporalPreviewPanel.tsx` | 新增 | 同区域时相归组、地图定位、缩略图/键盘切换、文件夹定位 |

## 桌面封装层（`desktop/`，本项目新增）

- 单实例启动器 + detach 后台服务 + PyInstaller 打包 + NSIS 安装器。
- 单端口同源代理（SPA / API / 瓦片 / 启动页 / 浮条）。
- PostgreSQL 集群初始化、迁移、schema 版本跳过。
- 影像库 librarian（扫描 / 去重 / 整理 / 转换 / 入库 / 账本）。
- 控制台页面（状态、影像库设置、局域网、日志）。
- 所有 Windows 适配：SelectorEventLoop、线程版 subprocess shim、`GDAL_DRIVER_PATH`、BELOW_NORMAL 优先级、启动器 watchdog。

## 升级上游时的建议

1. 把新版上游源码替换到 `app/`；
2. 重新应用本文件列出的后端/前端改动；
3. 更新 `docs/UPSTREAM-CHANGES.md` 的基线版本；
4. 跑 `docs/BUILD.md` 的验证清单。

桌面层与上游业务代码解耦，通常无需改动。
