#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键：中文区划名 / adcode -> Esri 卫星影像 GeoTIFF。

用法:
  python satellite_imagery_cn.py 瑶海区
  python satellite_imagery_cn.py 340102 --zoom 18        # 仅支持区县级及以下
  python satellite_imagery_cn.py 朝阳区 --parent 长春市
  python satellite_imagery_cn.py 瑶海区 --output yaohai.tif --zoom 18
  python satellite_imagery_cn.py 瑶海区 --epsg 4326     # 重投影到 WGS84 与 cn-dem 对齐
  python satellite_imagery_cn.py 瑶海区 --feather 32     # 增强拼缝淡化
  python satellite_imagery_cn.py 瑶海区 --no-balance     # 关闭匀色（保留瓦片原色）
  python satellite_imagery_cn.py 瑶海区 --map            # 额外生成 ArcGIS 风格专题图
  python satellite_imagery_cn.py 瑶海区 --zoom 17 --dry-run   # 只预检瓦片数/体量，不下载
  python satellite_imagery_cn.py 瑶海区 --mem-budget-mb 512   # 内存紧张：走流式拼接
  python satellite_imagery_cn.py 瑶海区 --map --pdf           # 同时出 PNG + PDF 专题图
  python satellite_imagery_cn.py --version
注意: v1.3 起仅支持【区县级】及以下区划，省/地级市会被拒绝（提示改用其下辖区县或 --bbox）。
      v1.6.0 起长任务有阶段提示与带 ETA 的进度；成品 GeoTIFF 保持 jpeg+tiled（体积约为旧的 1/10）。
      v1.7.0 起 --pdf 为「额外输出」而非替换主图；--supersample 默认值取自 make_map。

依赖（Windows 隔离环境）:
  C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
需要: rasterio mercantile requests Pillow numpy
"""
import os
import sys
import json
import shutil
import tempfile
import datetime
import argparse

# 避免本 skill 脚本产生 __pycache__（Skill 包仅支持两级目录结构）。
# 运行时设 `PYTHONDONTWRITEBYTECODE` 无效（CPython 只在启动时读该变量初始化
# `sys.dont_write_bytecode`），必须直接改 `sys` —— 否则下面 import 的
# resolve_region / download_imagery /（延迟 import 的）make_map 仍会写 `__pycache__`。
# 【v1.7.8】删掉遗留的 `os.environ[...] = "1"`：它与上面这段说明自相矛盾、且从不生效。
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import resolve_region
from download_imagery import (build_mosaic, reproject_to_wgs84,
                             DEFAULT_MAX_TILES, DEFAULT_MAX_WORKERS,
                             DEFAULT_MEM_BUDGET_MB, DEFAULT_PROGRESS_SEC,
                             _build_session, print_plan, SOURCES,
                             __version__ as SKILL_VERSION)

from pathlib import Path


# 行政级别护栏：仅允许【区县级】及以下的区划下载。
# 省 / 地级市范围过大、瓦片量不可控，一律拒绝，避免误触发海量下载。
REJECT_LEVELS = ("province", "city")


def _guard_admin_level(info):
    """区县级护栏：省/地级市拒绝，并给出可执行的下一步提示。"""
    if info["level"] not in REJECT_LEVELS:
        return
    name = info["name"]
    if info["level"] == "province":
        print(f"ERROR: 当前技能仅支持下载【区县级】及以下的卫星影像，"
              f"「{name}」为省级行政区，范围过大、瓦片量不可控。")
    else:  # city
        print(f"ERROR: 当前技能仅支持下载【区县级】及以下的卫星影像，"
              f"「{name}」为地级市，瓦片量过大。")
        # 列出下属区县，便于直接选用（DataV 子级边界）
        try:
            kids = resolve_region._names(info["adcode"])
            if kids:
                print(f"  「{name}」下辖区县（任选其一即可下载）:")
                for k in kids[:40]:
                    print(f"    - {k['name']}  (adcode={k['adcode']})")
        except Exception:
            pass
    print(f"  若需更小范围，请用具体【区县】名称，或用 --bbox 框选自定小范围。")
    sys.exit(3)


def main():
    # allow_abbrev=False：禁止前缀缩写匹配，避免 `--boundary <path>` 被误当成
    # `--boundary-color <path>`（此前正是该陷阱把 JSON 路径当成了颜色值而崩溃）。
    ap = argparse.ArgumentParser(description="中文区划名 -> Esri 卫星影像 GeoTIFF",
                                 allow_abbrev=False)
    ap.add_argument("name", nargs="?", default=None,
                    help="区划名或 6 位 adcode，如 瑶海区 / 340102（仅支持区县级及以下）")
    ap.add_argument("--output", default=None,
                    help="输出 tif 路径（默认 <名>_satellite_z<zoom>.tif）")
    ap.add_argument("--zoom", type=int, default=17, help="缩放级别（默认 17，支持 18；19 级区县超瓦片上限需 --force）")
    ap.add_argument("--parent", default=None, help="同名消歧：指定上级市/省名")
    ap.add_argument("--force", action="store_true", help="超过瓦片上限也强制下载")
    ap.add_argument("--epsg", type=int, default=3857, help="输出坐标系（3857 或 4326，默认 3857）")
    ap.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    ap.add_argument("--timeout", type=float, default=15,
                    help="单瓦片下载超时秒（默认 15，失败快速跳过）")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    # 【v1.7.8】choices 取自 `download_imagery.SOURCES`（原先硬编码 ["esri","google"]）——
    # 否则给 download_imagery 新增影像源时，一键入口会静默地不认这个新源。
    ap.add_argument("--source", default="esri", choices=list(SOURCES.keys()),
                    help="影像源（默认 esri；google 国内需代理）")
    ap.add_argument("--no-cache", action="store_true", help="不读写瓦片缓存")
    ap.add_argument("--clean-cache", action="store_true", help="清空瓦片缓存后下载")
    ap.add_argument("--balance-mode", default="none",
                    choices=["percentile", "offset", "none"],
                    help="匀色策略：none=关闭（默认，保留原片色彩），offset=仅中位数亮度偏移，percentile=5/95%%线性拉伸（可能放大色差，谨慎使用）")
    ap.add_argument("--no-balance", action="store_true", help="关闭匀色（等价 --balance-mode none）")
    ap.add_argument("--feather", type=int, default=24,
                    help="拼缝淡化像素宽度（默认 24，0=关闭）")
    ap.add_argument("--no-fill-holes", action="store_true",
                    help="关闭空洞填充（下载失败的瓦片留黑，用于标记缺失）")
    ap.add_argument("--fill-blanks", action="store_true",
                    help="纯色/空瓦片（无数据区）也用均值填充")
    ap.add_argument("--refresh", action="store_true", help="重建区划索引")
    ap.add_argument("--map", action="store_true",
                    help="额外生成 ArcGIS 风格专题地图 PNG（含比例尺/指北针/经纬网/标题）")
    ap.add_argument("--title", default=None,
                    help="专题图标题（提供即自动出图，等价于 --map）")
    ap.add_argument("--map-output", default=None, help="专题图输出路径（默认 <tif>_map.png）")
    # 默认 None -> 运行时从 make_map 取，保持单一来源（同 --supersample/--legend-pos 的做法；
    # make_map 是延迟导入的，argparse 阶段还不能引用其常量）
    ap.add_argument("--map-dpi", type=int, default=None,
                    help="专题图分辨率（默认取 make_map 的 DPI）")
    ap.add_argument("--max-side", type=int, default=None,
                    help="专题图渲染最长边像素上限（默认取 make_map 的值）")
    ap.add_argument("--no-graticule", action="store_true", help="专题图不画经纬网")
    ap.add_argument("--no-scale-bar", action="store_true", help="专题图不画比例尺")
    ap.add_argument("--no-north-arrow", action="store_true", help="专题图不画指北针")
    ap.add_argument("--no-legend", action="store_true", help="专题图不画图例")
    ap.add_argument("--no-boundary", action="store_true",
                    help="专题图不叠加研究区行政边界（默认自动叠加本区县边界红线）")
    ap.add_argument("--boundary", default=None,
                    help="研究区边界 GeoJSON 路径/JSON 串（默认自动用本区县缓存边界）")
    ap.add_argument("--boundary-color", default="#d81e06", help="边界线颜色（默认 #d81e06）")
    ap.add_argument("--boundary-lw", type=float, default=1.6,
                    help="边界线宽（默认 1.6）")
    ap.add_argument("--legend-pos", default=None,
                    choices=["auto", "se", "sw", "ne", "nw", "e", "w", "s", "n"],
                    help="专题图图例位置：se=固定右下角（默认）；auto=按区界压力自动避让")
    # 默认 None -> 运行时从 make_map 取，保持单一来源（同 --supersample 的做法；
    # make_map 是延迟导入的，argparse 阶段还不能引用其常量）
    ap.add_argument("--panel-alpha", type=float, default=None,
                    help="比例尺/图例白色面板不透明度（默认 1.0=全不透明；0=不要面板）")
    ap.add_argument("--pdf", action="store_true",
                    help="除 PNG 外额外输出一份 PDF 专题图（矢量边框 + 栅格影像）")
    ap.add_argument("--pad", type=float, default=0.10,
                    help="下载区域相对行政区包围盒的外扩比例（默认 0.10，给研究区留出地理留白，避免边界贴图边）")
    ap.add_argument("--mem-budget-mb", type=int, default=DEFAULT_MEM_BUDGET_MB,
                    help=f"解码瓦片数组的内存预算 MB（默认 {DEFAULT_MEM_BUDGET_MB}）：超出则改流式（峰值内存与面积解耦）")
    ap.add_argument("--progress-sec", type=float, default=DEFAULT_PROGRESS_SEC,
                    help=f"下载进度刷新间隔秒（默认 {DEFAULT_PROGRESS_SEC}）")
    # 默认 None -> 运行时从 make_map.DEFAULT_SUPERSAMPLE 取，保持单一来源（v1.7.0）
    ap.add_argument("--supersample", type=float, default=None,
                    help="专题图源读取超采样倍数（默认 2.0；1.0=最快）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预检瓦片数/体量/是否超限，不下载不出图")
    ap.add_argument("--version", action="store_true", help="打印版本")
    args = ap.parse_args()

    if args.version:
        print("cn-satellite-imagery", SKILL_VERSION)
        return

    if not args.name:
        print("ERROR: 缺少区划名/adcode 参数（或与 --version 一起无需参数）")
        sys.exit(1)

    if not (0 <= args.zoom <= 19):
        print("ERROR: --zoom 应在 0-19")
        sys.exit(1)

    try:
        info = resolve_region.resolve(args.name, parent=args.parent, refresh=args.refresh)
    except resolve_region.AmbiguousName as e:
        print(f"「{args.name}」有多个同名区划，请消歧（--parent 或 adcode）:")
        for c in e.candidates:
            print("  -", resolve_region.describe(c["level"], c))
        sys.exit(2)

    # 区县级护栏：省/地级市一律拒绝，避免误触发海量下载
    _guard_admin_level(info)

    b = info["bbox"]
    # 外扩 pad 比例，给研究区留出地理留白（避免行政边界压在图边）
    pad = max(0.0, min(args.pad, 0.5))
    if pad > 0:
        w, h = b[2] - b[0], b[3] - b[1]
        dl_bbox = [b[0] - pad * w, b[1] - pad * h, b[2] + pad * w, b[3] + pad * h]
    else:
        dl_bbox = list(b)
    print(f"解析到: {info['name']}（{info['level']}，{info.get('parent') or '-'}，"
          f"adcode={info['adcode']}）")
    print(f"  行政区包围盒: {b[0]:.6f},{b[1]:.6f},{b[2]:.6f},{b[3]:.6f}")
    print(f"  下载包围盒(pad={pad:.0%}): {dl_bbox[0]:.6f},{dl_bbox[1]:.6f},"
          f"{dl_bbox[2]:.6f},{dl_bbox[3]:.6f}")

    out = os.path.abspath(args.output or f"{info['name']}_satellite_z{args.zoom}.tif")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    # 预检（对用户可见的「要下多少、大概多大、会不会超限」），--dry-run 到此为止
    print_plan(dl_bbox, args.zoom, args.max_tiles, prefix="[预检] ")
    if args.dry_run:
        print(f"[dry-run] 目标输出: {out}" + ("（另出专题图）" if (args.map or args.title) else ""))
        return

    cache_dir = Path(HERE) / "_tiles"
    if args.clean_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)
    if args.no_cache:
        cache_dir = Path(tempfile.mkdtemp(prefix="esri_"))

    session = _build_session(args.max_workers)

    region_meta = dict(
        skill="cn-satellite-imagery",
        skill_version=SKILL_VERSION,
        # 【v1.7.7】此前写死 "Esri World Imagery"，`--source google` 时元数据自相矛盾
        # （build_mosaic 随后会用 source_cfg["label"] 覆盖，但 dict 本身不该带错值）
        source=SOURCES.get(args.source, SOURCES["esri"])["label"],
        region_name=info["name"],
        adcode=str(info["adcode"]),
        admin_level=info["level"],
        zoom=str(args.zoom),
        bbox_wgs84=",".join(f"{v:.6f}" for v in dl_bbox),
        generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )

    mode = "none" if args.no_balance else args.balance_mode
    result = build_mosaic(dl_bbox, args.zoom, out, session, args.timeout, args.retries,
                          args.max_workers, cache_dir, args.max_tiles, args.force,
                          region_meta=region_meta, balance_mode=mode,
                          feather=args.feather, source=args.source,
                          fill_holes=not args.no_fill_holes,
                          fill_blanks=args.fill_blanks,
                          mem_budget_mb=args.mem_budget_mb,
                          progress_sec=args.progress_sec)

    if args.epsg == 4326:
        reproject_to_wgs84(result)

    import rasterio
    with rasterio.open(result) as ds:
        mb = os.path.getsize(result) / 1e6
        tags = ds.tags()
        print(f"\n完成: {result}")
        print(f"  尺寸: {ds.width} x {ds.height} px | CRS: {ds.crs} | 波段: {ds.count} (RGB)")
        print(f"  文件大小: {mb:.2f} MB")
        print(f"  影像源: {tags.get('source', '?')} | 失败瓦片: {tags.get('failed_tiles', '0')}"
              f" | 纯色瓦片: {tags.get('blank_tiles', '0')} | 已填充: {tags.get('filled_holes', '0')}")
        print(f"  血缘: skill_version={SKILL_VERSION} adcode={info['adcode']} zoom={args.zoom}")

    # 专题地图（ArcGIS 风格：比例尺 / 指北针 / 经纬网 / 标题）
    if args.map or args.title:
        import make_map
        mout = args.map_output or (os.path.splitext(out)[0] + "_map.png")
        boundary = None
        if not args.no_boundary:
            if args.boundary:
                # 显式指定：路径或 JSON 串直接透传，由 _boundary_rings_wgs84 自行解析
                boundary = args.boundary
            else:
                bpath = resolve_region.BOUNDARY_CACHE / f"{info['adcode']}.json"
                if bpath.exists():
                    boundary = json.loads(bpath.read_text(encoding="utf-8"))
        supersample = args.supersample
        if supersample is None:
            supersample = make_map.DEFAULT_SUPERSAMPLE
        legend_pos = args.legend_pos or make_map.LEGEND_DEFAULT_POS
        # 0 是合法值（= 不要面板），故用 is not None 判断而非 or
        panel_alpha = (make_map.PANEL_ALPHA if args.panel_alpha is None
                       else args.panel_alpha)
        # 【v1.7.8】与上面三项同理：不传就用 make_map 的默认值，不在两处各写一份数字
        map_dpi = make_map.DEFAULT_MAP_DPI if args.map_dpi is None else args.map_dpi
        max_side = make_map.DEFAULT_MAX_SIDE if args.max_side is None else args.max_side
        out_map = make_map.make_map(result, mout, title=args.title, dpi=map_dpi,
                          max_side=max_side,
                          graticule=not args.no_graticule,
                          scale_bar=not args.no_scale_bar,
                          north_arrow=not args.no_north_arrow,
                          legend=not args.no_legend,
                          pdf=args.pdf,
                          boundary=boundary, boundary_color=args.boundary_color,
                          boundary_lw=args.boundary_lw,
                          supersample=supersample,
                          legend_pos=legend_pos, panel_alpha=panel_alpha)
        print(f"\n专题地图: {out_map}")
        print("下一步提示: 想改标题/分辨率/装饰元素只需复用 TIF 重渲染，不必重下影像 ——")
        print(f'  <PY> make_map.py "{out}" --title "..." --map-dpi 300 '
              f'--boundary "{resolve_region.BOUNDARY_CACHE / (str(info["adcode"]) + ".json")}"')


if __name__ == "__main__":
    main()
