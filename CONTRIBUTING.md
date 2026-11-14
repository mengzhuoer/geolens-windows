# 参与开发

感谢你关注这个项目。它是 GeoLens 的 Windows 二次开发发行版，改动尽量集中在 `desktop/` 与少量上游补丁。

## 本地开发

```powershell
# 1. 准备运行时（Python + GDAL + PostgreSQL/PostGIS/pgvector）
powershell -ExecutionPolicy Bypass -File scripts/prepare-runtime.ps1

# 2. 构建前端
cd app/frontend; npm ci; npm run build; cd ../..

# 3. 前台运行桌面启动器
$env:GEOLENS_DESKTOP_HOME = "$PWD/desktop"
desktop/runtime/py/python.exe desktop/src/run_desktop.py
```

## 提交前检查

- Python：`desktop/runtime/py/python.exe -m py_compile` 修改过的文件；
- 前端：`cd app/frontend; npm run build` 必须通过（含 `tsc -b`）；
- 手动验证：启动 → 目录可打开 → 拖入一张 GeoTIFF 能入库 → 退出后端口释放；
- 若改了上游文件，请同步更新 `docs/UPSTREAM-CHANGES.md`。

## 提交信息

使用 Conventional Commits 风格，例如：

```text
feat(desktop): add temporal preview panel
fix(proxy): keep raster tiles on the same origin
docs(build): document libgdal-pg requirement
```

## 分支与 PR

- 从 `main` 切分支，一个 PR 只做一件事；
- PR 描述写清：改了什么、为什么、如何验证（命令或截图）；
- 不提交 `data/`、`desktop/runtime/`、`desktop/cache/`、`node_modules/`、`dist/` 等生成物。

## 上游改动

如果某个问题其实是上游 bug，优先到 [geolens-io/geolens](https://github.com/geolens-io/geolens/issues) 反馈；本项目只保留桌面/Windows 必需的补丁。
