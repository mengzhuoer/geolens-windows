# 第三方组件与许可证说明

本软件是 GeoLens 的本地化 Windows 打包版本，包含以下开源组件。各组件仍受其原始许可证约束。

| 组件 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| GeoLens | 1.29.2 | Apache License 2.0 | https://github.com/geolens-io/geolens |
| PostgreSQL | 16.15 (Windows x64) | PostgreSQL License | https://www.enterprisedb.com/download-postgresql-binaries |
| PostGIS | 3.6.2 | GNU GPL v2（PostGIS 部分组件） | https://postgis.net/ |
| pgvector | 0.8.6 | PostgreSQL License | https://github.com/pgvector/pgvector |
| Python | 3.13 | PSF License | https://www.python.org/ |
| GDAL / PROJ / GEOS | 3.13.3 / 9.x / 3.13 | MIT / X11 等 | https://gdal.org/ |
| rasterio | 1.5.2 | BSD-3-Clause | https://github.com/rasterio/rasterio |
| TiTiler | 2.4.0 | MIT | https://github.com/developmentseed/titiler |
| FastAPI / Starlette / Uvicorn | 最新兼容版 | MIT / BSD | PyPI |
| React / MapLibre GL JS | 构建产物 | MIT / BSD | 前端依赖 |
| pywebview | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com/ |
| PyInstaller | 6.22.3 | GPL with exception（打包工具，不传染输出） | https://pyinstaller.org/ |

完整的依赖清单与许可证文本可通过：

- 后端：`app\backend\pyproject.toml`
- 前端：`app\frontend_dist` 构建产物及原项目 `frontend\package.json`
- Python 运行时：`runtime\py\conda-meta` 与 `runtime\py\Lib\site-packages\*.dist-info`

查看。分发本软件时请保留本文件与 GeoLens 的 `LICENSE`。
