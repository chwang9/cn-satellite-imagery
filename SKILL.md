---
name: cn-satellite-imagery
agent_created: true
version: 1.7.9
description: "输入中国区县/乡镇级地名或 6 位 adcode，下载卫星影像并拼接为带地理参考的 GeoTIFF
  （Esri World Imagery，原生 EPSG:3857，默认 zoom=17、最高 18，可选重投影 EPSG:4326 与 cn-dem 对齐）；
  可一键生成 ArcGIS 风格遥感专题地图（图示比例尺 / 罗盘指北针 / 经纬网 / 标题 / 图例 / 研究区区界红线，PNG/PDF）。
  仅支持区县级及以下：省/地级市硬拒绝并列出其下辖区县；同名区划自动消歧。内置瓦片并发下载与本地缓存、
  瓦片数上限保护、匀色与消拼缝、失败瓦片空洞填充、多影像源（--source）；每张产物写入血缘元数据。
  触发场景：要卫星影像 / 遥感底图 / 卫星地图 / 区划影像 / 航拍底图 / 三维地形贴图 / 专题地图 / 论文研究区图。"
argument-hint: "<区划名 或 adcode> [--output 路径] [--zoom 18] [--parent 上级] [--epsg 4326] [--map] [--dry-run]"
---

# CN Satellite Imagery（Esri World Imagery → GeoTIFF / 专题地图）

> 下载中国**区县级及以下**卫星影像并拼接为带地理参考的 GeoTIFF，原生 EPSG:3857（无损画质），
> 默认 zoom=17（最高 18）；`--map` 一键生成 ArcGIS 风格专题地图。产物带血缘元数据。

## 参考资料（按需检索，勿整篇读入）

| 文件 | 内容 | 何时读 |
|---|---|---|
| `references/params.md` | 全部 CLI 参数（按分组，含默认值与适用入口） | 需确认某参数默认值 / 适用入口时 |
| `references/performance.md` | v1.6.0 实测性能基线、`--supersample` 权衡、进度交互样例 | 性能/内存调优时 |
| `references/troubleshooting.md` | 排错表、限流容错、维护陷阱（21 条）、自检方式 | **改代码前必读维护陷阱**；出错排查时 |
| `references/changelog.md` | v1.0.0 → 当前的完整更新日志 | 需要历史行为 / 版本差异时 |

版本遵循语义化 `MAJOR.MINOR.PATCH`；发版三处同步：frontmatter `version`、脚本 `__version__`、产物元数据 `skill_version`，并在 changelog 留痕。

> 面向仓库外部使用者（GitHub 首页）的完整说明见根目录 **`README.md`**（安装 / 快速开始 / 架构图 /
> 性能实测 / 免责声明）；本文件面向技能运行时，强调触发场景与硬性规则。

## 环境

优先复用 geo 隔离环境（已含 rasterio / mercantile / requests / Pillow / numpy）：

```bash
PY=C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
S=C:/Users/wangch/.workbuddy/skills/cn-satellite-imagery/scripts
```

（若 `default` 缺包，用 `satimg` 环境：`C:/Users/wangch/.workbuddy/binaries/python/envs/satimg/Scripts/python.exe`）

## 快速上手

```bash
# 地名 -> 影像（默认 zoom=17；EPSG:3857；消拼缝已开，匀色默认关闭）
$PY $S/satellite_imagery_cn.py 瑶海区
# 18 级 / adcode / 输出路径 / 同名消歧
$PY $S/satellite_imagery_cn.py 瑶海区 --zoom 18
$PY $S/satellite_imagery_cn.py 340102 --output d:/img/yaohai.tif
$PY $S/satellite_imagery_cn.py 朝阳区 --parent 长春市
# 重投影到 WGS84（与 cn-dem 的 DEM 叠加对齐）
$PY $S/satellite_imagery_cn.py 瑶海区 --epsg 4326
# 只预检（瓦片数 / 体量 / 是否超限），不下载
$PY $S/satellite_imagery_cn.py 瑶海区 --zoom 17 --dry-run
# 内存紧张：调小预算自动走流式（峰值内存与面积解耦）
$PY $S/satellite_imagery_cn.py 瑶海区 --mem-budget-mb 512
# 自定义范围（bbox 模式不受区县级护栏限制）
$PY $S/download_imagery.py --bbox "117.2,31.8,117.45,32.05" --zoom 16 --output x.tif
```

分步（一般无需）：`resolve_region.py 瑶海区 --json`（地名 → WGS84 bbox）→ `download_imagery.py --bbox ...`。

## 核心规则

- **行政区级护栏（v1.3 起，硬性）**：一键入口仅允许**区县级及以下**；省/地级市一律拒绝（退出码 3）
  并列出其下辖区县供选用。不提供 `--force` 绕过；更大/自定义范围走 bbox 模式（自行把控范围）。
- **同名区划不静默选错**：命中多个候选时列出并要求 `--parent` 或直接用 adcode 消歧。
  用户没说清范围时，先问清是哪个区县/市或让其给经纬度，不要猜。
- **坐标系**：默认 3857（Esri 瓦片原生，零重采样画质最佳）；与 cn-dem 叠加用 `--epsg 4326`（一次双线性重投影）。
  DataV 边界为 GCJ-02，已反算 WGS84，与 Esri 底图对齐。
- **瓦片上限**：`--max-tiles` 默认 60000；区县 z17≈5.5k、z18≈2.2 万瓦片（默认可跑），z19 需 `--force`。

## 输出规格

| 项 | 值 |
|---|---|
| 坐标系 | EPSG:3857 默认；`--epsg 4326` 可选 |
| 波段 | 3（RGB）uint8 |
| 压缩 | JPEG（YCbCr）+ tiled（256×256）+ 金字塔概览（按尺寸自适应）；裁切/重投影保留创建选项 |
| 地理参考 | 完整（精确裁切到请求 bbox） |
| 元数据 | 血缘标签：`skill_version` / `source` / `region_name` / `adcode` / `admin_level` / `zoom` / `bbox_wgs84` / `generated_at` / `failed_tiles` / `blank_tiles` |
| 命名 | `<区划名>_satellite_z<zoom>.tif` |

## 专题地图（`--map`；或 `make_map.py <tif>`）

- 装饰：标题（默认取 `region_name`，紧贴影像上沿）、**右上角**罗盘指北针、
  **左下角**图示比例尺、DMS 经纬网、**右下角**图例（仅叠边界时）、
  红色研究区区界（`--map` 默认叠本区县边界，`--no-boundary` 关闭）、数据来源注记（取 TIF 血缘
  标签 `source`，v1.7.7 起不再写死 Esri）。
  输出 PNG（默认 200 DPI）；`--pdf` **额外**输出同名 PDF（PNG 主图恒生成）。
- 渲染先按 `--supersample`（默认 2.0）降采样读源，再重投影到 EPSG:4326，保证经纬网为真经纬度网格。
- **位置约定（v1.7.4）**：图内四角装饰件**位置固定**，不随区界形状漂移 ——
  比例尺钉在左下角、图例钉在右下角。二者各有一块**白色衬底面板**（`--panel-alpha`，默认
  1.0 全不透明），因此即使研究区区界斜穿右下角，红线也会被图例面板压住、框内不出现红线。
- **比例尺（v1.7.3 真值 / v1.7.4 面板 / v1.7.5 越界修复 / v1.7.6 卡片几何）**：真值按 WGS84 椭球
  在**尺子自身纬度**上算（`N·cosφ·π/180`），实测偏差 ±0.000%；总长取漂亮数后按「能整除成漂亮数
  的最大段数」分段（5 km → 5 × 1 km），黑白交替分段 + 逐刻度居中标注。**卡片矩形由「条带 +
  下伸刻度 + 上方标注」的实测渲染包围盒并集反推**（非字号估算），四周留 `PANEL_PAD_PT` ——
  所以卡片在任何字体/字号/DPI 下都必然包住内容；窄图幅会自动右移/缩短条带，保证卡片不被裁。
  面板 `zorder` **显式**指定为 3.5（高于区界线 3/4、低于比例尺内容 5~8），不再依赖「同 zorder
  后画者在上」的插入顺序（v1.7.7）。
- **图例（v1.7.4 位置 / v1.7.7 版式）**：默认固定右下角。`--legend-pos auto` 可切回「按区界压力
  在 8 个锚点中自动避让」（代价 = 区界压力×1000 + 预留区重叠面积×100，同分按右下优先）；也可直接
  钉死 `sw/ne/nw/e/w/s/n`。**面板尺寸与字号由英寸基准 + 实测文字宽度反推**（`_legend_metrics`）：
  字号随轴宽在 `LEGEND_SCALE_RANGE` 内等比缩放，面板取 `max(LEGEND_MIN_*, 实测需求)` —— 图幅
  变小或区县名变长时文字都不会顶出白框；极窄图幅下若图例横向压到左下角比例尺卡片，会把图例
  **整体抬高**避让（保持「右下角」语义）。

```bash
$PY $S/satellite_imagery_cn.py 瑶海区 --map --title "瑶海区遥感影像专题图" --map-dpi 300
$PY $S/make_map.py yaohai.tif --output yaohai_map.png --title "瑶海区专题图"   # 已有 TIF，只出图
$PY $S/make_map.py yaohai.tif --supersample 1.0                                # 最快出图
$PY $S/make_map.py yaohai.tif --boundary-lw 2.4 --boundary-color "#0066cc"     # 区界线样式
$PY $S/make_map.py yaohai.tif --legend-pos auto                              # 图例改为自动避让区界
$PY $S/make_map.py yaohai.tif --panel-alpha 0.9                             # 面板半透明，底图微透
$PY $S/make_map.py yaohai.tif --panel-alpha 0                               # 不要面板
```

## 常用参数速查（完整表见 `references/params.md`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--zoom` | `17` | 最高 18；z19 需 `--force` |
| `--epsg` | `3857` | `4326` 重投影 WGS84 |
| `--parent` | — | 同名区划消歧 |
| `--pad` | `0.10` | 下载范围相对行政区包围盒外扩比例（研究区留白） |
| `--source` | `esri` | `google` 需代理/境外 |
| `--balance-mode` | `none` | `offset` 仅亮度偏移 / `percentile` 5/95% 拉伸（可能放大色差） |
| `--feather` | `24`（bbox 模式 `0`） | 消拼缝像素宽，建议 16~32 |
| `--no-fill-holes` / `--fill-blanks` | off | 失败瓦片留黑 / 纯色瓦片也填充（默认用全局均值填失败瓦片） |
| `--legend-pos` | `se` | 图例位置：默认固定右下角；`auto` 按区界压力自动避让；或 `sw/ne/nw/e/w/s/n` |
| `--panel-alpha` | `1.0` | 比例尺/图例白色面板不透明度（1.0=全不透明，0.9=底图微透，0=不要面板） |
| `--mem-budget-mb` | `2048` | 超出自动转流式 |
| `--max-workers` | `12` | 并发下载线程 |
| `--dry-run` | off | 只预检不下载 |
| `--no-cache` / `--clean-cache` | off | 跳过 / 清空瓦片缓存 |
| `--version` | — | 三个入口均可用 |

## 缓存位置

| 路径 | 内容 | 失效方式 |
|---|---|---|
| `scripts/_tiles/` | 瓦片缓存：esri 为 `{z}_{x}_{y}.jpg`，其他源为 `{source}_{z}_{x}_{y}.jpg`（按源隔离） | `--clean-cache`；坏缓存自动重下 |
| `scripts/_boundary/` | DataV 行政边界缓存（GCJ-02，已反算 WGS84） | 手动删除；403 限流时优先复用 |
| `scripts/_adcode_cache.json` | 区划索引缓存（TTL 7 天） | `--refresh` 或到期 |

## 脚本一览

- `satellite_imagery_cn.py`：一键入口（解析地名 → 下载 → 血缘元数据 → 可选专题图），启动即打印预检。
- `resolve_region.py`：地名/adcode → WGS84 bbox（china-division 索引 + DataV v3 边界，GCJ-02→WGS84）。
- `download_imagery.py`：bbox + zoom → GeoTIFF（窗口化落盘，内存与面积解耦；匀色/消拼缝/多源/重投影）。
- `make_map.py`：GeoTIFF → ArcGIS 风格专题图 PNG/PDF（自动注册系统 CJK 字体）。

## 最近更新（完整日志见 `references/changelog.md`）

- **v1.7.9**：**warp 多线程**（出图链路与整幅重投影同源根因）。① `reproject` 此前一直单线程；
  **关键坑：设 `GDAL_NUM_THREADS` 或用 `rasterio.Env` 包住对它完全无效**（实测 1/2/4/8/ALL_CPUS
  全为 960~990 ms 无差异），必须用 `reproject(num_threads=…)` 显式传参。② 收益：出图链路
  387 → 107 ms（8 线程，**3.6×**）、整幅 3857→4326（23943×29724 源）**53.3 s → 24.9 s（2.14×）**，
  两者**逐像素 max|Δ| = 0**（GDAL 按块独立计算）；端到端出图 **2.2 s → 1.64 s（−25%）**。
  ③ 线程上限取 `min(cpu_count, 8)`：16 线程在整幅场景反而 **慢 30%**（超额订阅争抢），8 是拐点。
  ④ bbox 模式血缘补 `zoom` 标签（此前只有一键入口写，`--bbox` 直接下载的产物缺）。
  ⑤ 新增根目录 **`README.md`**（面向 GitHub / 外部使用者的完整说明）。
- **v1.7.8**：**全技能优化审视**（流程 = 通读 → 静态测量 + 微基准 → 按数据定清单（含放弃项）
  → 逐项改 → 逐项回归）。① **出图性能**：先量出阶段分解 —— 读图 **56%** / 绘制 **2%** /
  存盘 **42%**，据此**没碰任何版式代码**，只优化两个大头：降采样读的重采样核
  `bilinear`→`average`（**1.41×**，且消除 4.9× 降采样的混叠，更快且更对）、PNG 存盘
  zlib 级别 6→3（**1.21×，体积完全不变、产物逐像素差 0**）→ 端到端 **2.8s → 2.2s（−21%）**。
  ② 删掉 v1.7.7 遗留的**自相矛盾死代码**（`os.environ["PYTHONDONTWRITEBYTECODE"]`：注释写着
  「无效」却仍在执行）。③ **参数面同源**：一键入口 `--source` 的 choices 改取
  `download_imagery.SOURCES`（原先硬编码，新增影像源时会静默不认）、`--map-dpi`/`--max-side`
  改取 `make_map.DEFAULT_MAP_DPI`/`DEFAULT_MAX_SIDE`。④ `src_path[:-4]` → `os.path.splitext`
  （`.tiff` 会切错）；`feather_mosaic` 的缝数改为统计**实际处理**数（`--feather 300` 时原先
  报数偏大）；`flip_y()` 标注为 TMS 扩展点（当前配置下运行期不可达）。⑤ DRY 收敛 3 处：
  `_axes_inch()` / `_base_title_fs()` / `_text_width_in()`，均为**代数等价**改写（7/7 断言通过）。
- **v1.7.7**：**全技能代码审计**（4 脚本 + 文档）——① **图例面板溢出**：面板尺寸写死 axes 分数、
  字号却是固定磅值，图幅变小/区县名变长时文字顶出白框（3.4in 轴宽溢出 0.68in）；改为**英寸基准 +
  实测文字宽反推**、字号随轴宽等比缩放，并加「极窄图幅图例抬高避开比例尺卡片」兜底。② **数据来源
  注记 / 元数据 `source` 写死 Esri**：`--source google` 时图面与血缘标签自相矛盾；改为读 TIF 血缘
  标签 / `SOURCES` 注册表。③ **标题栈被图幅上沿静默裁切**：上边距写死 0.5in，宽图幅下 14pt 标题
  超界；改为按实测文字宽/行高动态算上边距并收字号。④ **下载器「全部瓦片失败」判据不可达**
  （`n_fail == n` 恒假，首张探测瓦片失败会提前 `SystemExit`），告警形同虚设；改为按「拿到统计结果的
  瓦片数」判定，并新增「除探测瓦片外全失败」告警。⑤ 健壮性：`sys.dont_write_bytecode`（运行时设
  env 对解释器无效）、边界文件句柄改 `with` 关闭、`re.split` 弃用警告、比例尺面板 `zorder` 显式化。
- **v1.7.6**：比例尺卡片几何改由**实测渲染包围盒**反推（取代字号估算），保证卡片永远包住
  条带/刻度/标注；窄图幅自动右移或缩短条带，卡片不再被图幅边界裁掉。
- **v1.7.5**：修复比例尺条带**外框越界**（v1.7.4 回归）—— 条带用 blended transform 绘制，
  x 宽度必须是数据单位（经度差），误传 axes 分数导致外框宽 3.9×、伸出面板外。
- **v1.7.4**：图内装饰件**位置固定**（比例尺左下角 / 图例右下角）+ 共用白色衬底面板（新增
  `--panel-alpha`）—— 用「面板遮挡」替代 v1.7.3 的「挪位置避让」，红区界不再干扰图例阅读。
- **v1.7.3**：专题图出图质量 —— 比例尺改椭球真值（实测偏差 ±0.000%）+ 分段逐刻度标注；
  图例自动避让区界线（新增 `--legend-pos`）。
- **v1.7.2**：文档按 WorkBuddy 技能规范重构（SKILL.md 瘦身 + 详细资料迁入 `references/`）。
- **v1.7.1**：修复「切换影像源后静默命中旧源瓦片缓存」（缓存键加入影像源区分；esri 沿用旧命名，缓存零重下）。
- **v1.7.0**：稳健性大修 —— 裁切静默不生效、空洞填充色被拉暗、进度 100% 帧不打、`--version` 不可用、
  `--pdf` 语义（改为额外输出）、概览层按尺寸自适应。
- **v1.6.0**：成品 TIF 体积 8.8× 优化（jpeg q90 + tiled）、专题图读取 2.8× 加速、内存与面积解耦、
  进度按时间刷新 + ETA、新增 `--dry-run` / `--version`。
