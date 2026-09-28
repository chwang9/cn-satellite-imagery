#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把卫星影像 GeoTIFF 渲染成 ArcGIS 风格的「遥感影像专题地图」。

在内存中把影像重投影到 EPSG:4326（经纬度），叠加：
  - 标题 / 副标题
  - 指北针（右上角罗盘玫瑰，含 N/E/S/W）
  - 图示比例尺（**固定左下角**，分段交替黑白 + 逐段距离标注，带白色衬底面板；
    v1.5.4 起已去掉 1:N 数字比例尺）
  - 经纬度经纬网（DMS 标注，类似 ArcGIS graticule）
  - 图例（叠加行政边界时显示；**固定右下角**，白底面板保证区界线不干扰阅读）
  - 数据来源注记（取自 GeoTIFF 血缘标签的 `source`，不再写死 Esri）

产物为 PNG（默认）或 PDF。既可作为独立命令运行，也可被 satellite_imagery_cn.py 的
--map 调用，一键下载后直接出专题图。

依赖（Windows 隔离环境）:
  C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
需要: rasterio matplotlib numpy
"""
import os
import re
import sys
import math
import json
import time
import argparse

# 避免本 skill 脚本产生 __pycache__（Skill 包仅支持两级目录结构）。
# 【v1.7.7】运行时设 `os.environ["PYTHONDONTWRITEBYTECODE"]` **无效** —— CPython 只在
# 解释器启动时读该环境变量来初始化 `sys.dont_write_bytecode`，必须直接改 `sys`，
# 对「本进程之后导入的模块」才生效（被 import 的同级脚本才会生成 .pyc，正是要挡掉的）。
# 【v1.7.8】删掉遗留的那行 `os.environ[...] = "1"`：它紧挨着上面这段「无效」的说明却仍在
# 执行，是自相矛盾的死代码（真正一直起作用的只有下面这行）。
sys.dont_write_bytecode = True

# 同目录脚本（resolve_region）复用 GCJ-02→WGS84 反算
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np
import rasterio
from rasterio.warp import reproject, calculate_default_transform
from rasterio.enums import Resampling

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import transforms, font_manager
from matplotlib.patches import Rectangle, Polygon, Circle
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


WGS84 = "EPSG:4326"
__version__ = "1.7.8"
# 血缘标签缺失时的兜底影像源名（仅在 TIF 无 `source` 标签时用到）
DEFAULT_SOURCE_LABEL = "Esri World Imagery"

# 影像读取超采样倍数：按「最终显示像素 × 该倍数」从源降采样读（走概览层），再重投影。
# 2.0 = 读入 4× 显示像素，兼顾抗混叠与速度（实测 2.8× 加速、峰值内存降 5.8×，见 load_display_array）。
DEFAULT_SUPERSAMPLE = 2.0

# PNG 存盘用的 zlib 压缩级别（v1.7.8，见 make_map 里 savefig 处的微基准说明）。
# 3 相对默认 6：体积完全不变、产物逐像素相同、快约 11%。
PNG_COMPRESS_LEVEL = 3

# 出图默认参数（v1.7.8 起集中定义）：既作 make_map 的默认值，也供一键入口
# （satellite_imagery_cn.py）在延迟导入 make_map 后读取，避免两处各写一份魔法数字而漂移。
DEFAULT_MAP_DPI = 200
DEFAULT_MAX_SIDE = 3500

# 版边（英寸，绝对值）：左侧给纬度标注，底部给经度标注 + 比例尺，
# 右侧给经度标注留空；`TOP_IN` 是上边距的**下限** —— 实际值由 `_title_layout()` 按标题栈
# 所需高度取 max（v1.7.7：固定 0.5in 时「14pt 标题 + 副标题」会被裁）。
LEFT_IN, RIGHT_IN, TOP_IN, BOTTOM_IN = 0.85, 0.55, 0.5, 0.85

# ---------------------------------------------------------------------------
# 中文字体：matplotlib 自带 DejaVu Sans 不含 CJK 字形，需注册系统字体。
# 优先 Microsoft YaHei，回退 SimHei / SimSun，保证标题/比例尺/数据来源正常显示。
# ---------------------------------------------------------------------------
_CJK_FONT_SET = False


def _init_fonts():
    global _CJK_FONT_SET
    if _CJK_FONT_SET:
        return
    candidates = [
        (r"C:/Windows/Fonts/msyh.ttc", "Microsoft YaHei"),
        (r"C:/Windows/Fonts/msyhbd.ttc", "Microsoft YaHei"),
        (r"C:/Windows/Fonts/simhei.ttf", "SimHei"),
        (r"C:/Windows/Fonts/simsun.ttc", "SimSun"),
    ]
    families = []
    for path, name in candidates:
        if os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                if name not in families:
                    families.append(name)
            except Exception:
                pass
    if families:
        base = list(plt.rcParams.get("font.sans-serif", []))
        plt.rcParams["font.sans-serif"] = families + base
        plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False
    _CJK_FONT_SET = True


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _nice_ceil(x):
    """返回 >= x 的「漂亮」数（1/2/2.5/5 × 10^k 序列），用于经纬网间隔与比例尺长度。"""
    if x <= 0 or not math.isfinite(x):
        return 1.0
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (1, 2, 2.5, 5, 10):
        if m * base >= x:
            return m * base
    return 10.0 * base


def _fmt_dms(deg, is_lon):
    """把十进制度格式化为 DMS（度°分′秒″ + 半球字母），类似 ArcGIS 经纬网标注。"""
    hemi = "E" if is_lon else "N"
    if deg < 0:
        hemi = "W" if is_lon else "S"
    a = abs(deg)
    d = int(a)
    m = (a - d) * 60.0
    mi = int(m)
    s = (m - mi) * 60.0
    if s >= 59.5:
        mi += 1
        s = 0.0
    if mi >= 60:
        d += 1
        mi = 0
    if s >= 0.5:
        return f"{d}°{mi:02d}′{int(round(s)):02d}″{hemi}"
    return f"{d}°{mi:02d}′{hemi}"


def _fmt_len(meters):
    """把米数格式化为可读标签（≥1000 m 显示为 km）。"""
    if meters >= 1000:
        v = meters / 1000.0
        return f"{v:g} km" if abs(v - round(v)) > 1e-9 else f"{int(round(v))} km"
    return f"{int(round(meters)):d} m"


# ---------------------------------------------------------------------------
# WGS84 椭球：把「经度差」精确换算为地面距离（比例尺真值的基础）
# ---------------------------------------------------------------------------
_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)


def _m_per_deg_lon(lat_deg):
    """WGS84 椭球上给定纬度处 1° 经度对应的地面距离（米）。

    1° 经线弧长 = N·cosφ·(π/180)，N 为卯酉圈曲率半径。
    旧版用球面近似 `111320·cosφ`：在 31.7°N 处偏低约 0.08%；更关键的是旧版取的是
    **图幅中纬**，而比例尺画在靠近下沿的位置，两者纬度不同 → 真值进一步偏 0.1% 量级。
    """
    phi = math.radians(lat_deg)
    s = math.sin(phi)
    N = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * s * s)
    return math.radians(1.0) * N * math.cos(phi)


def _is_nice(x, tol=1e-6):
    """x 是否为「漂亮数」（1/2/2.5/5 × 10^k）—— 用于比例尺分段整除判定。"""
    if x <= 0 or not math.isfinite(x):
        return False
    m = x / (10.0 ** math.floor(math.log10(x)))
    return any(abs(m - c) < tol for c in (1.0, 2.0, 2.5, 5.0, 10.0))


# ---------------------------------------------------------------------------
# 行政边界叠加（研究区图必备）：DataV 边界为 GCJ-02，先转 WGS84 再与影像对齐
# ---------------------------------------------------------------------------
def _iter_geom_rings(geom):
    """遍历 Polygon / MultiPolygon 的所有环（外环 + 内环）。"""
    t = geom.get("type")
    cs = geom.get("coordinates", [])
    if t == "Polygon":
        for ring in cs:
            yield ring
    elif t == "MultiPolygon":
        for poly in cs:
            for ring in poly:
                yield ring


def _boundary_rings_wgs84(boundary):
    """解析边界输入 → WGS84 环列表 [(lons, lats), ...]（DataV 的 GCJ-02 自动反算）。

    boundary 可以是：
      - DataV 风格 GeoJSON（含 features[].geometry）；
      - 单个 geometry 字典；
      - 上述任一者的文件路径 / JSON 字符串。
    返回空列表表示无可绘制内容。
    """
    from resolve_region import gcj02_to_wgs84

    if isinstance(boundary, (str, os.PathLike)):
        p = str(boundary)
        if os.path.exists(p):
            # 【v1.7.7】改用 with 关闭句柄（此前 open() 泄漏 fd，Windows 上尤其不该）
            with open(p, encoding="utf-8") as fh:
                boundary = json.loads(fh.read())
        else:
            boundary = json.loads(p)

    if not isinstance(boundary, dict):
        return []
    if "features" in boundary:
        geoms = [f.get("geometry") for f in boundary["features"] if f.get("geometry")]
    elif "coordinates" in boundary:
        geoms = [boundary]
    else:
        return []

    rings = []
    for g in geoms:
        for ring in _iter_geom_rings(g):
            if len(ring) < 3:
                continue
            lons, lats = [], []
            for c in ring:
                lon, lat = gcj02_to_wgs84(float(c[0]), float(c[1]))
                lons.append(lon)
                lats.append(lat)
            rings.append((lons, lats))
    return rings


def _draw_boundary_rings(ax, rings, color="#d81e06", lw=1.6, zorder=4):
    """在经纬度坐标系上叠加行政边界线（ring 已由 _boundary_rings_wgs84 转成 WGS84）。"""
    for lons, lats in rings:
        # 白色底衬：保证在深浅不一的影像上都清晰可读
        ax.plot(lons, lats, color="white", linewidth=lw + 1.4, alpha=0.9,
                zorder=zorder - 1, solid_joinstyle="round", solid_capstyle="round")
        ax.plot(lons, lats, color=color, linewidth=lw, zorder=zorder,
                solid_joinstyle="round", solid_capstyle="round")


# ---------------------------------------------------------------------------
# 载入并降采样 / 重投影为地理坐标
# ---------------------------------------------------------------------------
def load_display_array(path, max_side=3500, supersample=DEFAULT_SUPERSAMPLE):
    """读取 GeoTIFF，降采样后（必要时）重投影到 EPSG:4326。

    返回 (rgb(H,W,3) uint8, extent(west,south,east,north), src_crs, tags)。

    优化：
    - 源本身已是 EPSG:4326 时直接跳过整幅重投影（省去重投影开销与无谓插值模糊）；
    - 源为 EPSG:3857 时，按**最终显示像素的 supersample 倍**降采样读（GDAL 自动选用合适的
      概览层），再对这个小数组做一次重投影。

    为什么 v1.6.0 把 v1.4.2 的「单步由源 band 直接 reproject」改回来（测量优先，纠正此前结论）：
    - 传 `rasterio.band(src, (1,2,3))` 给 `reproject` 会让 GDAL **物化整幅源**。v1.4.2 当时在
      小样本上测出单步更快（省一遍重采样），但真实产品是 15721×32039（1.5 GB 像元）：
      微基准（瑶海区 z17）单步 **3.47 s、进程峰值 ~1002 MB**，几乎全花在「读 + 解压整幅」上，
      而最终显示只要 1951×3375 像素 —— 白读约 90% 的像素。
    - 改为超采样读后：**1.22 s / 峰值 174 MB**（2.8× 加速、内存降 5.8×）。
    - 代价：多一遍采样（先降采样到 2× 显示尺寸，再重投影到显示网格），与旧路径的像素差
      mean=1.5、p99=7（/255）。注意旧路径的 8× 抽取 bilinear 本身有混叠，并非更优基准；
      超采样读走的是建概览时用的 `average` 金字塔，抗混叠更好。
    - supersample=1.0 可把源读降到与显示同尺寸（最快、0.70 s），代价是像素差升到 mean=2.8/p99=14。
    """
    with rasterio.open(path) as src:
        src_crs = src.crs
        scale = max(src.width, src.height) / max_side
        if scale < 1:
            scale = 1.0
        W = max(1, int(round(src.width / scale)))
        H = max(1, int(round(src.height / scale)))

        tags = src.tags()

        # 源已是经纬度：仅做显示降采样，无需重投影
        if src_crs is not None and str(src_crs).upper() == WGS84:
            # 【v1.7.8】降采样读改用 average 核（见下方 3857 分支的说明）
            arr = src.read(out_shape=(src.count, H, W), resampling=Resampling.average)
            if arr.shape[0] >= 3:
                arr = arr[:3]
            else:
                arr = np.repeat(arr[:1], 3, axis=0)
            new_transform = src.transform * src.transform.scale(
                (src.width / W), (src.height / H))
            extent = rasterio.transform.array_bounds(H, W, new_transform)
            return np.transpose(arr, (1, 2, 0)).copy(), extent, src_crs, tags

        # 源为 3857：先按 supersample 倍降采样读（走概览层），再重投影到 4326 目标网格
        dst_transform, dst_w, dst_h = calculate_default_transform(
            src_crs, WGS84, W, H, *src.bounds)
        k = float(supersample) if supersample and supersample > 0 else 1.0
        pre_w = min(src.width, max(dst_w, int(round(dst_w * k))))
        pre_h = min(src.height, max(dst_h, int(round(dst_h * k))))
        if pre_w < src.width or pre_h < src.height:
            # 【v1.7.8】这一跳是**降采样**（23943px 源 -> 4848px，约 4.9×），必须用
            # average（面积平均）核：bilinear 在降采样时只取 2×2 邻域、不覆盖像元足迹，
            # 会产生混叠（摩尔纹/细节闪烁）。微基准（包河区 z17，max_side=2600，
            # supersample=2.0）：**1420 ms -> 1004 ms（1.41×）**，且两法像素差
            # mean=0.31 / p99=2 / max=7（/255）—— 更快 + 更正确，是纯收益。
            # 注意：**只改这一跳**。下面 reproject 到目标网格那一跳保持 bilinear
            # （它是跨 CRS 重投影，不是同轴降采样）。
            arr = src.read(out_shape=(src.count, pre_h, pre_w),
                           resampling=Resampling.average)
            src_transform = src.transform * src.transform.scale(
                (src.width / pre_w), (src.height / pre_h))
        else:   # 源本身就只有显示尺寸量级，直接整幅读（此时整幅本来就不大）
            arr = src.read()
            src_transform = src.transform
        if arr.shape[0] >= 3:
            arr = arr[:3]
        else:
            arr = np.repeat(arr[:1], 3, axis=0)
        dst = np.zeros((3, dst_h, dst_w), dtype=np.uint8)
        reproject(
            source=arr,
            destination=dst,
            src_crs=src_crs,
            src_transform=src_transform,
            dst_crs=WGS84,
            dst_transform=dst_transform,
            resampling=Resampling.bilinear,
        )
        extent = rasterio.transform.array_bounds(dst_h, dst_w, dst_transform)
        return np.transpose(dst, (1, 2, 0)).copy(), extent, src_crs, tags


# ---------------------------------------------------------------------------
# 专题地图渲染
# ---------------------------------------------------------------------------
def _draw_graticule(ax, west, south, east, north):
    """绘制经纬网（虚线 + 白色半透明标签，标注为 DMS）。

    经纬间隔取统一步长（由影像的**较大跨度**按约 6 格取整得出），横纵共用 —— 保证网格
    是正方形而非被图幅比例拉长。返回 None（v1.7.0 之前 docstring 误称会返回标签列表）。
    """
    span = max(east - west, north - south)
    step = _nice_ceil(span / 6.0)
    xticks = np.arange(math.ceil(west / step) * step, east + 1e-9, step)
    yticks = np.arange(math.ceil(south / step) * step, north + 1e-9, step)
    ax.set_xticks(xticks)
    ax.set_yticks(yticks)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_dms(v, True)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_dms(v, False)))
    ax.grid(True, color="#1a1a1a", linestyle="--", linewidth=0.7, alpha=0.45, zorder=1)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_bbox(dict(facecolor="white", alpha=0.75, edgecolor="none", pad=1.0))
        lbl.set_fontsize(8)


# 比例尺几何常量（axes 分数：x 相对影像宽、y 相对影像高）
SCALE_BAR_X0 = 0.04        # 左端距影像左沿（固定）
SCALE_BAR_Y0 = 0.045       # 条带下沿距影像下沿（固定）
SCALE_BAR_H = 0.012        # 条带高度
SCALE_BAR_TARGET = 0.18    # 目标长度占影像宽度比例
# 窄图幅（长条形区县 / 小 --max-side）下依次尝试的收缩目标：保证「条带 + 两侧标注留白」
# 能塞进图幅水平空间，卡片不被图幅边界裁掉（v1.7.6）。
_SB_SHRINK_TARGETS = (SCALE_BAR_TARGET, 0.16, 0.14, 0.12, 0.10, 0.08, 0.06)
SCALE_BAR_FS = 7.5         # 距离标注字号（点）

# 面板（背景）样式：比例尺与图例共用同一套白色衬底。
# 影像明暗与红色区界线都可能压过装饰件 —— 统一垫一层白底，黑字 / 白描边在任何底图上都保持可读。
# 默认 **1.0（全不透明）**：半透明会留下区界线残影 —— 实测 alpha=0.94 时红线处仍偏白
# Δ≈(2,14,15)，在整片白面板上能看出一条淡粉细线，与「图例不与边界重合」的诉求相悖。
# 想让底图微微透出可用 `--panel-alpha 0.9`；完全不要面板用 `--panel-alpha 0`。
PANEL_ALPHA = 1.0
PANEL_EDGE = "#8c8c8c"
PANEL_LW = 0.8
PANEL_PAD_PT = 3.0         # 面板四周相对内容的内缩（点）


def _axes_inch(ax):
    """影像框的宽高（英寸）—— 用于把「点」精确换算成 axes 分数。"""
    fig = ax.figure
    bb = ax.get_position()
    return (max(1e-6, bb.width * fig.get_figwidth()),
            max(1e-6, bb.height * fig.get_figheight()))


def _draw_panel(ax, rect, alpha=PANEL_ALPHA, zorder=4):
    """在 axes 分数矩形 `rect` 内绘制白色面板，作为比例尺 / 图例的背景。

    zorder 由调用方显式指定，且两块面板都**高于**区界线（白衬 3 / 主线 4）：
      · 比例尺面板 3.5（低于自身内容 5~8）
      · 图例面板 20（低于自身内容 21~22）
    —— 这是有意为之：红区界线穿到卡片处就被卡片压住，正是「装饰件不与边界重合」的实现方式
    （v1.7.4 用户明确要求）。v1.7.7 之前这里靠「同 zorder 后画者在上」的插入顺序达到同样
    效果，注释却写成「比例尺面板在区界线之下」，与实测相反；现改为显式 zorder。
    """
    x0, y0, x1, y1 = rect
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, transform=ax.transAxes,
                           facecolor="white", alpha=alpha, edgecolor=PANEL_EDGE,
                           linewidth=PANEL_LW, zorder=zorder))


def _scale_bar_geom(west, east, south, north, target=SCALE_BAR_TARGET):
    """计算比例尺几何：长度（经度差）、分段数、每段米数。

    分两步保证「精确可读」：
    1. 总长取「漂亮数」（1/2/2.5/5×10^k 米）；
    2. 再选能整除成漂亮数的**最大段数**（5 → 4 → 3 → 2），使每个刻度都落在整数距离上。
    例：总长 5 km → 5 段 × 1 km，刻度为 0/1/2/3/4/5 km。

    target : 条带目标长度占图宽比例（默认 `SCALE_BAR_TARGET`）。窄图幅（长条形区县、
        小 `--max-side`）下 `_draw_scale_bar` 会传更小的值，保证「条带 + 两侧标注留白」
        能塞进图幅水平空间，不会顶出卡片。
    """
    lat_bar = south + (north - south) * SCALE_BAR_Y0
    mpd = _m_per_deg_lon(lat_bar)                       # 米/度（尺子所在纬度，WGS84 椭球）
    total_m = _nice_ceil((east - west) * mpd * target)
    nseg, seg_m = 1, total_m
    for n in (5, 4, 3, 2):
        if _is_nice(total_m / n):
            nseg, seg_m = n, total_m / n
            break
    width_deg = total_m / mpd
    return {
        "mpd": mpd,
        "total_m": total_m,
        "nseg": nseg,
        "seg_m": seg_m,
        "width_deg": width_deg,
        "width_frac": width_deg / (east - west),
        "lat_bar": lat_bar,
    }


def _get_renderer(ax):
    """拿一个可用于量「文字/图元渲染包围盒」的 renderer（Agg 下无需先 draw）。"""
    fig = ax.figure
    try:
        return fig.canvas.get_renderer()
    except Exception:
        fig.canvas.draw()
        return fig.canvas.get_renderer()


def _text_width_in(ax, s, fs, weight="normal", renderer=None):
    """**实测**字符串在给定字号下的渲染宽度（英寸）。

    在当前 Axes 上临时造一个同字号 Text，量出真实包围盒后立刻删掉 —— 量的是渲染器最终
    真正使用的字体，与「0.62em × 字数」这类估算无关。
    【v1.7.8】此前同一套「临时 Text 量完即删」在 `_measure_max_half_width_pt` 与
    `_legend_metrics._text_in` 各写了一遍（另有一处变体在 `_fit_text_fs`），现收敛到此处。
    """
    fig = ax.figure
    r = renderer if renderer is not None else _get_renderer(ax)
    t = ax.text(0.0, 0.0, s, fontsize=fs, fontweight=weight)
    try:
        return t.get_window_extent(r).width / fig.dpi
    finally:
        t.remove()


def _measure_max_half_width_pt(ax, labels, fontsize):
    """实测最宽标签的**半宽**（点）—— 供比例尺面板左右留白。

    旧实现用 `0.62em × 字数 ÷ 2` 估算，属上限粗估，遇到字宽/字体变化就不再可靠；
    现改为用真实字体实测（`_text_width_in`）。
    """
    r = _get_renderer(ax)
    widest = max((_text_width_in(ax, s, fontsize, "normal", r) for s in labels),
                 default=0.0)
    return widest * 72.0 / 2.0


def _artists_bbox_axes(ax, artists, pad_pt=PANEL_PAD_PT):
    """把一组图元的**实际渲染包围盒**并起来，外扩 pad_pt 点后转成 axes 分数矩形。

    这是面板矩形（白色卡片）的权威来源：不论字号、字体、DPI、标签文案怎么变，
    卡片一定包住内容 —— 不存在「内容跑出卡片」的可能。
    量不到时返回 None，由调用方退回解析式保守矩形。
    """
    r = _get_renderer(ax)
    bb = None
    for a in artists:
        try:
            e = a.get_window_extent(r)
        except Exception:
            continue
        if e is None or e.width <= 0 or e.height <= 0:
            continue
        bb = e if bb is None else transforms.Bbox.union([bb, e])
    if bb is None:
        return None
    pad = pad_pt / 72.0 * ax.figure.dpi
    bb = transforms.Bbox.from_extents(bb.x0 - pad, bb.y0 - pad, bb.x1 + pad, bb.y1 + pad)
    (x0, y0), (x1, y1) = ax.transAxes.inverted().transform([(bb.x0, bb.y0), (bb.x1, bb.y1)])
    return (x0, y0, x1, y1)


def _draw_scale_bar(ax, geom, west, east, panel_alpha=PANEL_ALPHA):
    """图内**固定左下角**：精确直线比例尺（分段交替黑白 + 逐刻度距离标注）+ 白色衬底面板。

    针对「比例尺要精确」的三点修正：
    1. 地面距离改用 WGS84 椭球在**尺子自身纬度**上计算（旧版用球面 111320·cosφ 且取图幅中纬）；
    2. 尺长按「漂亮数总长 ÷ 漂亮数段长」分段，每个刻度都对应整数距离，可直接读数；
    3. 标注**居中于刻度**（旧版首尾用 ha=right/left 贴边，视觉上尺子显得比标称值长/短）。

    尺子用「x=数据坐标、y=axes 分数」的混合变换绘制，因此其像素长度严格等于
    width_deg 的经度差 —— 不经过任何额外重采样。

    卡片（面板）几何（v1.7.6 起）：不再由字号估算反推，而是**先画完所有图元，再用它们的
    实测渲染包围盒并集 + `PANEL_PAD_PT` 留白**得到矩形 —— 卡片永远包住条带、刻度与标注。
    水平空间不足（窄图幅）时还会把条带右移 / 依次缩短目标长度，避免卡片被图幅边界裁掉。
    返回该矩形，供图例避让（`auto` 模式）复用。
    """
    # ⚠ 单位陷阱：条带用 blended(transData, transAxes) 绘制，x 方向是**数据坐标**，
    # 因此所有宽度必须用「经度差」width_deg（数据单位），绝不能用 width_frac（axes 分数）——
    # 两者相差 (east-west) 倍，在本图幅约 3.9×，会把条带外框画到图幅中部去。
    ax_w_in, ax_h_in = _axes_inch(ax)

    # ---- 1) 排版：条带左端、标注密度、卡片水平留白（文字宽度**实测**）----
    def _plan(g):
        bar_pt = g["width_frac"] * ax_w_in * 72.0
        if bar_pt / g["nseg"] >= 26.0:
            idx = list(range(g["nseg"] + 1))
        elif bar_pt >= 42.0:
            idx = [0, g["nseg"]]
        else:
            idx = [g["nseg"]]
        labs = [_fmt_len(i * g["seg_m"]) for i in idx]
        pad = (_measure_max_half_width_pt(ax, labs, SCALE_BAR_FS) + PANEL_PAD_PT) / 72.0 / ax_w_in
        return idx, pad

    tick_idx, pad_x, x0f = [geom["nseg"]], 0.0, SCALE_BAR_X0
    for _ in range(3):                  # 缩短条带会改变标注 → 最多迭代 3 轮收敛
        tick_idx, pad_x = _plan(geom)
        x0f = max(SCALE_BAR_X0, pad_x)  # 左侧留白不足 → 把条带右移
        if x0f + geom["width_frac"] + pad_x <= 1.0:
            break
        shrunk = False
        for t in _SB_SHRINK_TARGETS:    # 右侧仍不够 → 依次缩短条带目标长度
            cand = _scale_bar_geom(west, east, south, north, target=t)
            if cand["width_frac"] <= 1.0 - x0f - pad_x:
                geom, shrunk = cand, True
                break
        if not shrunk:
            break

    bar_w = geom["width_deg"]           # 条带本体宽度（**数据空间**，见上「单位陷阱」）
    nseg, seg_m = geom["nseg"], geom["seg_m"]
    seg_w = bar_w / nseg
    x_start = west + (east - west) * x0f
    y0, h = SCALE_BAR_Y0, SCALE_BAR_H
    label_y = y0 + h + 0.009
    trans = transforms.blended_transform_factory(ax.transData, ax.transAxes)

    # ---- 2) 画图元（卡片最后画，靠 zorder=4 落到它们下面）----
    artists = []
    # 条带：白衬 + 交替黑白分段（ArcGIS「Alternating Scale Bar」风格）+ 外框
    artists.append(ax.add_patch(Rectangle((x_start, y0), bar_w, h, transform=trans,
                                          facecolor="white", edgecolor="white",
                                          linewidth=2.6, zorder=5)))
    for i in range(nseg):
        artists.append(ax.add_patch(Rectangle((x_start + i * seg_w, y0), seg_w, h, transform=trans,
                                              facecolor=("black" if i % 2 else "white"),
                                              edgecolor="none", zorder=6)))
    artists.append(ax.add_patch(Rectangle((x_start, y0), bar_w, h, transform=trans,
                                          facecolor="none", edgecolor="black",
                                          linewidth=1.0, zorder=7)))
    # 分段刻度线（向下伸出，黑白段上都可见）
    for i in range(nseg + 1):
        x = x_start + i * seg_w
        artists.append(ax.add_line(Line2D([x, x], [y0 - 0.004, y0 + h], transform=trans,
                                          color="black", linewidth=1.0, zorder=8)))
    # 距离标注：面板已提供背景，故不再逐个加 bbox（少一层白块，观感更干净）
    for i in tick_idx:
        artists.append(ax.text(x_start + i * seg_w, label_y, _fmt_len(i * seg_m), transform=trans,
                               ha="center", va="bottom", fontsize=SCALE_BAR_FS,
                               color="black", zorder=8))

    # ---- 3) 卡片矩形 = 上述图元实测包围盒并集 + 留白 ----
    rect = _artists_bbox_axes(ax, artists, PANEL_PAD_PT)
    if rect is None:                    # 极端环境下量不到 → 退回解析式保守矩形
        pad_y = PANEL_PAD_PT / 72.0 / ax_h_in
        rect = (max(0.0, x0f - pad_x), max(0.0, y0 - 0.004 - pad_y),
                min(1.0, x0f + geom["width_frac"] + pad_x),
                min(1.0, label_y + SCALE_BAR_FS * 1.35 / 72.0 / ax_h_in))
    # zorder=3.5：**低于**比例尺内容（5~8）但**高于**区界线（主 4 / 白衬 3）——
    # 显式指定，不再依赖「同 zorder 后画者在上」的插入顺序（v1.7.7）。
    _draw_panel(ax, rect, alpha=panel_alpha, zorder=3.5)
    return rect


# 指北针相对「影像右上角」的内缩量（单位：点）。用固定点距而非 axes-fraction，
# 这样不论地图长宽比如何，罗盘（含 N/E/S/W 注记）都稳定贴住右上角。
# 46pt ≈ 罗盘半径 24pt + E/S/W 注记半宽；60pt ≈ 罗盘半径 + N 注记高度，均留 ~6pt 余量。
NORTH_ARROW_INSET_X_PT = 46.0
NORTH_ARROW_INSET_Y_PT = 60.0


def _north_arrow_xy(ax):
    """把「距影像右上角固定点距」换算成 axes-fraction 坐标（v1.6.0）。

    此前用固定 axes-fraction (0.87, 0.80)：在窄长图幅上 0.80 离上沿还有 20% 高，
    罗盘看起来落在「右中」而不是右上角。改为按点距内缩——点距是物理单位，
    影像多高多宽都只影响换算系数，因此对任意长宽比都稳定贴合右上角。
    """
    # 【v1.7.8】复用 `_axes_inch()`，不再就地重写一遍英寸换算（同文件里原本有 3 份副本）
    w_in, h_in = _axes_inch(ax)
    x = 1.0 - (NORTH_ARROW_INSET_X_PT / 72.0) / w_in
    y = 1.0 - (NORTH_ARROW_INSET_Y_PT / 72.0) / h_in
    return (min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))


def _draw_north_arrow(ax):
    """图内右上角：罗盘式指北针（经典双色罗盘玫瑰，上=北）。

    每个星臂由「暗面 + 亮面」两个三角组成（经典罗盘的立体明暗），北臂用红色系（暗红 +
    浅红），其余臂深灰 + 浅灰；外圈白底深边，中心枢轴点。四周标注 N/E/S/W（N 加粗红）。
    用 DrawingArea（点单位、各向同性）绘制 —— 注意其坐标为**数学坐标（y 向上）**，故北=+y。
    """
    S = 56.0
    cx = S / 2.0
    cy = S / 2.0
    R = 24.0                      # 外圈 / 主星臂长
    rin = R * 0.32                # 星臂基部内半径
    delta = math.radians(19.0)    # 星臂基部半角
    DARK, LIGHT = "#1a1a1a", "#cfcfcf"
    RED_D, RED_L = "#d81e06", "#f0a49c"

    da = DrawingArea(S, S, 0, 0)
    da.add_artist(Circle((cx, cy), R, facecolor="white",
                         edgecolor="#1a1a1a", linewidth=1.3))

    def add_point(angle_deg, length, c_dark, c_light):
        th = math.radians(angle_deg)
        tip = (cx + length * math.cos(th), cy + length * math.sin(th))
        b1 = (cx + rin * math.cos(th - delta), cy + rin * math.sin(th - delta))
        b2 = (cx + rin * math.cos(th + delta), cy + rin * math.sin(th + delta))
        # 以「中心→顶点」连线为界，分暗面 / 亮面（经典罗盘立体感）
        da.add_artist(Polygon([tip, b2, (cx, cy)], closed=True, facecolor=c_dark,
                              edgecolor="#1a1a1a", linewidth=0.5))
        da.add_artist(Polygon([tip, (cx, cy), b1], closed=True, facecolor=c_light,
                              edgecolor="#1a1a1a", linewidth=0.5))

    # 0°=E, 90°=N, 180°=W, 270°=S；偶数索引为主星（长），奇数索引为次星（短）
    for i in range(8):
        ang = i * 45.0
        if i == 2:                       # N：红
            add_point(ang, R, RED_D, RED_L)
        elif i % 2 == 0:                 # E/S/W 主星
            add_point(ang, R, DARK, LIGHT)
        else:                            # 45° 次星（短）
            add_point(ang, R * 0.60, DARK, LIGHT)

    # 中心枢轴
    da.add_artist(Circle((cx, cy), 2.6, facecolor="white",
                         edgecolor="#1a1a1a", linewidth=1.0))
    center = _north_arrow_xy(ax)
    ab = AnnotationBbox(da, center, xycoords="axes fraction",
                        box_alignment=(0.5, 0.5), frameon=False, zorder=9,
                        annotation_clip=False)
    ax.add_artist(ab)
    # N/E/S/W 注记（N 加粗红），放在外圈外
    _ncompass_label(ax, center, 0, 40, "N", "#d81e06", 10.5, True, "bottom")
    _ncompass_label(ax, center, 0, -40, "S", "#555555", 8.5, False, "top")
    _ncompass_label(ax, center, 34, 0, "E", "#555555", 8.5, False, "center", "left")
    _ncompass_label(ax, center, -34, 0, "W", "#555555", 8.5, False, "center", "right")


def _ncompass_label(ax, center, dx, dy, txt, color, fs, bold, va, ha="center"):
    """在罗盘中心相对偏移处放置一个方向字母（白底小框，深浅影像均清晰）。"""
    ax.annotate(txt, xy=center, xycoords="axes fraction",
                xytext=(dx, dy), textcoords="offset points",
                ha=ha, va=va, fontsize=fs, fontweight=("bold" if bold else "normal"),
                color=color,
                bbox=dict(facecolor="white", alpha=0.85, edgecolor="none", pad=0.4),
                zorder=10, annotation_clip=False)


def _north_arrow_reserved(ax):
    """指北针（罗盘 + N/E/S/W 注记）占用的 axes 分数矩形 —— 供图例避让。

    罗盘 DrawingArea 边长 56 pt，方向字母在 ±40 pt 处，故按中心 ±50 pt 包裹。
    """
    cx, cy = _north_arrow_xy(ax)
    # 【v1.7.8】复用 `_axes_inch()`
    w_in, h_in = _axes_inch(ax)
    dx = (50.0 / 72.0) / w_in
    dy = (50.0 / 72.0) / h_in
    return (cx - dx, cy - dy, cx + dx, cy + dy)


TITLE_GAP_IN = 0.12              # 标题距影像上沿（英寸）
TITLE_FS_RANGE = (11.0, 14.0)    # 主标题字号范围（点）
SUBTITLE_FS_RANGE = (7.5, 9.0)   # 副标题字号范围（点）
TITLE_MIN_FS = 6.0               # 标题过长时的收字下限（防止横向溢出被裁）


def _base_title_fs(img_w_in):
    """标题 / 副标题的**基准字号**（点）：随地图宽度（英寸）线性插值并夹在区间内。

    【v1.7.8】此前 `_title_layout` 与 `_draw_title_and_credit` 各自抄了一遍同样的两行
    公式 —— 二者必须同源（一个是「收字前的基准」，一个是「单独调用时的回算」），
    抄两份必然改漏一处。现收敛为单一来源。
    """
    return (min(TITLE_FS_RANGE[1], max(TITLE_FS_RANGE[0], img_w_in * 0.45)),
            min(SUBTITLE_FS_RANGE[1], max(SUBTITLE_FS_RANGE[0], img_w_in * 0.3)))


def _credit_line(tags):
    """数据来源注记：取 GeoTIFF 血缘标签里的影像源。

    【v1.7.7 修复】此前写死「数据来源：Esri World Imagery」。TIF 的 `source` 标签本身是
    对的（`build_mosaic` 会按 `--source` 写入），只有图面注记没跟着走 —— 用
    `--source google` 出的图仍标 Esri，属于「不报错但结果不对」。
    """
    src = str((tags or {}).get("source") or "").strip() or DEFAULT_SOURCE_LABEL
    # 去掉括注，如 "Google Satellite (需代理/境外)" -> "Google Satellite"
    # （maxsplit 必须用关键字传参：位置传参在 3.13 已 DeprecationWarning）
    return f"数据来源：{re.split(r'[（(]', src, maxsplit=1)[0].strip()}"


def _fit_text_fs(text, fs, avail_in, dpi, weight="bold"):
    """把字号收小到「实测文字宽 <= avail_in 英寸」（步长 0.5pt，下限 `TITLE_MIN_FS`）。

    量不到（无 renderer）时原样返回 —— 不允许因为度量失败而改变输出。
    """
    if not text or avail_in <= 0:
        return fs
    try:
        probe = plt.figure(figsize=(max(1.0, avail_in), 1.0), dpi=dpi)
        try:
            r = probe.canvas.get_renderer()
            while fs > TITLE_MIN_FS:
                t = probe.text(0.0, 0.5, text, fontsize=fs, fontweight=weight)
                try:
                    w = t.get_window_extent(r).width / dpi
                finally:
                    t.remove()
                if w <= avail_in:
                    break
                fs -= 0.5
        finally:
            plt.close(probe)
    except Exception:  # noqa: BLE001 —— 度量失败就用原字号
        pass
    return fs


def _title_layout(img_w_in, fig_w_in, title, subtitle, dpi):
    """返回 (主标题字号, 副标题字号, 所需上边距英寸)。

    【v1.7.7 修复】此前上边距写死 `TOP_IN = 0.5 in`，标题栈高度却按「0.12 in 间隙 + 估算
    行高」定位 —— 大图（`img_w_in ≥ 31 in` ⇒ 字号到达 14 pt）再带副标题时，所需高度
    `0.12 + 0.263 + 0.175 = 0.558 in > 0.5 in`，副标题被**静默裁掉**（只是少了半行字，
    不报错）。现按保守行高算出所需边距并取 max(0.5in, 需要值)，同时把过长的标题收字，
    避免横向溢出。
    """
    title_fs, sub_fs = _base_title_fs(img_w_in)          # 【v1.7.8】单一来源
    avail = max(1.0, fig_w_in - 0.3)          # 左右各留 0.15 in 不贴边
    title_fs = _fit_text_fs(title, title_fs, avail, dpi, weight="bold")
    if subtitle:
        sub_fs = _fit_text_fs(subtitle, sub_fs, avail, dpi, weight="normal")
    need = TITLE_GAP_IN + title_fs * 1.40 / 72.0 + (sub_fs * 1.50 / 72.0 if subtitle else 0.0)
    return title_fs, sub_fs, max(TOP_IN, need + 0.06)


def _draw_title_and_credit(fig, ax, title, subtitle, title_fs=None, sub_fs=None,
                           credit=None):
    """标题紧贴影像上沿（距上沿 0.12in），数据来源注记在右下角。

    字号按地图宽度（英寸）缩放，防止小图溢出；`title_fs` / `sub_fs` 缺省时按同一公式
    回算（保持单独调用时的行为不变），实际由 `_title_layout()` 传入（含收字结果）。
    标题位置由影像上沿（`ax` 位置）推算，不再固定在 figure 顶部 —— 此前固定在 0.99
    导致标题离影像过远。
    """
    img_w_in = max(1.0, fig.get_figwidth() - LEFT_IN - RIGHT_IN)
    if title_fs is None or sub_fs is None:               # 【v1.7.8】单一来源
        base_title_fs, base_sub_fs = _base_title_fs(img_w_in)
        if title_fs is None:
            title_fs = base_title_fs
        if sub_fs is None:
            sub_fs = base_sub_fs
    fig_h = fig.get_figheight()
    img_top = ax.get_position().y1          # 影像上沿（figure 分数）
    gap = TITLE_GAP_IN / fig_h              # 标题距影像上沿 0.12 英寸
    fig.text(0.5, img_top + gap, title, ha="center", va="bottom",
             fontsize=title_fs, fontweight="bold", color="#1a1a1a")
    if subtitle:
        fig.text(0.5, img_top + gap + (title_fs * 1.35 / 72.0) / fig_h, subtitle,
                 ha="center", va="bottom", fontsize=sub_fs, color="#444444")
    fig.text(0.99, 0.015, credit or f"数据来源：{DEFAULT_SOURCE_LABEL}",
             ha="right", va="bottom", fontsize=7, color="#666666", style="italic")


# ---------------------------------------------------------------------------
# 图例几何
# ---------------------------------------------------------------------------
# 【v1.7.7 修复】此前图例框写死为 axes 分数 `0.27 × 0.056`、而框内文字是**固定磅值**
# （9.5 / 8.8 pt）—— 二者随图幅长宽脱钩：轴宽小于约 7 in 时文字必然溢出面板
# （实测 4 in 轴溢出 0.675 in、5.3 in 轴溢出 0.14 in；`--max-side 900 --map-dpi 150`
# 这种小图就能复现，图上表现为区县名跑到白框外）。现在改为：
#   · 版式常量以**英寸**给出（按 `LEGEND_REF_AX_W_IN` 基准图幅标定，与旧渲染逐像素等价）；
#   · 字号随轴宽等比缩放（`LEGEND_SCALE_RANGE` 限幅，大图保持原字号不变）；
#   · 面板尺寸 = 实测内容所需尺寸，再对 `LEGEND_MIN_*` 取 max 保底 —— 即
#     「不会比现在小，只在内容放不下时变大」，因此现有观感不变、小图不再溢出。
LEGEND_MIN_W = 0.27          # 面板最小宽（axes 分数，标定于基准轴宽）
LEGEND_MIN_H = 0.056         # 面板最小高（axes 分数）
LEGEND_MARGIN = 0.025        # 距图幅边的留白（axes 分数）
LEGEND_REF_AX_W_IN = 10.3    # 版式标定基准：轴宽 10.3 in 时与旧版渲染一致
LEGEND_SCALE_RANGE = (0.72, 1.00)   # 字号随轴宽缩放的限幅（下限保小图仍可读，上限不放大）
LEGEND_FS_TITLE = 9.5        # 「图例」标题字号（点，scale=1 时）
LEGEND_FS_LABEL = 8.8        # 「行政边界（…）」字号（点，scale=1 时）
LEGEND_PAD_X_IN = 0.185      # 内容左内边距（英寸）
LEGEND_TITLE_BELOW_TOP_IN = 0.164   # 标题基线中心距面板上沿
LEGEND_ROW_Y_IN = 0.207      # 样块行中心距面板下沿
LEGEND_SAMPLE_W_IN = 0.660   # 样块线段长度
LEGEND_GAP_IN = 0.165        # 样块右端 → 文字左端
LEGEND_PAD_RIGHT_IN = 0.16   # 内容右内边距
# 默认锚点：**右下角固定**（ArcGIS 惯例）。区界线压框的问题改由不透明面板解决（v1.7.4），
# 不再靠挪位置回避；下列候选顺序仅 `--legend-pos auto` 时启用。
LEGEND_DEFAULT_POS = "se"
_LEGEND_ANCHOR_ORDER = ("se", "sw", "ne", "nw", "e", "w", "s", "n")

def _legend_metrics(ax, region_name):
    """图例的尺寸与字号 —— 由**实测文字宽度**反推，面板必然包住内容（v1.7.7）。

    返回 dict(w, h, fs_title, fs_label, scale)：w/h 为 axes 分数，其余为磅值 / 倍率。
    """
    ax_w_in, ax_h_in = _axes_inch(ax)
    label = f"行政边界（{region_name or '研究区'}）"
    renderer = _get_renderer(ax)
    pad_pt_in = PANEL_PAD_PT / 72.0

    def _text_in(s, fs, weight="normal"):
        # 【v1.7.8】复用统一实现（原先与 _measure_max_half_width_pt 各写了一遍同样的量法）
        return _text_width_in(ax, s, fs, weight, renderer)

    def _calc(scale):
        pad_x = LEGEND_PAD_X_IN * scale
        pad_top = LEGEND_TITLE_BELOW_TOP_IN * scale
        row_y = LEGEND_ROW_Y_IN * scale
        sample_w = LEGEND_SAMPLE_W_IN * scale
        gap = LEGEND_GAP_IN * scale
        pad_r = LEGEND_PAD_RIGHT_IN * scale
        fs_t = LEGEND_FS_TITLE * scale
        fs_l = LEGEND_FS_LABEL * scale
        need_w = max(pad_x + _text_in("图例", fs_t, "bold") + pad_pt_in,
                     pad_x + sample_w + gap + _text_in(label, fs_l) + pad_r)
        # 行高按 1.35(标题) / 1.5(正文) 倍字号估算 —— 只用于「够不够高」，留了 PANEL_PAD_PT 余量
        need_h = max(pad_top + fs_t * 1.35 / 144.0 + pad_pt_in,
                     row_y + fs_l * 1.50 / 144.0 + pad_pt_in)
        return need_w / ax_w_in, need_h / ax_h_in, fs_t, fs_l

    scale = min(LEGEND_SCALE_RANGE[1], max(LEGEND_SCALE_RANGE[0],
                                           ax_w_in / LEGEND_REF_AX_W_IN))
    w, h, fs_t, fs_l = _calc(scale)
    # 极端窄/矮图幅：再降字号，直到面板能塞进图幅（不会裁切）
    while (w > 0.92 or h > 0.34) and scale > 0.30:
        scale *= 0.85
        w, h, fs_t, fs_l = _calc(scale)
    return dict(w=max(LEGEND_MIN_W, w), h=max(LEGEND_MIN_H, h),
                fs_title=fs_t, fs_label=fs_l, scale=scale)



def _legend_anchor(anchor, bw, bh):
    """把一个语义锚点换算成图例框左下角的 axes 分数坐标。"""
    m = LEGEND_MARGIN
    table = {
        "se": (1.0 - bw - m, m),
        "sw": (m, m),
        "ne": (1.0 - bw - m, 1.0 - bh - m),
        "nw": (m, 1.0 - bh - m),
        "e": (1.0 - bw - m, 0.5 - bh / 2.0),
        "w": (m, 0.5 - bh / 2.0),
        "s": (0.5 - bw / 2.0, m),
        "n": (0.5 - bw / 2.0, 1.0 - bh - m),
    }
    if anchor not in table:
        raise ValueError(f"未知图例锚点: {anchor}")
    x0, y0 = table[anchor]
    return (x0, y0, x0 + bw, y0 + bh)


def _seg_rect_hit(x0, y0, x1, y1, rect):
    """线段与矩形是否相交（Liang–Barsky 裁剪）。"""
    rx0, ry0, rx1, ry1 = rect
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - rx0), (dx, rx1 - x0), (-dy, y0 - ry0), (dy, ry1 - y0)):
        if abs(p) < 1e-15:
            if q < 0:
                return False
        elif p < 0:
            r = q / p
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            r = q / p
            if r < t0:
                return False
            t1 = min(t1, r)
    return t0 <= t1


def _boundary_pressure(nrings, rect, margin=0.004):
    """区界线压在图例框内的「压力值」：框内顶点数 + 直接穿框的分段数。

    nrings 为 `_normalize_rings` 输出的 axes 分数 numpy 折线。命中越多，图例越不该放这里。
    """
    rx0, ry0, rx1, ry1 = rect
    mx0, my0, mx1, my1 = rx0 - margin, ry0 - margin, rx1 + margin, ry1 + margin
    press = 0
    for px, py in nrings:
        n_in = int(((px >= mx0) & (px <= mx1) & (py >= my0) & (py <= my1)).sum())
        if n_in:
            press += n_in
            continue
        # 顶点都不在框内，再看是否有分段直接穿过（稀疏边界的情形）
        x0, x1 = px[:-1], px[1:]
        y0, y1 = py[:-1], py[1:]
        keep = ~((np.maximum(x0, x1) < mx0) | (np.minimum(x0, x1) > mx1) |
                 (np.maximum(y0, y1) < my0) | (np.minimum(y0, y1) > my1))
        for i in np.flatnonzero(keep):
            if _seg_rect_hit(float(x0[i]), float(y0[i]), float(x1[i]), float(y1[i]),
                             (mx0, my0, mx1, my1)):
                press += 1
    return press


def _normalize_rings(rings, west, east, south, north):
    """把 WGS84 环折线换算成 axes 分数坐标（xlim/y lim 即影像 extent）。"""
    sx = 1.0 / (east - west)
    sy = 1.0 / (north - south)
    return [((np.asarray(lons) - west) * sx, (np.asarray(lats) - south) * sy)
            for lons, lats in rings]


def _overlap_area(a, b):
    """两个矩形的重叠面积（axes 分数²）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _place_legend(nrings, reserved, bw=LEGEND_MIN_W, bh=LEGEND_MIN_H):
    """自动选图例位置 —— **仅** `<入口> --legend-pos auto` 时启用（非默认路径）。

    默认路径是「固定右下角 + 不透明面板」(v1.7.4)，已经能压住斜穿右下角的区界线；
    本函数留给「宁可挪位置、也不要面板压底图」的场景。代价函数：
    区界压力 ×1000 + 预留区重叠面积 ×100，同分时按 `_LEGEND_ANCHOR_ORDER` 取靠前候选。
    bw/bh 由 `_legend_metrics()` 给出（v1.7.7 起图例尺寸按内容实测变化，不再是常量）。
    """
    best, best_cost = None, None
    for anchor in _LEGEND_ANCHOR_ORDER:
        rect = _legend_anchor(anchor, bw, bh)
        cost = float(_boundary_pressure(nrings, rect)) * 1000.0 if nrings else 0.0
        for r in reserved:
            if _overlap_area(rect, r) > 0:
                cost += 100.0 * _overlap_area(rect, r)
        if best_cost is None or cost < best_cost - 1e-9:
            best, best_cost = rect, cost
        if cost <= 0:
            break
    return best, float(best_cost or 0.0)


def _draw_legend(ax, rect, boundary_color, region_name, metrics, alpha=PANEL_ALPHA):
    """在图例框 `rect`（axes 分数 x0,y0,x1,y1）内绘制图例。

    白底面板（`_draw_panel`，zorder 20 —— **高于区界线**，故即使红线斜穿右下角也不会
    透到框内）+ **线段**样块（白描边 + 彩线，与图面行政边界绘制方式一致 —— 边界是「线」
    不是「面」），说明红线含义。

    版式（v1.7.7）：位置与字号全部来自 `metrics`（英寸基准 + 实测文字宽），因此面板随
    图幅缩小时字号同步缩小、面板始终包住内容；scale=1 时与旧版绝对常量逐点等价。
    """
    x0, y0, x1, y1 = rect
    ax_w_in, ax_h_in = _axes_inch(ax)
    s = metrics["scale"]

    def _fx(v_in):          # 英寸 -> axes 分数（x 方向）
        return v_in / ax_w_in

    def _fy(v_in):          # 英寸 -> axes 分数（y 方向）
        return v_in / ax_h_in

    _draw_panel(ax, rect, alpha=alpha, zorder=20)
    # 标题「图例」
    ax.text(x0 + _fx(LEGEND_PAD_X_IN * s), y1 - _fy(LEGEND_TITLE_BELOW_TOP_IN * s),
            "图例", transform=ax.transAxes,
            ha="left", va="center", fontsize=metrics["fs_title"], fontweight="bold",
            color="#1a1a1a", zorder=21)
    # 线段样块：白色底衬 + 彩色线（与行政边界线型一致）
    xs = x0 + _fx(LEGEND_PAD_X_IN * s)
    xe = x0 + _fx((LEGEND_PAD_X_IN + LEGEND_SAMPLE_W_IN) * s)
    yl = y0 + _fy(LEGEND_ROW_Y_IN * s)
    ax.add_line(Line2D([xs, xe], [yl, yl], transform=ax.transAxes,
                       color="white", linewidth=4.0, solid_capstyle="round",
                       zorder=21))
    ax.add_line(Line2D([xs, xe], [yl, yl], transform=ax.transAxes,
                       color=boundary_color, linewidth=2.0, solid_capstyle="round",
                       zorder=22))
    label = f"行政边界（{region_name or '研究区'}）"
    ax.text(x0 + _fx((LEGEND_PAD_X_IN + LEGEND_SAMPLE_W_IN + LEGEND_GAP_IN) * s), yl,
            label, transform=ax.transAxes,
            ha="left", va="center", fontsize=metrics["fs_label"],
            color="#1a1a1a", zorder=21)



def _render_map_core(fig, ax, rgb, extent, title, subtitle,
                     graticule, scale_bar, north_arrow, legend,
                     boundary=None, boundary_color="#d81e06", boundary_lw=1.6,
                     region_name=None, legend_pos=LEGEND_DEFAULT_POS,
                     panel_alpha=PANEL_ALPHA, title_fs=None, sub_fs=None,
                     credit=None):
    """把专题图元素绘制到给定 Figure / Axes。"""
    west, south, east, north = extent
    ax.imshow(rgb, extent=[west, east, south, north], origin="upper", zorder=0)

    if graticule:
        _draw_graticule(ax, west, south, east, north)
    else:
        ax.set_xticks([])
        ax.set_yticks([])

    rings = _boundary_rings_wgs84(boundary) if boundary is not None else []
    if rings:
        _draw_boundary_rings(ax, rings, color=boundary_color, lw=boundary_lw)

    for spine in ax.spines.values():
        spine.set_linewidth(1.0)
        spine.set_edgecolor("black")

    # 装饰件：比例尺固定在左下角（自带面板），指北针固定在右上角
    reserved = []
    scale_rect = None
    if scale_bar:
        geom = _scale_bar_geom(west, east, south, north)
        scale_rect = _draw_scale_bar(ax, geom, west, east, panel_alpha=panel_alpha)
        reserved.append(scale_rect)
    if north_arrow:
        _draw_north_arrow(ax)
        reserved.append(_north_arrow_reserved(ax))
    # 图例默认固定右下角（不透明面板压住底下的区界线）；`auto` 才走避让评分
    if legend and rings:
        metrics = _legend_metrics(ax, region_name)
        if legend_pos == "auto":
            nrings = _normalize_rings(rings, west, east, south, north)
            rect, _cost = _place_legend(nrings, reserved, metrics["w"], metrics["h"])
        else:
            rect = _legend_anchor(legend_pos, metrics["w"], metrics["h"])
            # 极窄图幅下图例会变宽，可能横向压到左下角的比例尺卡片 ——
            # 抬到卡片之上（保留「右下角」的语义，而不是挪到别处）
            if scale_rect is not None and _overlap_area(rect, scale_rect) > 0:
                dy = scale_rect[3] + LEGEND_MARGIN - rect[1]
                if rect[3] + dy <= 1.0:
                    rect = (rect[0], rect[1] + dy, rect[2], rect[3] + dy)
        _draw_legend(ax, rect, boundary_color, region_name, metrics, alpha=panel_alpha)

    _draw_title_and_credit(fig, ax, title, subtitle, title_fs=title_fs,
                           sub_fs=sub_fs, credit=credit)


def make_map(tif_path, out_path=None, title=None, subtitle=None, dpi=DEFAULT_MAP_DPI,
             max_side=DEFAULT_MAX_SIDE, graticule=True, scale_bar=True, north_arrow=True,
             legend=True, pdf=False, boundary=None, boundary_color="#d81e06",
             boundary_lw=1.6, supersample=DEFAULT_SUPERSAMPLE,
             legend_pos=LEGEND_DEFAULT_POS, panel_alpha=PANEL_ALPHA):
    """把 GeoTIFF 渲染成 ArcGIS 风格专题地图。

    参数
    ----
    tif_path : 输入 GeoTIFF（建议来自本 skill，含血缘元数据标签）。
    out_path : 输出图片路径（默认 <tif 名>_map.png）。
    title / subtitle : 标题 / 副标题；缺省时从 GeoTIFF 元数据推断。
    dpi : 输出分辨率。
    max_side : 渲染最长边像素上限（降采样，控制内存与出图速度）。
    graticule / scale_bar / north_arrow / legend : 四大装饰开关。
    boundary : 研究区行政边界（DataV GeoJSON / geometry / 路径 / JSON 串）；
        GCJ-02 会自动反算为 WGS84 后叠加，用于绘制论文「研究区」红线。
    boundary_color / boundary_lw : 边界线颜色与线宽。
    supersample : 源读取超采样倍数（默认 2.0）。1.0 = 读入即显示尺寸（最快）；越大越接近
        「从全分辨率源重采样」的观感，但读取越慢（见 load_display_array 的微基准）。
        v1.7.8 起这一跳（降采样读）改用 average 核：更快且无混叠。
    legend_pos : 图例位置。`se`（默认）= **固定右下角**，白底面板压住底下的区界线；
        `auto` = 按区界压力自动避让；也可强制 `sw/ne/nw/e/w/s/n`。
    panel_alpha : 比例尺 / 图例白色面板的不透明度（默认 1.0 = 完全不透明，
        0 = 不要面板）。
    pdf : 在输出主图（PNG）之外**额外**输出一份同名 PDF（v1.7.0 起；此前会把主图直接
        替换成 PDF，PNG 丢失）。

    返回主图（PNG）路径。
    """
    tif_path = os.path.abspath(tif_path)
    if out_path is None:
        base = os.path.splitext(tif_path)[0]
        out_path = base + "_map.png"
    out_path = os.path.abspath(out_path)
    # 【v1.7.0】此前 pdf=True 时 out_path 会被改成 ".pdf"，PNG 从未写出 —— 与文档
    # 「同时输出 PDF」不符。现恒以 out_path 为主图（PNG），PDF 作为附加产物。
    # 若用户给的主路径本身就是 .pdf，则主图改用同名 .png，避免一种丢失另一种。
    root, ext = os.path.splitext(out_path)
    if pdf and ext.lower() == ".pdf":
        out_path = root + ".png"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    _init_fonts()

    rgb, extent, src_crs, tags = load_display_array(
        tif_path, max_side=max_side, supersample=supersample)
    H, W = rgb.shape[:2]

    # ---- 默认标题：仅保留主标题，副标题不再自动生成（v1.5.3 起）----
    # 旧版会自动拼接「Zoom 17 | Esri World Imagery | 时间」作为副标题，科研配图
    # 中显得杂乱，故移除。如需副标题可显式通过 --subtitle 传入。
    region = tags.get("region_name")
    if title is None:
        title = (f"{region} 遥感影像专题图" if region else "卫星影像专题图")

    # ---- 版心：保持影像 1 px = 1/dpi 英寸，四周留绝对英寸边距 ----
    # 上边距由标题栈实际需要的高度定（v1.7.7）—— 固定 0.5in 时大图 + 副标题会被裁。
    img_w_in = W / dpi
    img_h_in = H / dpi
    fig_w = img_w_in + LEFT_IN + RIGHT_IN
    title_fs, sub_fs, top_in = _title_layout(img_w_in, fig_w, title, subtitle, dpi)
    fig_h = img_h_in + top_in + BOTTOM_IN
    left = LEFT_IN / fig_w
    bottom = BOTTOM_IN / fig_h
    width = img_w_in / fig_w
    height = img_h_in / fig_h

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi)
    ax = fig.add_axes([left, bottom, width, height])
    _render_map_core(fig, ax, rgb, extent, title, subtitle,
                     graticule, scale_bar, north_arrow, legend,
                     boundary=boundary, boundary_color=boundary_color,
                     boundary_lw=boundary_lw, region_name=region,
                     legend_pos=legend_pos, panel_alpha=panel_alpha,
                     title_fs=title_fs, sub_fs=sub_fs, credit=_credit_line(tags))
    # 【v1.7.8】PNG 存盘用 zlib 级别 3（而非 matplotlib 默认的 6）。微基准
    # （2424×2296 影像、2600×2720 画布）：1123 ms / 18.56 MB → 1002 ms / 18.56 MB ——
    # **体积一模一样、产物逐像素最大差 0**，纯提速约 11%。存盘占出图总时长约 42%
    # （实测分解：读图 56% / 绘制 2% / 存盘 42%），所以这是当前出图链路上最划算的一项。
    # 不再往下调到 1：虽再快约 9%，但体积涨到 23.13 MB（+25%）。
    save_kw = ({"pil_kwargs": {"compress_level": PNG_COMPRESS_LEVEL}}
               if os.path.splitext(out_path)[1].lower() == ".png" else {})
    fig.savefig(out_path, dpi=dpi, bbox_inches=None, pad_inches=0.0, **save_kw)

    if pdf:
        pdf_path = os.path.splitext(out_path)[0] + ".pdf"
        fig.savefig(pdf_path, dpi=dpi, bbox_inches=None, pad_inches=0.0)
        plt.close(fig)
        print(f"  专题图 PDF: {pdf_path}")

    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    # allow_abbrev=False：禁止 `--boundary` 被前缀匹配成 `--boundary-color`
    ap = argparse.ArgumentParser(description="GeoTIFF -> ArcGIS 风格遥感影像专题地图",
                                 allow_abbrev=False)
    # input 置为可选，等 --version 判断完再校验 —— 否则 `make_map.py --version`
    # 会先被「缺少 input」挡住，与文档所述的「--version 均支持」不符。
    ap.add_argument("input", nargs="?", default=None, help="输入 GeoTIFF 路径")
    ap.add_argument("--output", default=None, help="输出图片（默认 <输入>_map.png）")
    ap.add_argument("--title", default=None, help="专题图标题（缺省从元数据推断）")
    ap.add_argument("--subtitle", default=None, help="副标题")
    ap.add_argument("--dpi", type=int, default=DEFAULT_MAP_DPI,
                    help=f"输出分辨率（默认 {DEFAULT_MAP_DPI}）")
    ap.add_argument("--max-side", type=int, default=DEFAULT_MAX_SIDE,
                    help=f"渲染最长边像素上限（默认 {DEFAULT_MAX_SIDE}）")
    ap.add_argument("--no-graticule", action="store_true", help="不画经纬网")
    ap.add_argument("--no-scale-bar", action="store_true", help="不画比例尺")
    ap.add_argument("--no-north-arrow", action="store_true", help="不画指北针")
    ap.add_argument("--no-legend", action="store_true", help="不画图例")
    ap.add_argument("--pdf", action="store_true", help="同时输出 PDF 版")
    ap.add_argument("--boundary", default=None,
                    help="研究区行政边界（DataV GeoJSON / geometry / 文件路径），自动 GCJ-02→WGS84 叠加红线")
    ap.add_argument("--boundary-color", default="#d81e06", help="边界线颜色（默认 #d81e06）")
    ap.add_argument("--boundary-lw", type=float, default=1.6, help="边界线宽（默认 1.6）")
    ap.add_argument("--legend-pos", default=LEGEND_DEFAULT_POS,
                    choices=["auto", "se", "sw", "ne", "nw", "e", "w", "s", "n"],
                    help=f"图例位置：{LEGEND_DEFAULT_POS}=固定右下角（默认）；"
                         f"auto=按区界压力自动避让")
    ap.add_argument("--panel-alpha", type=float, default=PANEL_ALPHA,
                    help=f"比例尺/图例白色面板不透明度（默认 {PANEL_ALPHA}=全不透明；0=不要面板）")
    ap.add_argument("--supersample", type=float, default=DEFAULT_SUPERSAMPLE,
                    help=f"源读取超采样倍数（默认 {DEFAULT_SUPERSAMPLE}；1.0=读入即显示尺寸最快）")
    ap.add_argument("--version", action="store_true", help="打印版本")
    args = ap.parse_args()

    if args.version:
        print("cn-satellite-imagery / make_map", __version__)
        return
    if not args.input:
        ap.error("缺少输入 GeoTIFF（--version 除外）")

    t0 = time.time()
    out = make_map(args.input, args.output, title=args.title, subtitle=args.subtitle,
                   dpi=args.dpi, max_side=args.max_side,
                   graticule=not args.no_graticule,
                   scale_bar=not args.no_scale_bar,
                   north_arrow=not args.no_north_arrow, pdf=args.pdf,
                   legend=not args.no_legend,
                   boundary=args.boundary, boundary_color=args.boundary_color,
                   boundary_lw=args.boundary_lw, supersample=args.supersample,
                   legend_pos=args.legend_pos, panel_alpha=args.panel_alpha)
    print(f"专题地图: {out}（用时 {time.time() - t0:.1f}s）")


if __name__ == "__main__":
    main()
