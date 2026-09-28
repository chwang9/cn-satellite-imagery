#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 WGS84 包围盒按卫星影像瓦片下载并拼成 GeoTIFF。

特点：
- 输出原生 EPSG:3857（Web Mercator），无需重采样，画质无损；
- 窗口化落盘（逐瓦片写入），**峰值内存与面积解耦**：解码数组是否全量留内存由
  `--mem-budget-mb`（默认 2048）决定，超出预算自动改流式（写入阶段回读缓存瓦片），
  因此 zoom=18 的大图也不会把内存吃爆（v1.6.0 起为名副其实）；
- 瓦片本地缓存（scripts/_tiles/），二次同区域免重复下载；坏缓存自动重下；
- 瓦片数上限保护，避免大面积高 zoom 误触发的海量下载；
- 精确裁切到请求 bbox；可选重投影到 EPSG:4326（便于与 cn-dem 的 DEM 叠加）；
- 【v1.1】匀色（per-tile 相对辐射归一化）+ 消拼缝（瓦片边界交叉淡化），内存轻量；
- 【v1.2】失败瓦片不致命（空洞用全局均值填充）+ 纯色/空瓦片检测（可选填充）
  + 多影像源（--source）+ 下载连接池/快速失败调优。
- 【v1.4.2】瓦片统计/空白检测性能：4×4 子采样（统计只需分布特征）+ none 匀色模式跳过
  百分位计算 + 直接复用下载阶段已持有的内存解码数组（不再重读缓存磁盘），z18 量级统计约 15× 加速。
- 【v1.4.3】none 模式空洞填充均值改为复用 _analyze_tile 的 4×4 子采样均值（省去此前每张瓦片
  对整张 256×256 做的全分辨率 mean，约 16× 计算量）；首张瓦片连通性探测结果在并发阶段直接复用
  （避免重复下载+解码）；--epsg 4326 重投影后重建概览金字塔（此前成品缺金字塔、GIS 放大很慢）。
- 【v1.5.0】严重正确性修复 + 新增研究区边界叠加。
  (A) 影像内容与其地理参考完全错位（共三处根因，均已修复）：
    1) 影像源行号误翻转：Esri 的 {y} 是 XYZ 行号（y=0 最北），此前误设 flip=True，请求到
       「关于赤道镜像」的行——瑶海区实际取到南纬约 32°（西澳小麦带），故出现大片农田与
       597 个「纯色/空瓦片」（实为海洋）。改为 flip=False。
    2) 行偏移方向反了：写瓦片用 `(y1 - t.y)`（y1 是最南行），把最北行写到图像底部，造成
       南北镜像；应为 `(t.y - y0)`（y0 是最北行）。
    3) 整块范围上下各错 1 个瓦片：误用 `ul((x0,y1))`/`ul((x1+1,y0-1))`，应改用角瓦片
       `xy_bounds(Tile(x0,y0))`（左/上）与 `xy_bounds(Tile(x1,y1))`（右/下）。
    以瓦片中心点回算经纬度 + 与独立获取的 (flip=False) 底图逐像素对比验证：仅
    (flip=False, row=(t.y-y0)) 组合 meanAbsDiff=0.00，其余组合 27~40 灰度级。
  (B) make_map.py 新增研究区行政边界叠加（论文「研究区」图）：--boundary（DataV GeoJSON，
    GCJ-02 自动转 WGS84），一键入口 --map 默认自动叠加本区县边界红线，--no-boundary 关闭。
- 【v1.5.1】第 4 处地理参考错位修复（历史遗留）：裁切 _crop_to_bbox 调
  from_bounds(new_left, new_top, ...) 时把北边界 new_top 当成了 bottom、南边界当成了 top，
  输出 Affine 的 e 由 -res 变 +res，成品 GeoTIFF 的 geotransform 南北颠倒（像元数据正确、
  坐标声明错误）。修正参数顺序为 from_bounds(left, bottom=north-h*res, right, top=north, w, h)。
  验证：与独立 Esri 底图直接比较，正向 NCC=0.937、垂直翻转 0.005，证明像元网格为 north-up。
- 【v1.5.2】专题图出图体验优化（无功能性 bug 修复）：
  (A) 一键入口新增 `--pad`（默认 0.10）：下载区域相对行政区包围盒外扩该比例，给研究区四周
      留出地理留白，避免行政边界压在图边（此前 TIF 精确裁到 bbox，红线正好贴图框）。
  (B) make_map.py 指北针重绘为专业双色罗盘指针（北深南浅 + 枢轴圆点 + 白描边），并给行政边界
      加白色底衬，深浅影像上均清晰可读。
- 【v1.7.9】warp 多线程（出图链路与整幅重投影同源根因）：
  (A) `reproject` 此前一直是**单线程**。**关键坑**：设 `GDAL_NUM_THREADS` 或用 `rasterio.Env`
      包住**完全无效** —— 实测该变量取 1/2/4/8/ALL_CPUS 时整条读取耗时都在 960~990 ms
      （概览生成同理，24~30 s 纯抖动）；必须用 `reproject(num_threads=...)` 显式传参。
  (B) 收益：出图链路（4592x4848 -> 2296x2424）387 ms -> 107 ms（8 线程，3.6x）；
      整幅 3857->4326（23943x29724 源）53.3 s -> 24.9 s（2.14x）。
      两者**逐像素 max|Δ| = 0**（GDAL 多线程 warp 按块独立计算、块间无共享状态）。
  (C) 线程数上限取 `min(cpu_count, 8)`：实测整幅场景 16 线程反而 **33.0 s（比 8 线程慢 30%）**，
      属超额订阅争抢 —— 8 是拐点，不是越多越好。
- 【v1.7.8】全技能优化审视（每项都有微基准）：
  (A) `_analyze_tile` 在 percentile 模式下把「np.median + 两次 np.percentile」合并为
      **一次** `np.percentile(p, (5, 50, 95), axis=0)`：536 → 363 µs/瓦片（-32%），
      三个值逐位一致（Δ=0）。**注意：默认的 none 模式本就只算一次 median，实测收益 0.2%
      （无可优化空间）** —— 此项只对 `--balance-mode percentile` 有意义。
  (B) `_crop_to_bbox` / `reproject_to_wgs84` 拼临时文件名由 `src_path[:-4]` 改为
      `os.path.splitext(src_path)[0]` —— 前者假定扩展名恒为 4 字符，传 `.tiff` 会切掉
      文件名尾部。
  (C) `feather_mosaic` 改为打印**实际处理**的缝数（原打印理论值 `nx-1`/`ny-1`；
      当 `--feather 300`（F > TILE=256）时紧贴图幅边的几条会被跳过，二者不等）。
  (D) `flip_y()` 的 docstring 标明它是 TMS 扩展点：现有 `SOURCES` 两源的 `flip` 均为
      False，故该函数**运行期不可达**（静态死代码检查查不出来）。
- 【v1.7.7】修「全部失败」判据不可达（严重性低但会让告警形同虚设）：
  `build_mosaic` 里 `if n_fail == n` 永假 —— 首张瓦片由「源可达性探测」阶段取回、失败即
  `SystemExit`，故 `n_fail` 最多 `n-1`。「产物是占位灰图」的提示从未打印过。现改为按
  **真正拿到统计结果的瓦片数** `n_ok` 判断（与填充色 `global_mean` 的来源一致），
  并单列「除首张探测瓦片外全部失败」的严重告警 + 填充色值。
  另：`sys.dont_write_bytecode = True`（运行时设 `PYTHONDONTWRITEBYTECODE` 无效）。
- 【v1.7.1】修复「切影像源静默命中旧源缓存」：瓦片缓存键此前恒为 `{z}_{x}_{y}.jpg`，不区分
  影像源 —— 先用 `--source esri` 下载、再换 `google` 会直接复用 esri 的瓦片（不报错、不重下、
  内容却是错的）。现 `tile_cache_path` 接收 `source`：esri 沿用旧命名（保住已有缓存、免全量
  重下），其余源加 `{source}_` 前缀隔离；流式回读 `read_cached_tile` 同步带 source。
- 【v1.7.0】整体稳健性与一致性优化（静态审计 + 运行时 Probe 定位，共 7 项）：
  (A) **修复「静默不裁切」**：`_crop_to_bbox` 依赖全局 `_REQUEST_BBOX`，但此前只有函数内
      `global` 声明、**没有模块级初值**；且它被 try/except 包住，独立调用时 NameError 被吞，
      表现为「跑完了、文件却没裁切」。现补模块级默认值 + 支持显式传 `bbox`，两者皆空则
      **直接抛错**；异常时清理残留的 `_crop.tmp.tif`。
  (B) **修复空洞填充色被拉暗**：`compute_reference_stats` 的 `global_mean` 除数是全部瓦片数
      `len(tiles)`，把下载失败的瓦片按 0 计入 —— 实测 6/10 成功、每瓦片均值 100 时算出 60
      （被拉低 40 灰度级）。改为除以**实际有统计的瓦片数**，全失败回退中性灰 128。
  (C) **修复下载进度最后一帧永不打印**：判据 `done == n` 而 futures 只有 `n-1` 个，100% 那一帧
      永远打不出来。改为与 `total = n-1` 比较。
  (D) 清理未使用 import（`make_map.py` 的 `datetime`、一键入口的 `WGS84`）。
  (E) 修复 `make_map(pdf=True)` 与文档不符：文档说「同时输出 PDF」，实现却把 `out_path` 直接
      改成 `.pdf`，PNG 从未写出。现恒出 PNG，`--pdf` 额外出同名 PDF。
  (F) 修复 `--version` 在 `download_imagery.py` / `make_map.py` 上不可用：必填参数先触发
      argparse 报错，版本打不出来。现改为 `--version` 优先判断、再手动校验必填项。
  (G) 一键入口补齐 `--pdf` / `--boundary-lw`；`--supersample` 默认值改为运行时从
      `make_map.DEFAULT_SUPERSAMPLE` 读取（此前硬编码 2.0，与 make_map 存在漂移风险）。
  (H) **概览层按尺寸自适应**（新增 `overview_levels`）：此前三处硬编码 `[2,4,8,16,32,64]`，
      对中等/偏小输出 GDAL 会抛 `Too many overviews levels of 1x1 dimension were requested`，
      在 `_crop_to_bbox` 里会让整段裁切失效。现按「降采样后最短边 >= 64px」筛选层级。
  - 复验：离线自测（56 瓦片桩 session）「留内存 vs 流式」像元最大差 0、tags/概览/分块/e<0 全一致；
    真实端到端（瑶海区 z17，7812 瓦片）1m05s、229.30 MB、0 失败瓦片，与 v1.6.1 产物逐像素相同。
- 【v1.6.1】专题图数据来源注记微调（据用户反馈）：右下角注记由
  「数据来源：Esri World Imagery（本技能下载）」改为「**数据来源：Esri World Imagery**」，
  去掉「（本技能下载）」后缀（纯版式调整，无功能变化）。
- 【v1.6.0】性能与交互优化（全部有微基准/实测支撑）：
  (A) **修复成品 TIF 体积膨胀约 10×（严重）**：`_crop_to_bbox` 与 `reproject_to_wgs84` 用
      `meta = ds.meta.copy()` 重建文件，而 rasterio 的 `ds.meta` **不含** compress / tiled /
      photometric / block*（实测 `ds.meta.get("compress") is None`），于是成品被悄悄写成
      「无压缩 + 每行一条 strip」，且毫无报错：
        · 合成 6144×6144 样例：jpeg+tiled 2.38 MB → 重建后 143.9 MB（**膨胀 60×**），
          块结构由 (256,256) 变成 (1,5944)；
        · 真实瑶海区 z17 成品：本应约 200 MB，实际 **2.02 GB**，且 1 行 strip 令后续
          所有读取（专题图渲染 / GIS 放大）明显变慢。
      新增 `_creation_options()` 显式重建这些选项。修复后成品保持 jpeg+tiled，1 行 strip 消失。
      实测真实瑶海区 z17 成品 **2024.9 MB → 229.3 MB（8.8×，q90 默认）**／137.9 MB（14.7×，q75），
      块结构 (1,15721) → (256,256)，尺寸 / CRS / 概览层不变；相对旧成品的全分辨率再编码损失
      mean 2.96 / p99 12（/255）。
  (A2) **JPEG 质量默认提到 90**（`JPEG_QUALITY`）：本 skill 有多代编码
      （源 JPEG → 拼接写 JPEG → 裁切再写 JPEG），GDAL 默认 q75 会累积再量化误差。受控实验
      （无压缩成品做输入、只测再编码一次的损失，2048×2048）：q75 = 0.077×/mean 3.92、
      q85 = 0.101×/2.92、q90 = 0.124×/2.31（选定）、q95 = 0.167×/1.56。
  (B) **内存与面积解耦（此前文档说法不成立）**：原实现把**全部**瓦片的解码数组留在内存
      `results` 里做统计与写入，z17 瑶海区 7812 张 ≈ **1.8 GB**，z18 的 21630 张 ≈ 4.2 GB
      会触发换页。现改为：统计在工作线程内就地完成（同时被多线程摊开），并按
      `--mem-budget-mb`（默认 2048）决定是否保留数组；超预算则丢弃数组、写入阶段回读缓存。
      离线自测（桩 session，9 张瓦片）验证两条路径**输出像素完全一致（max diff 0）**、
      tags/overviews 一致。
  (C) **修 `--balance-mode offset` 的隐性错误**：原实现把 percentile 模式的 `lo`（5% 分位）
      当作「中位数」用于亮度偏移，与「仅中位数亮度偏移」的说明不符；现 `_analyze_tile`
      返回真正的子采样中位数，offset 模式按中位数偏移。
  (D) **交互优化（针对长任务「看起来不动了」）**：进度打印由「每 500 张」改为**按时间节流**
      （`--progress-sec`，默认 2s），并给出百分比 / 张每秒 / 已用 / **预计剩余**；全流程改为
      `[1/6]…[6/6]` 阶段提示（规划 / 源可达 / 下载 / 拼接写入 / 金字塔+裁切 / 完成），
      下载阶段汇总**缓存命中数**；新增 `--dry-run` **预检**（瓦片数、块尺寸、像元体量、是否超限，
      不下载）与 `--version`。
- 【v1.5.5】专题图版式微调（据用户反馈）：
  (A) 图例移到**右下角**；样块由「实心色块（面）」改为**线段**（白描边 + 彩线，与图面边界
      线型一致）—— 行政边界是「线」不是「面」。
  (B) 指北针由实心八向星改为**经典双色罗盘玫瑰**：每个星臂分「暗面 + 亮面」两个三角，
      北臂红系（暗红 + 浅红）、其余深灰 + 浅灰，外圈白底深边、中心枢轴点。
  (C) 标题贴近影像：`TOP_IN` 1.0in → 0.5in，标题改为按影像上沿 + 0.12in 定位
      （此前固定在 figure 顶部 0.99，离影像过远）。
- 【v1.5.4】专题图要素补齐（据用户反馈）：
  (A) 新增**图例**：左上角白底灰框 + 红色线段样块 + 「行政边界（XXX）」，仅当叠加了行政边界时
      绘制；新增 `--no-legend` 开关（make_map.py 与一键入口均已补齐）。
  (B) **去掉数字型比例尺**：删除「比例尺 1 : N」数字注记，仅保留图示型（直线）比例尺
      （含 0 / 全长 km 距离标注）。
  (C) **指北针改罗盘式**：由导航箭头改为经典**罗盘玫瑰**（八向星 + 外圈 + N/E/S/W 注记，
      北向 arm 红色高亮 + 中心枢轴点）；仍用 DrawingArea 绘制以保证各向同性。
- 【v1.5.3】专题图出图体验再优化：
  (A) 取消自动生成的副标题「Zoom N | Esri World Imagery | 时间」，标题区只保留主标题，
      科研配图更干净（如需副标题仍可显式 `--subtitle` 传入）。
  (B) 指北针改为简洁矢量导航箭头（实心箭头 + 竖直细杆 + 底端圆点，单色深黑、白描边），
      与 v1.5.2 双色罗盘指针区分，更显简洁专业。
"""
import argparse
import io
import os
import sys
import time
import shutil
import tempfile
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 见 make_map.py 同名说明：运行时设环境变量对 `sys.dont_write_bytecode` 无效（CPython 只在
# 启动时读），必须直接改 `sys`，否则被 import 的同级脚本仍会生成 `__pycache__`。
sys.dont_write_bytecode = True

import mercantile
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from PIL import Image
import rasterio
from rasterio.transform import from_bounds
from rasterio.windows import Window
from rasterio.enums import Resampling

# 多影像源注册表（XYZ 瓦片）。flip=True 表示该源 URL 的行号采用 TMS（自下而上），
# 需先把 XYZ 行号（y=0 在最北）翻转成 TMS 再请求。Esri World Imagery 与 Google 卫星
# 的 {y} 均为 XYZ 行号（与 mercantile 一致），故 flip=False。
# 【v1.4.4 修复】Esri 此前误设为 flip=True，导致请求到的是「关于赤道镜像」的行
# （如瑶海区实际取到了南纬约 32° 的另一个半球），影像内容与地理参考完全不符。
SOURCES = {
    "esri": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "flip": False,
        "label": "Esri World Imagery",
    },
    "google": {
        "url": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        "flip": False,
        "label": "Google Satellite (需代理/境外)",
    },
}

TILE = 256
WEB_MERCATOR = "EPSG:3857"
WGS84 = "EPSG:4326"
# 请求 bbox（由 build_mosaic 注入）。显式声明模块级默认值 None —— 此前只有函数内的
# `global _REQUEST_BBOX` 声明而没有模块级初值，一旦有人直接调用 `_crop_to_bbox()`（未先跑
# build_mosaic）就会 NameError；而它被包在 try 里，表现为「静默不裁切」而不是报错。
# 【v1.7.0 修复】给默认值，并让接收方支持显式传参，不再依赖调用顺序。
_REQUEST_BBOX = None
__version__ = "1.7.9"
DEFAULT_MAX_TILES = 60000
DEFAULT_MAX_WORKERS = 12
# 单张瓦片的解码数组（256×256×3 uint8）在内存里约为 0.2 MB。若把全部瓦片都留内存，
# 瓦片数 ×0.2MB 就是峰值增量：z17 瑶海区 7812 张 ≈ 1.8 GB，z18 的 21630 张 ≈ 4.2 GB（会换页）。
# v1.6.0 起按下面的预算决定「留内存」还是「流式回读缓存」，让内存占用与面积解耦。
TILE_BYTES = TILE * TILE * 3
DEFAULT_MEM_BUDGET_MB = 2048
# 进度刷新间隔（秒）。此前固定「每 500 张」打印一次，z18 时可能长时间无输出（用户曾反馈
# 「怎么不动了」）；改为按时间节流，长任务也不再静默。
DEFAULT_PROGRESS_SEC = 2.0
# 逐瓦片统计结果（_analyze_tile 返回）：lo/hi 为 5%/95% 分位，med 为中位数，
# mean 为子采样均值（用于空洞填充），is_blank 为纯色/空瓦片标记。
TileStat = namedtuple("TileStat", "lo hi med std is_blank mean")
# 纯色/空瓦片判定阈值：
# - std_mean < BLANK_STD：低方差纯色块（海洋/雪原等）；
# - dominant_frac > BLANK_DOMINANT：中位色附近小方块内像素占比过高（Esri "Map data not yet available" 占位图）。
BLANK_STD = 1.5
BLANK_DOMINANT = 0.90
BLANK_CUBE_HALF = 20
# JPEG 编码质量。GDAL 默认 75，但本 skill 存在「源 JPEG -> 拼接写 JPEG -> 裁切再写 JPEG」
# 的多代编码，默认质量会把再量化误差累积放大。受控实验（拿无压缩成品做输入、只测「再编码
# 一次」的损失，2048×2048 窗口）：
#   q75: 体积系数 0.077×，损失 mean 3.92 / p99 16 / max 46
#   q85: 0.101×，mean 2.92 / p99 12 / max 30
#   q90: 0.124×，mean 2.31 / p99  9 / max 26   <- 选此：损失降 41%，体积仍约为原来的 1/9
#   q95: 0.167×，mean 1.56 / p99  6 / max 19
JPEG_QUALITY = 90

# GDAL warp（3857 -> 4326 重投影）的并行线程数。
# 【v1.7.9】此前 warp 一直是单线程。注意：**光设 `GDAL_NUM_THREADS` / 用 `rasterio.Env`
# 包住是没用的** —— 实测该变量取 1/2/4/8/ALL_CPUS 时整条读取耗时都在 960~990 ms，无差异；
# 必须用 `reproject(num_threads=...)` 显式传参（rasterio 会翻译成 GDAL warp 的 NUM_THREADS）。
# 微基准（包河区 z17 出图链路，输入 4592x4848 -> 2296x2424）：
#   num_threads 默认(1) 387 ms -> 2: 212 -> 4: 126 -> 8: 107 -> 16: 87 ms，
#   且**逐像素 max|Δ| = 0**（GDAL 多线程 warp 按块独立计算，块间无共享状态）。
# 整幅重投影（23943x29724 源）收益更大，实测数字见 references/performance.md。
# 上限取 8：此时已拿到 3.6x，再往上边际收益低、却更吃内存与线程调度。
WARP_THREADS = max(1, min(os.cpu_count() or 1, 8))


def flip_y(t):
    """TMS 行号 -> Esri/XYZ 行号（从上往下）。

    【v1.7.8 标注】**当前 `SOURCES` 里两个源的 `flip` 都是 False，故本函数运行期不可达** ——
    静态查「死代码」查不出来（`download_tile` 里确实调用了它），但按现有配置它永不执行。
    保留是因为它是新增 TMS 源（`flip=True`）时 `source_cfg["flip"]` 分支的唯一实现，
    删掉会让该分支失去意义。若将来确认不再支持 TMS 源，可连同 `SOURCES[*]["flip"]`
    与 `download_tile` 里的三元表达式一并删除。
    """
    return (2 ** t.z) - 1 - t.y


def _build_session(max_workers):
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max_workers,
        pool_maxsize=max_workers,
        max_retries=0,  # 重试由 download_tile 自行控制
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (cn-satellite-imagery)"})
    return session


def tile_cache_path(cache_dir, t, source="esri"):
    """瓦片缓存路径。

    【v1.7.1】缓存键必须**区分影像源**：此前恒为 `{z}_{x}_{y}.jpg`，先用 `--source esri`
    下载、再换 `google` 会**静默命中 esri 的缓存瓦片** —— 不报错、不重下，但内容是错的
    （正是本 skill 最警惕的「不报错但结果不对」）。
    为不破坏已有 esri 缓存（避免全量重下），esri 沿用旧命名（不带前缀），
    其余源加 `{source}_` 前缀隔离。
    """
    name = (f"{t.z}_{t.x}_{t.y}.jpg" if source == "esri"
            else f"{source}_{t.z}_{t.x}_{t.y}.jpg")
    return Path(cache_dir) / name


def download_tile(t, session, timeout, retries, cache_dir, source_cfg,
                  source="esri"):
    """下载单张瓦片到缓存。返回 (tile, arr_or_None, err_or_None, from_cache)。

    - 缓存命中且可正常解码 -> 直接返回（不重下），from_cache=True；
    - 缓存损坏（解码失败）-> 删除后重新下载；
    - 网络/解码最终失败 -> arr=None，err 描述，不抛异常（避免全盘崩溃）。
    """
    flip = source_cfg["flip"]
    url = source_cfg["url"].format(z=t.z, y=flip_y(t) if flip else t.y, x=t.x)
    cache_file = tile_cache_path(cache_dir, t, source)

    # 缓存命中：校验完整性，坏缓存删掉重下
    if cache_file.exists():
        try:
            arr = np.asarray(Image.open(cache_file).convert("RGB"))
            if arr.shape[:2] != (TILE, TILE):
                raise ValueError("bad shape")
            return t, arr, None, True
        except Exception:
            try:
                cache_file.unlink()
            except OSError:
                pass

    last = None
    for _ in range(retries):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            data = r.content
            try:
                arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
                if arr.shape[:2] != (TILE, TILE):
                    raise ValueError("bad shape")
                try:
                    cache_file.write_bytes(data)
                except OSError:
                    pass
                return t, arr, None, False
            except Exception as e:  # 下到内容但解码失败（极少见）
                return t, None, f"decode error: {e}", False
        except Exception as e:  # 网络异常
            last = e
    return t, None, f"http failed after {retries} retries: {last}", False


def read_cached_tile(cache_dir, t, source="esri"):
    """流式模式下从缓存回读单张瓦片（内存不足时的写回路径）。失败返回 None。"""
    p = tile_cache_path(cache_dir, t, source)
    try:
        arr = np.asarray(Image.open(p).convert("RGB"))
        return arr if arr.shape[:2] == (TILE, TILE) else None
    except Exception:
        return None


def _fetch_tile(t, session, timeout, retries, cache_dir, source_cfg,
                keep_array, need_percentiles, source="esri"):
    """并发工作单元：下载/解码 + **就地统计**（v1.6.0）。

    返回 (tile, arr_or_None, err_or_None, stat_or_None, from_cache)：
    - 统计就在工作线程里算完，写入阶段只做汇总，不再串行遍历所有瓦片数组
      （同时也让统计计算被多线程摊开）；
    - `keep_array=False` 时丢弃解码数组（arr=None 但 err=None，表示「已下载、需回读缓存」），
      把峰值内存从「瓦片数 × 0.2 MB」降到「线程数 × 0.2 MB」。
    """
    t, arr, err, from_cache = download_tile(t, session, timeout, retries,
                                            cache_dir, source_cfg, source)
    if arr is None:
        return t, None, err, None, from_cache
    st = _analyze_tile(arr, need_percentiles=need_percentiles)
    return t, (arr if keep_array else None), err, st, from_cache


def _fmt_dur(sec):
    """把秒数格式化为 12s / 3m05s / 1h02m，用于进度与 ETA。"""
    try:
        sec = max(0, int(round(sec)))
    except Exception:
        return "?"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _creation_options(ds, width, height):
    """从已打开的数据集提取「创建选项」，用于原地重建文件时保持压缩 / 分块 / 色彩空间。

    【v1.6.0 修复】rasterio 的 `ds.meta` **只含几何与波段信息**，不含 `compress` /
    `tiled` / `photometric` / `blockxsize` / `interleave`（实测 `ds.meta.get("compress")
    is None`）。因此 `meta = ds.meta.copy(); meta.update(...)` 这种「原地重建」写法会把
    成品悄悄退化成「无压缩 + 每行一条 strip」，且毫无报错：
      - 合成 6144×6144 样例：jpeg+tiled 2.38 MB  →  重建后 **143.9 MB（膨胀 60×）**，
        块结构从 (256,256) 变成 (1,5944)；
      - 真实瑶海区 z17 成品：本应约 200 MB，实际 **2.02 GB**（膨胀约 10×），
        1 行 strip 还让后续所有读取（专题图渲染 / GIS 放大）明显变慢。
    受影响的是 `_crop_to_bbox` 与 `reproject_to_wgs84` 两个「读出来再写回去」的环节。
    """
    opts = {"tiled": True, "interleave": "pixel"}
    shape = ds.block_shapes[0] if ds.block_shapes else (256, 256)
    # JPEG 要求块边长为 16 的倍数：沿用源块尺寸，非法则回退 256
    bx = shape[1] if shape[1] and shape[1] % 16 == 0 else 256
    by = shape[0] if shape[0] and shape[0] % 16 == 0 else 256
    opts["blockxsize"], opts["blockysize"] = bx, by
    comp = ds.compression
    if comp:
        opts["compress"] = comp
        # 注意：ds.compression 返回的是 rasterio 的 Compression **枚举**（值形如 'JPEG' 大写），
        # 不是字符串 'jpeg'。此处必须按 value 比较，否则 jpeg_quality 永远传不进去（已实测踩过）。
        if str(getattr(comp, "value", comp)).lower() == "jpeg":
            opts["jpeg_quality"] = JPEG_QUALITY
    try:
        pv = getattr(ds.photometric, "name", ds.photometric)
        pv = str(pv).lower() if pv is not None else ""
        if pv == "ycbcr":
            opts["photometric"] = "YCbCr"
        elif pv == "rgb":
            opts["photometric"] = "RGB"
    except Exception:
        pass
    opts["bigtiff"] = (width * height * 3 > 2_000_000_000)
    return opts


def overview_levels(width, height, min_side=64, max_level=64):
    """按输出尺寸挑选合适的概览层（2 的幂），避免对小图请求过多层级。

    【v1.7.0】此前全脚本硬编码 `[2,4,8,16,32,64]`。对很小的输出（如 bbox 框到几像素、
    或重投影后长宽骤缩）GDAL 会抛 `Too many overviews levels of 1x1 dimension were requested`；
    在 `_crop_to_bbox` 里这个异常会让整段裁切被 except 吞掉 —— 表现为「裁切静默不生效」。
    现一律按「降采样后最短边仍 >= min_side」筛选层级，空列表则调用方跳过建概览。
    """
    levels = []
    lv = 2
    while lv <= max_level:
        if width // lv >= min_side and height // lv >= min_side:
            levels.append(lv)
        else:
            break
        lv *= 2
    return levels


def _crop_to_bbox(src_path, bbox=None):
    """把已写好的 3857 大块精确裁切到请求 bbox（WGS84）。原地覆盖。

    bbox 显式传入；省略时回退到模块 `_REQUEST_BBOX`（由 build_mosaic 注入）。
    【v1.7.0】两者都为空时**直接抛错**而不是静默返回 —— 此前只有 global 声明没有模块级
    初值，独立调用会 NameError，却被 try/except 吞掉变成「看起来跑过了、实际没裁切」。
    """
    if bbox is None:
        bbox = _REQUEST_BBOX
    if bbox is None:
        raise ValueError("缺少裁切 bbox：请显式传入，或先用 build_mosaic 设置 _REQUEST_BBOX")
    crop_path = None
    try:
        with rasterio.open(src_path) as ds:
            west, south, east, north = bbox
            res = ds.res
            xmin, ymin = mercantile.xy(west, south)
            xmax, ymax = mercantile.xy(east, north)
            col_min = max(0, int((xmin - ds.bounds.left) / res[0]))
            col_max = min(ds.width, int((xmax - ds.bounds.left) / res[0]) + 1)
            row_min = max(0, int((ds.bounds.top - ymax) / abs(res[1])))
            row_max = min(ds.height, int((ds.bounds.top - ymin) / abs(res[1])) + 1)
            if col_min <= 0 and row_min <= 0 and col_max >= ds.width and row_max >= ds.height:
                return  # 已是精确范围
            win = Window(col_min, row_min, col_max - col_min, row_max - row_min)
            data = ds.read(window=win)
            new_left = ds.bounds.left + col_min * res[0]
            new_top = ds.bounds.top - row_min * abs(res[1])
            new_w = col_max - col_min
            new_h = row_max - row_min
            # from_bounds(left, bottom, right, top, w, h)：new_top 是北边界(top)，
            # new_top - new_h*res 才是南边界(bottom)。此前两者写反，导致裁切后的成品
            # geotransform 南北颠倒（e>0），即「像元数据正确但地理参考错位」。
            new_transform = from_bounds(new_left, new_top - new_h * abs(res[1]),
                                       new_left + new_w * res[0], new_top, new_w, new_h)
            meta = ds.meta.copy()
            meta.update(height=new_h, width=new_w, transform=new_transform,
                        **_creation_options(ds, new_w, new_h))
            tags = ds.tags()
            # 【v1.7.8】用 splitext 而非 `src_path[:-4]`：后者假定扩展名恒为 4 字符，
            # 传入 `.tiff`（5 字符）会把文件名尾部一起切掉。
            crop_path = os.path.splitext(src_path)[0] + "_crop.tmp.tif"
            with rasterio.open(crop_path, "w", **meta) as dst:
                dst.write(data)
                levels = overview_levels(new_w, new_h)
                if levels:
                    dst.build_overviews(levels, resampling=Resampling.average)
                if tags:
                    dst.update_tags(ns="", **tags)
        os.replace(crop_path, src_path)
    except Exception as e:  # noqa: BLE001
        # 失败时别留下 .tmp.tif 垃圾文件（此前会残留到下次运行）
        try:
            if crop_path and os.path.exists(crop_path):
                os.remove(crop_path)
        except OSError:
            pass
        sys.stderr.write(f"  裁切跳过（保留整块）: {e}\n")


def reproject_to_wgs84(src_path):
    """把 3857 结果重投影到 WGS84（EPSG:4326），原地覆盖。"""
    from rasterio.warp import reproject, calculate_default_transform
    with rasterio.open(src_path) as src:
        transform, w, h = calculate_default_transform(
            src.crs, WGS84, src.width, src.height, *src.bounds)
        meta = src.meta.copy()
        meta.update(crs=WGS84, transform=transform, width=w, height=h,
                    **_creation_options(src, w, h))
        tags = src.tags()
        # 【v1.7.8】同上：splitext 而非 `[:-4]`（`.tiff` 会被切错）
        dst_path = os.path.splitext(src_path)[0] + "_4326.tmp.tif"
        with rasterio.open(dst_path, "w", **meta) as dst:
            # 三波段一次性重投影（比逐波段循环更紧凑、少一次边界计算）
            reproject(
                source=rasterio.band(src, (1, 2, 3)),
                destination=rasterio.band(dst, (1, 2, 3)),
                src_crs=src.crs, dst_crs=WGS84,
                src_transform=src.transform, dst_transform=transform,
                resampling=Resampling.bilinear,
                # 【v1.7.9】多线程 warp（此前单线程）。整幅重投影（2.4 万 x 3 万 源）
                # 是这条链路里最吃 CPU 的一步，收益见 references/performance.md；
                # 结果与单线程逐像素一致（max|Δ| = 0）。
                num_threads=WARP_THREADS,
            )
            if tags:
                dst.update_tags(ns="", **tags)
            # 重投影后重建概览金字塔，否则 4326 成品在 GIS 软件中放大很慢（3857 成品由 _crop 阶段已建）
            levels = overview_levels(w, h)
            if levels:
                dst.build_overviews(levels, resampling=Resampling.average)
    # 用 os.replace 原子替换，绕过部分运行时注入的 safe-delete hook 对 os.remove 的拦截问题
    os.replace(dst_path, src_path)


def _analyze_tile(arr, need_percentiles=True):
    """返回 TileStat(lo, hi, med, std, is_blank, mean)，用于匀色、空瓦片判定与全局均值。

    优化（v1.4.2）：
    - 对大瓦片做 4×4 子采样：统计只需分布特征，无需逐像素，且对 std / 中位色占比判定无影响；
    - need_percentiles=False（none 匀色模式仅做空白检测）时**跳过两次百分位计算**。
      这是 z18 量级统计耗时的主因——默认模式此前仍对每张瓦片做两次 O(n) 选择排序，纯属浪费。

    优化（v1.4.3）：global_mean 复用此处已算出的子采样均值 mean，不再对整张 256×256 瓦片
    做全分辨率 `arr.reshape(-1,3).mean(0)`（65536 像素）。空洞填充只需要一个粗略均值，
    4096 像素子采样均值与全分辨率均值在小数位内一致。

    优化（v1.6.0）：同时返回 `med`（子采样中位数，本函数本来就要算它做空白判定），
    使 `offset` 匀色模式可以用真正的**中位数**——此前 offset 模式复用的是 percentile 模式的
    `lo`（5% 分位数），与「按中位数亮度偏移」的说明不符（隐性 bug，已修）。
    """
    h, w = arr.shape[0], arr.shape[1]
    if h > 64 or w > 64:
        p = arr[::4, ::4].reshape(-1, 3).astype(np.float64)
    else:
        p = arr.reshape(-1, 3).astype(np.float64)
    pmean = p.mean(0)
    std = float(p.std(0).mean())
    if need_percentiles:
        # 【v1.7.8】一次调用取 3 个分位（含中位数），省掉原先单独那一次 np.median 的
        # 完整 partition。微基准（4096 点子采样）：536 → 363 µs/瓦片（-32%），
        # 三个值逐位一致（Δ=0）。**注意只在 percentile 模式生效** —— 默认的 none 模式
        # 根本不调分位，实测收益 0.2%（无可优化空间）。
        lo, med, hi = np.percentile(p, (5.0, 50.0, 95.0), axis=0)
        hi = np.where(hi - lo < 1e-6, lo + 1.0, hi)
    else:
        lo = hi = None
        med = np.median(p, axis=0)
    within = float(np.all(np.abs(p - med) <= BLANK_CUBE_HALF, axis=1).mean())
    is_blank = std < BLANK_STD or within > BLANK_DOMINANT
    return TileStat(lo=lo, hi=hi, med=med, std=std, is_blank=is_blank, mean=pmean)


def compute_reference_stats(stats, tiles, mode="none"):
    """汇总逐瓦片统计（`stats`: tile -> TileStat），同时标记纯色/空瓦片。

    返回 (ref_stats, per_tile, blank_flags, global_mean)：
    - ref_stats: percentile 模式为 (low,high) 全局参考；offset 模式为 (median,None)；
      none 模式为 None。
    - per_tile: 与 tiles 对齐，成功且非空白瓦片为匀色参考元组，否则 None。
    - blank_flags: 与 tiles 对齐，纯色/空瓦片为 True（不参与匀色参考）。
    - global_mean: 所有**成功**瓦片的逐通道均值（用于空洞填充）。

    优化（v1.4.2）：不再重新打开并解码缓存 JPG（此前每张瓦片被读磁盘两次）。
    优化（v1.6.0）：统计输入改为**逐瓦片统计结果**（在下载线程里就地算完），
    `_analyze_tile` 不再被串行二次调用；本函数退化为纯汇总，几乎零成本。

    【v1.7.0 修复】global_mean 的除数此前是 `len(tiles)`（**全部**瓦片数），把下载失败的
    瓦片按 0 计入均值，导致填充色被拉暗 —— 实测 6/10 成功、每瓦片均值 100 时算出 60
    （被拉低 40 灰度级）。现改为除以**实际有统计的瓦片数**；全失败时回退中性灰 128。
    """
    per_tile = []
    blank_flags = []
    acc0 = np.zeros(3, np.float64)
    acc1 = np.zeros(3, np.float64)
    acc_med = np.zeros(3, np.float64)
    n_valid = 0
    n_med = 0
    global_mean = np.zeros(3, np.float64)
    n_stats = 0

    for t in tiles:
        st = stats.get(t)
        if st is None:
            per_tile.append(None)
            blank_flags.append(False)
            continue
        blank_flags.append(st.is_blank)
        if mode == "percentile" and not st.is_blank:
            per_tile.append((st.lo, st.hi))
            acc0 += st.lo
            acc1 += st.hi
            n_valid += 1
        elif mode == "offset" and not st.is_blank:
            per_tile.append((st.med, None))
            acc_med += st.med
            n_med += 1
        else:
            per_tile.append(None)
        global_mean += st.mean
        n_stats += 1

    # 只用「有统计结果」的成功瓦片求均值；全失败时回退中性灰，避免填充成黑块
    global_mean = (global_mean / n_stats if n_stats
                   else np.array([128.0, 128.0, 128.0]))
    if mode == "none":
        ref_stats = None
    elif mode == "offset":
        ref_stats = (acc_med / n_med if n_med else global_mean, None)
    elif n_valid > 0:
        ref_stats = (acc0 / n_valid, acc1 / n_valid)
    else:  # 全部空白的退化情况
        ref_stats = (global_mean, global_mean + 1.0)
    return ref_stats, per_tile, blank_flags, global_mean


def _feather_strip_v(arr, F):
    """垂直拼缝（沿宽度轴 2F 像素）交叉淡化，使用边缘复制合成重叠。"""
    left = np.empty_like(arr)
    right = np.empty_like(arr)
    left[:, :F, :] = arr[:, :F, :]
    left[:, F:, :] = arr[:, F - 1:F, :]
    right[:, F:, :] = arr[:, F:, :]
    right[:, :F, :] = arr[:, F:F + 1, :]
    w = (np.arange(2 * F, dtype=np.float32) / (2 * F - 1))[None, :, None]
    return (1.0 - w) * left + w * right


def _feather_strip_h(arr, F):
    """水平拼缝（沿高度轴 2F 像素）交叉淡化。"""
    left = np.empty_like(arr)
    right = np.empty_like(arr)
    left[:F, :, :] = arr[:F, :, :]
    left[F:, :, :] = arr[F - 1:F, :, :]
    right[F:, :, :] = arr[F:, :, :]
    right[:F, :, :] = arr[F:F + 1, :, :]
    w = (np.arange(2 * F, dtype=np.float32) / (2 * F - 1))[:, None, None]
    return (1.0 - w) * left + w * right


def feather_mosaic(out_path, x0, y0, x1, y1, F):
    """在已写好的 GeoTIFF 上，对瓦片网格的内部边界做交叉淡化（消拼缝）。

    内存轻量：每条拼缝只读/写一条 2F 宽的窄带（薄 strip），与总面积无关。
    仅处理严格落在图内的内部拼缝（外部边界无邻居，跳过）。
    """
    nx = x1 - x0 + 1
    ny = y1 - y0 + 1
    if F <= 0 or (nx < 2 and ny < 2):
        return
    # 【v1.7.8】统计**实际处理**的缝数，而不是理论值 `nx-1`/`ny-1`。两者在
    # `F <= TILE`（正常情形）下相等；但当用户给了 `--feather 300`（F > TILE=256）时，
    # 紧贴图幅边的那几条缝会因 `C-F < 0` / `C+F > W` 被 `continue` 跳过，
    # 打印的理论值就会比实际处理数偏大（又一次「报数与实际不符」）。
    n_v = n_h = 0
    with rasterio.open(out_path, "r+") as ds:
        H, W = ds.height, ds.width
        for i in range(1, nx):
            C = i * TILE
            if C - F < 0 or C + F > W:
                continue
            win = Window(C - F, 0, 2 * F, H)
            arr = ds.read(window=win).transpose(1, 2, 0).astype(np.float32)
            out = _feather_strip_v(arr, F)
            ds.write(out.transpose(2, 0, 1).astype(np.uint8), window=win)
            n_v += 1
        for j in range(1, ny):
            R = j * TILE
            if R - F < 0 or R + F > H:
                continue
            win = Window(0, R - F, W, 2 * F)
            arr = ds.read(window=win).transpose(1, 2, 0).astype(np.float32)
            out = _feather_strip_h(arr, F)
            ds.write(out.transpose(2, 0, 1).astype(np.uint8), window=win)
            n_h += 1
    print(f"消拼缝完成（feather={F}px，处理 {n_v} 条纵缝 / {n_h} 条横缝）")


def build_mosaic(bbox, zoom, out_path, session, timeout, retries, max_workers,
                 cache_dir, max_tiles, force, region_meta=None,
                 balance_mode="none", feather=0, source="esri",
                 fill_holes=True, fill_blanks=False,
                 mem_budget_mb=DEFAULT_MEM_BUDGET_MB,
                 progress_sec=DEFAULT_PROGRESS_SEC):
    global _REQUEST_BBOX
    _REQUEST_BBOX = bbox
    _T0 = time.time()
    cache_dir = Path(cache_dir)      # 容忍调用方传 str
    source_cfg = SOURCES.get(source, SOURCES["esri"])
    west, south, east, north = bbox
    tiles = list(mercantile.tiles(west, south, east, north, zoom))
    n = len(tiles)
    if n == 0:
        raise SystemExit(f"ERROR: 该范围在 zoom={zoom} 下没有覆盖瓦片")
    if n > max_tiles and not force:
        raise SystemExit(
            f"ERROR: 瓦片数 {n} 超过安全上限 {max_tiles}（zoom={zoom} 对大面积区域过大）。\n"
            f"        请换更小的区划（区县/乡镇级），或降低 --zoom，或加 --force 强制下载。")
    xs = [t.x for t in tiles]
    ys = [t.y for t in tiles]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    width = (x1 - x0 + 1) * TILE
    height = (y1 - y0 + 1) * TILE
    # 整块地理范围 = 最北行/最西列角瓦片(x0,y0) 的左上角 ~ 最南行/最东列角瓦片(x1,y1) 的右下角。
    # y0 是最小 y（最北），y1 是最大 y（最南）。此前用 ul((x0,y1)) / ul((x1+1,y0-1)) 会把
    # 上下边各错 1 个瓦片（约 256px），并与下面的行偏移叠加，造成整幅影像垂直翻转——v1.4.4 修复。
    b_nw = mercantile.xy_bounds(mercantile.Tile(x0, y0, zoom))
    b_se = mercantile.xy_bounds(mercantile.Tile(x1, y1, zoom))
    transform = from_bounds(b_nw.left, b_se.bottom, b_se.right, b_nw.top, width, height)

    bigtiff = (width * height * 3 > 2_000_000_000)   # 用「字节数」判定（原用像素数，3 波段下偏小）
    out_meta = dict(driver="GTiff", height=height, width=width, count=3, dtype="uint8",
                    crs=WEB_MERCATOR, transform=transform, compress="jpeg",
                    photometric="YCbCr", tiled=True, bigtiff=bigtiff,
                    jpeg_quality=JPEG_QUALITY)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 内存预算：解码数组是否全量留内存（见 DEFAULT_MEM_BUDGET_MB 说明）
    keep_array = (n * TILE_BYTES) <= mem_budget_mb * 1_000_000
    need_pct = (balance_mode != "none")
    mode_note = (f"全量留内存（约 {n * TILE_BYTES / 1e6:.0f} MB）" if keep_array
                 else f"流式（峰值约 {max_workers * TILE_BYTES / 1e6:.0f} MB，写入阶段回读缓存）")
    print(f"[1/6] 规划: 瓦片 {n} 张，块 {width}x{height} px，"
          f"原始像元约 {width * height * 3 / 1e6:.0f} MB；内存策略 = {mode_note}")

    # 2) 源可达性探测：先试首张瓦片，失败立即报错退出，避免不可达源卡满超时
    _probe = _fetch_tile(tiles[0], session, timeout, 1, cache_dir, source_cfg,
                         keep_array, need_pct, source)
    if _probe[1] is None and _probe[2] is not None:
        raise SystemExit(
            f"ERROR: 影像源「{source_cfg['label']}」不可达（首瓦片下载失败：{_probe[2]}）。\n"
            f"        请检查网络/代理，或改用默认 --source esri。")
    print(f"[2/6] 影像源可达（{source_cfg['label']}）")

    t0 = time.time()

    # 3) 并发下载（落缓存）+ 就地统计。失败瓦片返回 None，不抛异常。
    #    探测已成功下载并解码首张瓦片，直接复用其结果，避免重复下载 + 重复解码。
    results = {tiles[0]: (_probe[1], _probe[2])}
    stats = {tiles[0]: _probe[3]}
    n_fail = 0
    n_cached = 1 if _probe[4] else 0
    total = n - 1   # 首张瓦片已由可达性探测阶段取回
    print(f"[3/6] 并发下载 {total} 张（{max_workers} 线程）…")
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_tile, t, session, timeout, retries, cache_dir,
                             source_cfg, keep_array, need_pct, source): t for t in tiles[1:]}
        done = 0
        next_report = time.time() + progress_sec
        for fut in as_completed(futures):
            t, arr, err, st, from_cache = fut.result()  # 不会抛
            results[t] = (arr, err)
            stats[t] = st
            if err is not None:
                n_fail += 1
            if from_cache:
                n_cached += 1
            done += 1
            now = time.time()
            # 【v1.7.0 修复】判据此前是 `done == n`，而 futures 只有 n-1 个 —— 100% 那一帧
            # 永远打不出来（只剩靠时间节流偶然命中）。改为与总数 total 比较。
            if done == total or now >= next_report:
                next_report = now + progress_sec
                el = now - t0
                rate = done / el if el > 0 else 0.0
                eta = (total - done) / rate if rate > 0 else 0.0
                print(f"  下载 {done}/{total} ({done / max(1, total):5.1%})  "
                      f"{rate:5.1f} 张/s  已用 {_fmt_dur(el)}  剩余≈{_fmt_dur(eta)}"
                      + (f"  失败 {n_fail}" if n_fail else ""))
    t_dl = time.time() - t0
    if n_fail:
        print(f"警告: {n_fail}/{n} 个瓦片下载失败，将用全局均值填充（黑块处）。")
    print(f"  下载阶段完成：用时 {_fmt_dur(t_dl)}，缓存命中 {n_cached}/{n}")

    # 4) 匀色参考统计 + 空白瓦片标记（统计已在工作线程算完，这里只做汇总）
    ref_stats, per_tile, blank_flags, global_mean = compute_reference_stats(
        stats, tiles, mode=balance_mode)

    # 【v1.7.7 修复】判据此前是 `n_fail == n`，但 `n_fail` 最多只到 `n-1` —— 首张瓦片由
    # 「源可达性探测」阶段取回（失败会直接 SystemExit），所以这条「全部失败」分支**永不可达**，
    # 「产物是占位灰图」的提示形同虚设。现改为按「真正拿到统计结果的瓦片数」判断，与填充色
    # 的来源（`global_mean` 只统计成功瓦片）保持一致；并单列「除探测瓦片外全军覆没」的告警。
    n_ok = sum(1 for t in tiles if stats.get(t) is not None)
    if n_ok == 0:                      # 兜底：一个可用均值都没有，用中性灰避免纯黑图
        fill_value = np.array([128, 128, 128], dtype=np.uint8)
        print("严重警告: 全部瓦片下载失败，产物为占位灰图（请检查网络 / 影像源 --source）。")
    else:
        fill_value = np.clip(global_mean, 0, 255).astype(np.uint8)
        if n > 1 and n_fail >= n - 1:
            print(f"严重警告: 除首张探测瓦片外 {n_fail} 张全部下载失败，图面绝大部分是均值填充色"
                  f"（{fill_value.tolist()}）—— 请检查网络 / 影像源 --source 后重跑（瓦片缓存可复用）。")
    fill_img = np.tile(fill_value, (TILE, TILE, 1))

    n_blank = sum(1 for b in blank_flags if b)
    if n_blank:
        print(f"提示: 检测到 {n_blank} 个纯色/空瓦片（多为无数据区），"
              + ("已用均值填充。" if fill_blanks else "保留原色（加 --fill-blanks 可填充）。"))

    # 5) 写入 + 匀色（逐瓦片读取、归一化、落盘；失败/空白按需填充）
    print("[4/6] 拼接写入…")
    t1 = time.time()
    holes = 0
    n_refetch = 0
    with rasterio.open(out_path, "w", **out_meta) as dst:
        for idx, t in enumerate(tiles):
            arr, err = results[t]
            if err is not None:  # 下载失败
                if fill_holes:
                    dst.write(fill_img.transpose(2, 0, 1),
                              window=Window((t.x - x0) * TILE, (t.y - y0) * TILE, TILE, TILE))
                    holes += 1
                continue
            if blank_flags[idx] and fill_blanks:  # 纯色/空瓦片
                dst.write(fill_img.transpose(2, 0, 1),
                          window=Window((t.x - x0) * TILE, (t.y - y0) * TILE, TILE, TILE))
                holes += 1
                continue
            if arr is None:  # 流式模式：未保留数组，从缓存回读
                arr = read_cached_tile(cache_dir, t, source)
                if arr is None:
                    if fill_holes:
                        dst.write(fill_img.transpose(2, 0, 1),
                                  window=Window((t.x - x0) * TILE, (t.y - y0) * TILE, TILE, TILE))
                        holes += 1
                    continue
                n_refetch += 1
            if balance_mode != "none" and per_tile[idx] is not None:
                a, b = per_tile[idx]
                if balance_mode == "percentile":
                    lo, hi = a, b
                    ref_lo, ref_hi = ref_stats
                    scale = (ref_hi - ref_lo) / (hi - lo)
                    arr = (arr.astype(np.float32) - lo) * scale + ref_lo
                elif balance_mode == "offset":
                    ref_med = ref_stats[0]
                    arr = arr.astype(np.float32) + (ref_med - a)  # a = 本瓦片中位数（v1.6.0 修正）
                arr = np.clip(arr, 0, 255)
            arr = arr.astype(np.uint8)
            col_off = (t.x - x0) * TILE
            row_off = (t.y - y0) * TILE
            dst.write(arr.transpose(2, 0, 1), window=Window(col_off, row_off, TILE, TILE))
        tags_to_write = dict(region_meta) if region_meta else {}
        tags_to_write["source"] = source_cfg["label"]
        # 【v1.7.9】bbox 模式（不经一键入口，region_meta=None）此前**不写 zoom 标签** ——
        # 但 `--zoom` 正是本次下载的核心参数，且文档把 zoom 列为血缘标签之一，导致
        # 「同一产物经 ② bbox 入口产出就没有 zoom、经 ① 一键入口产出就有」的不一致。
        # 用 setdefault：一键入口已在 region_meta 里写过同值项，不去覆盖它。
        tags_to_write.setdefault("zoom", str(zoom))
        tags_to_write["failed_tiles"] = str(n_fail)
        tags_to_write["blank_tiles"] = str(n_blank)
        tags_to_write["filled_holes"] = str(holes)
        tags_to_write["skill_version"] = __version__
        dst.update_tags(ns="", **tags_to_write)
    if n_refetch:
        print(f"  流式回读 {n_refetch} 张（内存预算不足，走缓存）")
    print(f"  写入完成，用时 {_fmt_dur(time.time() - t1)} -> {os.path.basename(out_path)}")

    # 6) 消拼缝（在整块上做边界交叉淡化，再裁切）
    if feather and feather > 0:
        feather_mosaic(out_path, x0, y0, x1, y1, int(feather))

    # 7) 概览金字塔（feather 之后再建，保证低 zoom 也不见缝）+ 精确裁切
    with rasterio.open(out_path, "r+") as dst:
        levels = overview_levels(width, height)
        if levels:
            dst.build_overviews(levels, resampling=Resampling.average)
    _crop_to_bbox(out_path, bbox)
    print(f"[5/6] 概览金字塔 + 精确裁切完成")
    print(f"[6/6] 全部完成，总用时 {_fmt_dur(time.time() - _T0)}")
    return out_path


def plan_tiles(bbox, zoom, max_tiles=DEFAULT_MAX_TILES):
    """预检：不下载，先算出瓦片数 / 块尺寸 / 是否需要 --force（供 --dry-run 与确认）。"""
    tiles = list(mercantile.tiles(bbox[0], bbox[1], bbox[2], bbox[3], zoom))
    if not tiles:
        return dict(tiles=0, width=0, height=0, over_limit=False)
    xs = [t.x for t in tiles]
    ys = [t.y for t in tiles]
    return dict(tiles=len(tiles),
                width=(max(xs) - min(xs) + 1) * TILE,
                height=(max(ys) - min(ys) + 1) * TILE,
                over_limit=(len(tiles) > max_tiles))


def print_plan(bbox, zoom, max_tiles=DEFAULT_MAX_TILES, prefix=""):
    """打印一行预检结论（--dry-run 与一键入口共用）。"""
    p = plan_tiles(bbox, zoom, max_tiles)
    if p["tiles"] == 0:
        print(f"{prefix}预检 zoom={zoom}: 该范围没有覆盖瓦片。")
        return p
    print(f"{prefix}预检 zoom={zoom}: 瓦片 {p['tiles']} 张 → 块 {p['width']}x{p['height']} px，"
          f"像元约 {p['width'] * p['height'] * 3 / 1e6:.0f} MB"
          + ("（超过 --max-tiles 上限，需 --force）" if p["over_limit"] else ""))
    return p


def main():
    ap = argparse.ArgumentParser(description="bbox -> 卫星影像 GeoTIFF")
    # 必填项在 argparse 里置为可选，等 --version 判断完再手动校验 —— 否则
    # `xxx.py --version` 会先被「缺少 --bbox/--zoom/--output」挡住，打印不了版本。
    ap.add_argument("--bbox", help="west,south,east,north（WGS84 度）")
    ap.add_argument("--zoom", type=int)
    ap.add_argument("--output")
    ap.add_argument("--source", default="esri", choices=list(SOURCES.keys()),
                    help="影像源（默认 esri，可选 google 但国内需代理）")
    ap.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    ap.add_argument("--timeout", type=float, default=15,
                    help="单瓦片下载超时秒（默认 15，失败快速跳过）")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--epsg", type=int, default=3857, help="3857 或 4326")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--clean-cache", action="store_true")
    ap.add_argument("--balance-mode", default="none",
                    choices=["percentile", "offset", "none"],
                    help="匀色策略：none=关闭（默认，保留原片色彩），offset=仅中位数亮度偏移，percentile=5/95%%线性拉伸（可能放大色差，谨慎使用）")
    ap.add_argument("--no-balance", action="store_true", help="关闭匀色（等价 --balance-mode none）")
    ap.add_argument("--feather", type=int, default=0,
                    help="拼缝淡化像素宽度（0=关闭，建议 16~32）")
    ap.add_argument("--fill-holes", dest="fill_holes", action="store_true", default=True,
                    help="下载失败的瓦片用全局均值填充（默认开，避免黑块）")
    ap.add_argument("--no-fill-holes", dest="fill_holes", action="store_false",
                    help="关闭空洞填充（失败瓦片留黑，用于标记缺失）")
    ap.add_argument("--fill-blanks", action="store_true",
                    help="纯色/空瓦片（无数据区）也用均值填充")
    ap.add_argument("--mem-budget-mb", type=int, default=DEFAULT_MEM_BUDGET_MB,
                    help=f"解码瓦片数组的内存预算 MB（默认 {DEFAULT_MEM_BUDGET_MB}）："
                         f"超出预算则改为流式（写入时回读缓存），峰值内存与面积解耦")
    ap.add_argument("--progress-sec", type=float, default=DEFAULT_PROGRESS_SEC,
                    help=f"下载进度刷新间隔秒（默认 {DEFAULT_PROGRESS_SEC}，按时间节流避免长任务静默）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预检瓦片数/体量/是否超限，不下载")
    ap.add_argument("--version", action="store_true", help="打印版本")
    args = ap.parse_args()

    if args.version:
        print("cn-satellite-imagery / download_imagery", __version__)
        return
    if not (args.bbox and args.zoom is not None and args.output):
        ap.error("--bbox / --zoom / --output 均为必填（--version 除外）")

    HERE = os.path.dirname(os.path.abspath(__file__))
    bbox = [float(v) for v in args.bbox.split(",")]
    if args.dry_run:
        print_plan(bbox, args.zoom, args.max_tiles, prefix="[dry-run] ")
        return
    cache_dir = Path(HERE) / "_tiles"
    if args.clean_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)
    if args.no_cache:
        cache_dir = Path(tempfile.mkdtemp(prefix="esri_"))

    session = _build_session(args.max_workers)
    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    mode = "none" if args.no_balance else args.balance_mode
    result = build_mosaic(bbox, args.zoom, out, session, args.timeout, args.retries,
                          args.max_workers, cache_dir, args.max_tiles, args.force,
                          region_meta=None, balance_mode=mode, feather=args.feather,
                          source=args.source, fill_holes=args.fill_holes,
                          fill_blanks=args.fill_blanks,
                          mem_budget_mb=args.mem_budget_mb,
                          progress_sec=args.progress_sec)
    if args.epsg == 4326:
        reproject_to_wgs84(result)
    print("OK:", result)


if __name__ == "__main__":
    main()
