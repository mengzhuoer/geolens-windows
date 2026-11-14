# 构建说明（Windows）

本文档描述如何从本仓库源码构建出 `GeoLens.exe`、免安装 ZIP 与安装包。全部步骤在 Windows 10/11 x64 上完成，不需要 Docker，也不需要 Visual Studio。

## 0. 目录约定

```text
<repo>/
  app/backend/            上游 FastAPI 后端（含改动）
  app/frontend/           上游 React/Vite 前端（含改动）
  desktop/src/            桌面封装层
  desktop/installer/      NSIS 脚本
  desktop/GeoLens.spec    PyInstaller 配置
  desktop/launchers/      分发包里的启动 .bat 与使用前必读
  desktop/runtime/        构建产物（py/ + pgsql/，不入库）
  desktop/cache/          下载缓存（不入库）
  desktop/dist/           构建产物（GeoLens/、ZIP、安装包，不入库）
```

## 1. 前置环境

- Windows 10/11 x64
- 能访问 conda-forge、PyPI、EDB、OSGeo 的网络
- Node.js 20+（本项目使用 Node 26）
- NSIS 3.11（仅生成安装包时需要；可用 `winget install NSIS.NSIS`）
- 7-Zip（可选，生成 ZIP 更快；否则脚本回退到 `Compress-Archive`）

## 2. 一键准备运行时

```powershell
powershell -ExecutionPolicy Bypass -File scripts/prepare-runtime.ps1
```

脚本会：

1. 下载 micromamba 到 `desktop/cache/`；
2. 在 `desktop/runtime/py` 创建 conda 环境：`python=3.13 pip gdal`；
3. 安装 `desktop/requirements-runtime.txt`（后端 + TiTiler + uvicorn）；
4. 安装 `libgdal-pg=3.13.3`（矢量入库需要的 `ogr_PG.dll`）；
5. 下载并解压 PostgreSQL 16.15（EDB）、PostGIS 3.6.2、pgvector 0.8.6 到 `desktop/runtime/pgsql`；
6. 用 `initdb` + `CREATE EXTENSION postgis/vector` 做一次冒烟验证。

若只想重装某一步，可加参数：

```powershell
# 只重建 Python 环境
powershell -ExecutionPolicy Bypass -File scripts/prepare-runtime.ps1 -PythonOnly

# 只重建 PostgreSQL/PostGIS/pgvector
powershell -ExecutionPolicy Bypass -File scripts/prepare-runtime.ps1 -PostgresOnly
```

### 手工等价步骤

```powershell
# Python + GDAL
desktop/cache/micromamba.exe create -y -p desktop/runtime/py -c conda-forge python=3.13 pip gdal
desktop/runtime/py/python.exe -m pip install -r desktop/requirements-runtime.txt
desktop/cache/micromamba.exe install -y -p desktop/runtime/py -c conda-forge "libgdal-pg=3.13.3"

# PostgreSQL 16.15
Invoke-WebRequest https://get.enterprisedb.com/postgresql/postgresql-16.15-1-windows-x64-binaries.zip -OutFile desktop/cache/pg16.zip
Expand-Archive desktop/cache/pg16.zip desktop/cache/pg16 -Force
Move-Item desktop/cache/pg16/pgsql desktop/runtime/pgsql

# PostGIS 3.6.2（合并 bin/lib/share，跳过 7 个同名 DLL）
Invoke-WebRequest http://download.osgeo.org/postgis/windows/pg16/postgis-bundle-pg16-3.6.2x64.zip -OutFile desktop/cache/postgis16.zip
Expand-Archive desktop/cache/postgis16.zip desktop/cache/postgis16 -Force
# 把 postgis 目录下的 bin/lib/share 内容覆盖进 desktop/runtime/pgsql，
# 但不要覆盖 libcrypto-3-x64.dll、libcurl.dll、libiconv-2.dll、liblz4.dll、
# libssl-3-x64.dll、libzstd.dll、zlib1.dll。

# pgvector：从 conda 环境取出 extension 文件
desktop/cache/micromamba.exe create -y -p desktop/cache/pgv -c conda-forge pgvector=0.8.6
Copy-Item desktop/cache/pgv/Library/lib/vector.dll desktop/runtime/pgsql/lib/
Copy-Item desktop/cache/pgv/Library/share/extension/vector* desktop/runtime/pgsql/share/extension/
```

验证数据库组件：

```powershell
$rt = "$PWD/desktop/runtime"
& "$rt/pgsql/bin/initdb.exe" -D "$PWD/tmp/pgdata" -U postgres --pwfile="$PWD/tmp/pw.txt" --encoding=UTF8 --locale=C
& "$rt/pgsql/bin/pg_ctl.exe" -D "$PWD/tmp/pgdata" -l "$PWD/tmp/pg.log" -o "-p 55432" start
& "$rt/pgsql/bin/psql.exe" -h 127.0.0.1 -p 55432 -U postgres -d postgres `
  -c "CREATE EXTENSION postgis; CREATE EXTENSION vector;" `
  -c "CREATE TABLE t(e vector(4)); CREATE INDEX ON t USING hnsw (e vector_cosine_ops);"
& "$rt/pgsql/bin/pg_ctl.exe" -D "$PWD/tmp/pgdata" stop
```

能建 HNSW 索引即成功。

## 3. 构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

脚本会：

1. `app/frontend`：`npm ci` + `npm run build`；
2. `desktop`：`runtime/py/python.exe -m PyInstaller --noconfirm GeoLens.spec`；
3. 组装 `desktop/dist/GeoLens/`：

```text
desktop/dist/GeoLens/
  GeoLens.exe + _internal/     ← PyInstaller
  app/backend/                 ← app/backend（排除 tests/、__pycache__、.venv）
  app/frontend_dist/           ← app/frontend/dist
  app/server_entry.py
  app/geolens_server.py        ← desktop/src/geolens_server.py
  app/geolens_web.py           ← desktop/src/geolens_web.py
  app/desktop/                 ← desktop/src/desktop（CLI 用源码）
  runtime/py/                  ← desktop/runtime/py
  runtime/pgsql/               ← desktop/runtime/pgsql
  启动.bat / 启动-局域网.bat / 启动后端.bat / 启动网页.bat ...
  使用前必读.txt
  README-使用说明.md           ← docs/USAGE.md
```

**不要**把 `desktop/data/` 放进分发包：首次启动自动生成。

### 开发调试（免打包）

```powershell
# 用构建好的 runtime 直接跑桌面启动器（前台，Ctrl+C 停止）
$env:GEOLENS_DESKTOP_HOME = "$PWD/desktop"
desktop/runtime/py/python.exe desktop/src/run_desktop.py
```

- 桌面控制台：<http://127.0.0.1:8080/__desktop/>
- 数据目录：<http://127.0.0.1:8080/>

## 4. 生成安装包

```powershell
powershell -ExecutionPolicy Bypass -File scripts/package-release.ps1
# 指定 NSIS 路径（默认从 PATH 查找 makensis.exe）
powershell -ExecutionPolicy Bypass -File scripts/package-release.ps1 -Makensis "C:\Program Files (x86)\NSIS\makensis.exe"
```

输出：

```text
desktop/dist/release/GeoLens-Portable-<版本>-win64.zip
desktop/dist/release/GeoLens-Setup-<版本>-win64.exe
desktop/dist/release/*.sha256
```

版本号在 `desktop/src/desktop/config.py` 的 `APP_VERSION` 与 `desktop/installer/geolens.nsi` 的 `VERSION` 两处，需同时修改。

## 5. 验证清单

1. 干净环境双击 `GeoLens.exe` → 出现进度页 → 自动进入数据目录（`http://127.0.0.1:8080`）；
2. 目录顶部显示 `admin`，无“登录”入口，能列出已有数据集；
3. 把 1 个 GeoTIFF 拖进 `影像导入\`，15 秒内出现“入库成功”，目录出现该数据集；
4. 打开栅格数据集 → 地图能出图（切片 200）→ 下方“多时相预览”能切换时相；
5. 右下角浮条点两次“退出” → 端口 8080/8000/8002/8003/54329 全部释放，无残留进程；
6. 安装包卸载 → 默认保留 `data/`，重装后可继续使用。

## 6. 常见构建问题

| 现象 | 原因/处理 |
|---|---|
| 启动报 `psycopg` 事件循环错误 | 必须用 `desktop/src/server_entry.py` 以 `SelectorEventLoop` 启动 API/Worker |
| 矢量入库 422、`create_subprocess_exec` 不可用 | `server_entry.py` 的线程版 subprocess shim 被跳过或运行了非该入口 |
| `ogr_PG.dll` 找不到 | 未安装 `libgdal-pg=3.13.3`，或没有设置 `GDAL_DRIVER_PATH` |
| COG 转换报 `IGNORE_COG_LAYOUT_BREAK` | `app/processing/raster/cog.py` 的补丁未生效 |
| 安装包体积异常大 | 打包时没有排除 `data/`（默认脚本已排除） |
| `npm ci` 失败 | 用 Node 20+，并确认 `app/frontend/package-lock.json` 完整 |

更多见 [FAQ.md](FAQ.md)。
