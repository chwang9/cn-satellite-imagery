#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文区划名 / adcode -> WGS84 包围盒 (west, south, east, north)。

复用 cn-dem 的成熟方案：
- 名称索引: china-division@2.5.0 pcas-code.json（1 次请求拿省 / 市 / 县三级）
- 行政边界: 阿里 DataV GeoAtlas（GCJ-02），本地缓存
- GCJ-02 -> WGS84 反算，消除几百米偏移，使影像与 WGS84 底图对齐
"""
import sys
import json
import time
import math
import requests
from pathlib import Path

# 见 make_map.py 同名说明：运行时设环境变量对 `sys.dont_write_bytecode` 无效，必须直接改。
sys.dont_write_bytecode = True

BASE = "https://geo.datav.aliyun.com/areas_v3/bound"
PCAS_URL = "https://unpkg.com/china-division@2.5.0/dist/pcas-code.json"
CACHE = Path(__file__).parent / "_adcode_cache.json"
BOUNDARY_CACHE = Path(__file__).parent / "_boundary"
CACHE_TTL = 7 * 86400
CACHE_VERSION = 1
BACKOFF = (5, 15, 40)


class AmbiguousName(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("ambiguous")


# --------------------------------------------------------------------------
# 网络与索引
# --------------------------------------------------------------------------
def _get(url, timeout=30, retries=3, strict=False):
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
        except Exception:
            if i < retries:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
                continue
            return None
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            if strict:
                raise RuntimeError(f"DataV 限流(403): {url}")
            if i < retries:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
                continue
            return None
        if i < retries:
            time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
            continue
    return None


def _names(adcode):
    j = _get(f"{BASE}/{adcode}_full.json", retries=1, strict=True)
    if not j:
        return []
    return [{"adcode": f["properties"]["adcode"], "name": f["properties"].get("name", "")}
            for f in j.get("features", [])]


def _pad(code, width=6):
    return int(str(code).ljust(width, "0"))


def build_from_pcas(data):
    prov, cities, districts = [], [], []
    for p in data:
        pname = p["name"]
        prov.append({"adcode": _pad(p["code"]), "name": pname, "parent": None})
        for c in p.get("children", []):
            cname = c["name"]
            cities.append({"adcode": _pad(c["code"]), "name": cname,
                           "parent": pname, "province": pname})
            shown = pname if cname in ("市辖区", "县", "市", "省直辖县级行政区划") else cname
            for d in c.get("children", []):
                districts.append({"adcode": _pad(d["code"]), "name": d["name"],
                                  "parent": shown, "province": pname})
    if len(districts) < 2000:
        raise RuntimeError("备用索引数据不完整")
    sys.stderr.write(f"  索引来自备用源（1 次请求）: {len(prov)} 省 / "
                     f"{len(cities)} 市 / {len(districts)} 区县\n")
    return {"time": time.time(), "version": CACHE_VERSION,
            "province": prov, "city": cities, "district": districts}


def build_from_datav():
    from concurrent.futures import ThreadPoolExecutor
    sys.stderr.write("  备用源不可用，回退 DataV 级联（370+ 次请求，可能被限流）\n")
    prov = [dict(p, parent=None) for p in _names(100000)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        city_groups = list(ex.map(lambda p: _names(p["adcode"]), prov))
    cities = []
    for p, grp in zip(prov, city_groups):
        for c in grp:
            cities.append(dict(c, parent=p["name"], province=p["name"]))
    with ThreadPoolExecutor(max_workers=16) as ex:
        dist_groups = list(ex.map(lambda c: _names(c["adcode"]), cities))
    districts = []
    for c, grp in zip(cities, dist_groups):
        for d in grp:
            districts.append(dict(d, parent=c["name"], province=c.get("province")))
    return {"time": time.time(), "version": CACHE_VERSION,
            "province": prov, "city": cities, "district": districts}


def build_index():
    try:
        r = requests.get(PCAS_URL, timeout=60)
        if r.status_code == 200:
            return build_from_pcas(r.json())
    except Exception as e:
        sys.stderr.write(f"  备用索引源失败: {type(e).__name__} {e}\n")
    return build_from_datav()


def load_index(refresh=False):
    if not refresh and CACHE.exists():
        try:
            c = json.loads(CACHE.read_text(encoding="utf-8"))
            fresh = time.time() - c.get("time", 0) < CACHE_TTL
            if fresh and c.get("district") and c.get("version") == CACHE_VERSION:
                return c
        except Exception:
            pass
    idx = build_index()
    CACHE.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


def candidates(idx, name):
    name = name.strip()
    hits = []
    for level in ("province", "city", "district"):
        for f in idx[level]:
            if f["name"] == name or str(f["adcode"]) == name:
                hits.append((level, f))
    if hits:
        return hits
    for level in ("district", "city", "province"):
        for f in idx[level]:
            if name and name in f["name"]:
                hits.append((level, f))
    hits.sort(key=lambda x: len(x[1]["name"]))
    return hits


def describe(level, f):
    if level == "district" and f.get("parent"):
        return f"{f['name']} ({f['parent']} / {f.get('province')}) adcode={f['adcode']}"
    if level == "city" and f.get("parent"):
        return f"{f['name']} ({f['parent']}) adcode={f['adcode']}"
    return f"{f['name']} ({level}) adcode={f['adcode']}"


# --------------------------------------------------------------------------
# GCJ-02 -> WGS84
# --------------------------------------------------------------------------
A = 6378245.0
EE = 0.00669342162296594323


def _t_lat(x, y):
    r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    r += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return r


def _t_lon(x, y):
    r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    r += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return r


def in_china(lon, lat):
    return 73.66 < lon < 135.05 and 3.86 < lat < 53.55


def gcj02_to_wgs84(lon, lat):
    if not in_china(lon, lat):
        return lon, lat
    dlat = _t_lat(lon - 105, lat - 35)
    dlon = _t_lon(lon - 105, lat - 35)
    radlat = lat / 180 * math.pi
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180) / ((A * (1 - EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180) / (A / sqrtmagic * math.cos(radlat) * math.pi)
    mglat = lat + dlat
    mglon = lon + dlon
    return lon * 2 - mglon, lat * 2 - mglat


def bbox_of_geometry(geom):
    minlon, minlat, maxlon, maxlat = 1e9, 1e9, -1e9, -1e9

    def walk(c):
        if isinstance(c, (list, tuple)):
            if len(c) >= 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                lon, lat = gcj02_to_wgs84(float(c[0]), float(c[1]))
                nonlocal minlon, minlat, maxlon, maxlat
                if lon < minlon:
                    minlon = lon
                if lat < minlat:
                    minlat = lat
                if lon > maxlon:
                    maxlon = lon
                if lat > maxlat:
                    maxlat = lat
            else:
                for x in c:
                    walk(x)

    walk(geom.get("coordinates", []))
    return (minlon, minlat, maxlon, maxlat)


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------
def resolve(name, parent=None, refresh=False, list_only=False):
    """返回 list（list_only）或 dict：{level,name,adcode,parent,province,bbox}。
    同名歧义时抛 AmbiguousName。"""
    idx = load_index(refresh=refresh)
    hits = candidates(idx, name)
    if not hits:
        raise SystemExit(f"找不到区划: {name}（可加 --refresh 重建索引）")
    if parent:
        narrowed = [h for h in hits
                    if parent in (h[1].get("parent") or "")
                    or parent in (h[1].get("province") or "")]
        if narrowed:
            hits = narrowed
        else:
            sys.stderr.write(f"警告: --parent {parent} 未匹配，回退到全部候选\n")
    if list_only:
        return [{"level": l, **f} for l, f in hits]
    if len(hits) > 1:
        raise AmbiguousName([{"level": l, **f} for l, f in hits])
    level, f = hits[0]
    adcode = f["adcode"]
    BOUNDARY_CACHE.mkdir(parents=True, exist_ok=True)
    cached = BOUNDARY_CACHE / f"{adcode}.json"
    if cached.exists() and not refresh:
        data = json.loads(cached.read_text(encoding="utf-8"))
    else:
        data = _get(f"{BASE}/{adcode}_full.json") or _get(f"{BASE}/{adcode}.json")
        if not data:
            raise SystemExit(f"边界下载失败: {adcode}（DataV 可能限流，可稍后重试或 --refresh）")
        cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    geom = data["features"][0]["geometry"]
    bbox = bbox_of_geometry(geom)
    return {"level": level, "name": f["name"], "adcode": adcode,
            "parent": f.get("parent"), "province": f.get("province"), "bbox": bbox}


def main():
    pos, opts, flags = [], {}, set()
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--parent":
            opts["parent"] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            flags.add(a.lstrip("-"))
            i += 1
        else:
            pos.append(a)
            i += 1
    if not pos:
        print("用法: python resolve_region.py 瑶海区 [--parent 合肥市] [--list] [--refresh] [--json]")
        sys.exit(1)
    name = pos[0]
    parent = opts.get("parent")
    refresh = "refresh" in flags
    list_only = "list" in flags
    as_json = "json" in flags
    try:
        res = resolve(name, parent=parent, refresh=refresh, list_only=list_only)
    except AmbiguousName as e:
        print(f"「{name}」有多个同名区划，请消歧:")
        for c in e.candidates:
            print("  -", describe(c["level"], c))
        sys.exit(2)
    if list_only:
        for c in res:
            print(describe(c["level"], c))
        return
    if as_json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        b = res["bbox"]
        print(f"{res['name']} ({res['level']}, {res.get('parent') or '-'}) adcode={res['adcode']}")
        print(f"  WGS84 包围盒: {b[0]:.6f},{b[1]:.6f},{b[2]:.6f},{b[3]:.6f}")


if __name__ == "__main__":
    main()
