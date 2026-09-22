#!/usr/bin/env python3
# OffGridFinder: offline proximity search over OpenStreetMap data.
# Copyright (C) 2026 Zack (NullAngst)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
OffGridFinder

Download OpenStreetMap extracts (a state, a country), convert them into a
local SQLite index, and run proximity queries with no network connection:

    "Dollar General within 5 mi of 34.58, -83.33"
    "campgrounds within 2 mi of any part of the Appalachian Trail"
    "every gas station in the region, sorted by distance from my campsite"

Results can be chained: the results of one search can become the "measured
from" anchor of the next search.
"""

import argparse
import csv
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

try:
    import osmium
    import osmium.filter
    HAVE_OSMIUM = True
except ImportError:  # the GUI still runs for searching existing data
    osmium = None
    HAVE_OSMIUM = False

APP_NAME = "OffGridFinder"
APP_VERSION = "1.0.0"
SCHEMA_VERSION = "1"
GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/NullAngst/OffGridFinder)"

EARTH_R = 6371008.8          # mean earth radius, meters
M_PER_DEG = 111320.0         # meters per degree (latitude, approx.)
M_PER_DEG_MIN = 110574.0     # smallest meters per degree of latitude (for lower bounds)
UNITS = {"mi": 1609.344, "km": 1000.0, "m": 1.0, "ft": 0.3048}

MAX_GEOM_POINTS = 4000       # geometry vertices kept per feature (decimated above this)
MAX_CANDIDATES = 250000      # safety cap on rows pulled from the database per search
MAX_ANCHORS = 20000          # cap on "use all matches" anchors
PICKER_DISPLAY_LIMIT = 2000  # rows shown in the feature picker

# --------------------------------------------------------------------------
# What gets imported from OSM. Edit these sets to change what is indexed.
# --------------------------------------------------------------------------

# Checked in this order; the first matching key becomes the feature category.
PRIMARY_KEYS = (
    "shop", "amenity", "tourism", "historic", "leisure", "healthcare",
    "office", "craft", "natural", "waterway", "highway", "route", "place",
    "boundary", "landuse", "aeroway", "railway", "man_made", "emergency",
    "building",
)

TRAIL_HIGHWAYS = {"path", "footway", "track", "bridleway", "cycleway", "steps"}
HIGHWAY_POIS = {"trailhead", "rest_area", "services"}
ROUTE_TYPES = {"hiking", "foot", "walking", "bicycle", "mtb", "horse", "canoe"}
WATERWAY_LINES = {"river", "stream", "canal"}
AMENITY_NOISE = {
    "bench", "waste_basket", "bicycle_parking", "parking_space",
    "parking_entrance", "vending_machine", "grit_bin", "waste_disposal",
    "clock", "loading_dock", "motorcycle_parking", "letter_box", "lounger",
    "recycling", "telephone",
}
TOURISM_NOISE = {"information", "artwork"}
LEISURE_UNNAMED_OK = {
    "park", "nature_reserve", "picnic_table", "firepit", "slipway",
    "fishing", "marina", "dog_park", "swimming_area", "playground",
    "beach_resort", "bird_hide",
}
NATURAL_UNNAMED_OK = {
    "peak", "spring", "hot_spring", "waterfall", "cave_entrance", "volcano",
    "saddle", "arch", "geyser",
}
NATURAL_NEVER = {"coastline", "tree", "tree_row", "scrub", "grassland", "heath"}
NATURAL_LINES = {"ridge", "valley", "cliff", "arete"}
NATURAL_AREAS = {"water", "wood", "wetland", "beach", "bay", "glacier"}
LEISURE_AREAS = {"park", "nature_reserve", "common", "recreation_ground", "golf_course"}
TOURISM_AREAS = {"camp_site", "caravan_site", "theme_park", "zoo"}
LANDUSE_OK = {"forest", "recreation_ground", "reservoir", "meadow", "military"}
PLACE_OK = {
    "city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter",
    "locality", "isolated_dwelling", "island", "islet", "county", "state",
}
NAMED_ONLY_KEYS = {"man_made", "railway", "emergency", "building", "office", "craft"}
RAILWAY_OK = {"station", "halt", "tram_stop"}
AEROWAY_OK = {"aerodrome", "helipad", "airstrip"}

TAG_DROP_PREFIXES = (
    "tiger:", "gnis:", "nhd:", "NHD:", "source", "created_by", "note",
    "fixme", "FIXME", "massgis:", "lacounty:", "osak:", "import",
    "check_date", "ref:NPLG", "nysgissam:",
)

SEARCH_TAGS = (
    "name", "alt_name", "old_name", "official_name", "short_name", "loc_name",
    "brand", "operator", "ref", "cuisine", "addr:street", "addr:city",
)

# Extra words indexed for text search so "gas" finds amenity=fuel, etc.
SYNONYMS = {
    "amenity=fuel": "gas station petrol fuel",
    "shop=variety_store": "dollar store variety",
    "shop=supermarket": "grocery supermarket",
    "shop=convenience": "convenience store",
    "tourism=camp_site": "campground camping campsite",
    "tourism=caravan_site": "rv park campground camping",
    "amenity=fast_food": "fast food restaurant",
    "amenity=toilets": "restroom bathroom toilet",
    "amenity=drinking_water": "drinking water spigot potable",
    "amenity=water_point": "water fill potable",
    "highway=path": "trail path",
    "highway=footway": "trail footpath",
    "highway=track": "trail track",
    "highway=bridleway": "trail horse",
    "highway=trailhead": "trailhead trail",
    "route=hiking": "trail hiking route",
    "route=foot": "trail walking route",
    "route=mtb": "trail mountain bike route",
    "tourism=hotel": "hotel lodging",
    "tourism=motel": "motel hotel lodging",
    "amenity=pharmacy": "pharmacy drugstore",
    "leisure=slipway": "boat ramp launch slipway",
    "amenity=charging_station": "ev charger charging",
    "shop=hardware": "hardware store",
    "amenity=hospital": "hospital emergency er",
}

PRESET_ANY = "(any category)"
PRESETS = {
    PRESET_ANY: [],
    "Grocery / supermarket": [("shop", ["supermarket", "grocery", "greengrocer", "wholesale"])],
    "Convenience store": [("shop", ["convenience"])],
    "Dollar / variety store": [("shop", ["variety_store", "discount"])],
    "Gas station": [("amenity", ["fuel"])],
    "EV charging": [("amenity", ["charging_station"])],
    "Restaurant / fast food / cafe": [("amenity", ["restaurant", "fast_food", "cafe", "food_court"])],
    "Bar / pub": [("amenity", ["bar", "pub", "biergarten"])],
    "Hotel / motel / lodging": [("tourism", ["hotel", "motel", "guest_house", "hostel", "chalet",
                                             "apartment", "alpine_hut", "wilderness_hut"])],
    "Campground / RV park": [("tourism", ["camp_site", "caravan_site", "camp_pitch"])],
    "Trail (named)": [("highway", ["path", "footway", "track", "bridleway", "cycleway", "steps"]),
                      ("route", sorted(ROUTE_TYPES))],
    "Trailhead": [("highway", ["trailhead"])],
    "Park / forest / protected area": [("leisure", ["park", "nature_reserve"]),
                                       ("boundary", ["national_park", "protected_area"]),
                                       ("landuse", ["forest"])],
    "Viewpoint / attraction / museum": [("tourism", ["viewpoint", "attraction", "museum", "gallery",
                                                     "theme_park", "zoo", "aquarium"])],
    "Historic site": [("historic", None)],
    "Waterfall / spring / peak / cave": [("natural", ["waterfall", "spring", "hot_spring", "peak",
                                                      "cave_entrance"]),
                                         ("waterway", ["waterfall"])],
    "River / stream / lake": [("waterway", ["river", "stream", "canal"]), ("natural", ["water"])],
    "Drinking water": [("amenity", ["drinking_water", "water_point"])],
    "Toilets / showers": [("amenity", ["toilets", "shower"])],
    "Picnic / shelter / fire pit": [("tourism", ["picnic_site"]), ("amenity", ["shelter", "bbq"]),
                                    ("leisure", ["picnic_table", "firepit"])],
    "Pharmacy": [("amenity", ["pharmacy"]), ("healthcare", ["pharmacy"])],
    "Hospital / clinic / urgent care": [("amenity", ["hospital", "clinic", "doctors"]),
                                        ("healthcare", ["hospital", "clinic", "urgent_care"])],
    "Hardware / outdoor / sporting goods": [("shop", ["hardware", "doityourself", "outdoor", "sports",
                                                      "hunting", "fishing"])],
    "Auto repair / parts / tires": [("shop", ["car_repair", "car_parts", "tyres"])],
    "Laundry": [("shop", ["laundry", "dry_cleaning"])],
    "Bank / ATM": [("amenity", ["bank", "atm"])],
    "Post office": [("amenity", ["post_office"])],
    "Library": [("amenity", ["library"])],
    "Police / fire / ranger": [("amenity", ["police", "fire_station", "ranger_station"])],
    "Parking": [("amenity", ["parking"])],
    "Town / city / village": [("place", ["city", "town", "village", "hamlet"])],
    "Boat launch / marina": [("leisure", ["slipway", "marina"])],
    "Fishing spot": [("leisure", ["fishing"])],
    "Place of worship": [("amenity", ["place_of_worship"])],
    "Airport / airstrip": [("aeroway", ["aerodrome", "airstrip"])],
    "Street address": [("addr:housenumber", None)],
}


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Paths and small utilities
# --------------------------------------------------------------------------

def default_data_dir():
    env = os.environ.get("OFFGRIDFINDER_HOME")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


def resource_path(*parts):
    """Locate bundled files both when run from source and from a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "region"


def ro_uri(path):
    return Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def fmt_distance(m, unit):
    v = m / UNITS[unit]
    if unit in ("mi", "km"):
        return f"{v:.2f} {unit}" if v < 100 else f"{v:.1f} {unit}"
    return f"{v:,.0f} {unit}"


def category_label(cat):
    if not cat:
        return ""
    key, _, val = cat.partition("=")
    if key == "addr":
        return "Address"
    return val.replace("_", " ").capitalize()


COORD_PAIR_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)")
DMS_RE = re.compile(
    r"""(?P<deg>\d+(?:\.\d+)?)\s*(?:°|º|d|\s)?\s*
        (?:(?P<min>\d+(?:\.\d+)?)\s*(?:'|′|m|\s)?\s*)?
        (?:(?P<sec>\d+(?:\.\d+)?)\s*(?:"|″|s)?\s*)?
        (?P<hem>[NSEW])""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_coords(text):
    """Parse '34.577, -83.332', '34°34'37"N 83°19'55"W', or a map URL containing '@lat,lon'."""
    s = text.strip()
    if not s:
        raise ValueError("No coordinates entered.")
    dms = list(DMS_RE.finditer(s))
    if len(dms) == 2:
        vals = {}
        for m in dms:
            v = float(m.group("deg")) + float(m.group("min") or 0) / 60 + float(m.group("sec") or 0) / 3600
            hem = m.group("hem").upper()
            if hem in "SW":
                v = -v
            vals["lat" if hem in "NS" else "lon"] = v
        if "lat" in vals and "lon" in vals:
            return _check_latlon(vals["lat"], vals["lon"])
    m = COORD_PAIR_RE.search(s)
    if m:
        return _check_latlon(float(m.group(1)), float(m.group(2)))
    raise ValueError(f"Could not read coordinates from: {text!r}\n"
                     "Use decimal degrees like 34.5773, -83.3324")


def _check_latlon(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"Coordinates out of range: {lat}, {lon}")
    return lat, lon


def parse_distance(text, unit, label):
    s = text.strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"'{label}' must be a number (got {s!r}).")
    if v < 0:
        raise ValueError(f"'{label}' cannot be negative.")
    return v * UNITS[unit]


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    return COMPASS[int((deg + 11.25) // 22.5) % 16]


def expand_bbox(bb, meters):
    """bb = (minlat, maxlat, minlon, maxlon). Returns a bbox that contains every point within `meters`."""
    dlat = meters / M_PER_DEG_MIN
    edge = min(89.0, max(abs(bb[0]), abs(bb[1])) + dlat)
    dlon = meters / (M_PER_DEG * max(0.01, math.cos(math.radians(edge))))
    return (max(-90.0, bb[0] - dlat), min(90.0, bb[1] + dlat), bb[2] - dlon, bb[3] + dlon)


def union_bbox(boxes):
    boxes = list(boxes)
    return (min(b[0] for b in boxes), max(b[1] for b in boxes),
            min(b[2] for b in boxes), max(b[3] for b in boxes))


class Geom:
    """A point, or a set of polyline parts. Areas use even-odd point-in-polygon over all parts,
    which works on multipolygon member ways without assembling them into rings."""
    __slots__ = ("lat", "lon", "parts", "is_area", "bbox", "part_bboxes")

    def __init__(self, lat, lon, parts=None, is_area=False):
        self.lat, self.lon = lat, lon
        self.parts = [p for p in (parts or []) if p] or None
        self.is_area = bool(is_area and self.parts)
        if self.parts:
            self.part_bboxes = []
            for p in self.parts:
                lats = [c[0] for c in p]
                lons = [c[1] for c in p]
                self.part_bboxes.append((min(lats), max(lats), min(lons), max(lons)))
            self.bbox = union_bbox(self.part_bboxes)
        else:
            self.part_bboxes = None
            self.bbox = (lat, lat, lon, lon)

    def vertices(self, max_n):
        if not self.parts:
            return [(self.lat, self.lon)]
        pts = [c for p in self.parts for c in p]
        step = max(1, math.ceil(len(pts) / max_n))
        out = pts[::step]
        if out[-1] != pts[-1]:
            out.append(pts[-1])
        return out


def point_in_parts(lat, lon, parts):
    inside = False
    for part in parts:
        for i in range(1, len(part)):
            y1, x1 = part[i - 1]
            y2, x2 = part[i]
            if (y1 > lat) != (y2 > lat):
                if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside


def dist_point_geom(lat, lon, g):
    """Distance in meters from a point to geometry g, plus the closest point on g."""
    if not g.parts:
        return haversine(lat, lon, g.lat, g.lon), g.lat, g.lon
    if g.is_area:
        b = g.bbox
        if b[0] <= lat <= b[1] and b[2] <= lon <= b[3] and point_in_parts(lat, lon, g.parts):
            return 0.0, lat, lon
    # Work in a local plane scaled so x and y are both "degrees of latitude".
    coslat = max(1e-6, math.cos(math.radians(lat)))
    order = []
    for idx, (a0, a1, o0, o1) in enumerate(g.part_bboxes):
        gy = max(0.0, a0 - lat, lat - a1)
        gx = max(0.0, o0 - lon, lon - o1) * coslat
        order.append((gy * gy + gx * gx, idx))
    order.sort()
    best = math.inf
    bx = by = 0.0
    for gap, idx in order:
        if gap >= best:
            break
        part = g.parts[idx]
        y1 = part[0][0] - lat
        x1 = (part[0][1] - lon) * coslat
        d = x1 * x1 + y1 * y1
        if d < best:
            best, bx, by = d, x1, y1
        for plat, plon in part[1:]:
            y2 = plat - lat
            x2 = (plon - lon) * coslat
            dx, dy = x2 - x1, y2 - y1
            seg = dx * dx + dy * dy
            if seg > 0:
                t = -(x1 * dx + y1 * dy) / seg
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                cx, cy = x1 + t * dx, y1 + t * dy
            else:
                cx, cy = x1, y1
            d = cx * cx + cy * cy
            if d < best:
                best, bx, by = d, cx, cy
            x1, y1 = x2, y2
    clat, clon = lat + by, lon + bx / coslat
    # Final distance is great-circle, so the planar step only has to pick the right segment.
    return haversine(lat, lon, clat, clon), clat, clon


def dist_geom_geom(ag, tg, samples=80):
    """Approximate distance between two line/area geometries by sampling vertices both ways.
    Returns (meters, point_on_a, point_on_t)."""
    if ag.is_area:
        for lat, lon in tg.vertices(samples):
            if point_in_parts(lat, lon, ag.parts):
                return 0.0, (lat, lon), (lat, lon)
    if tg.is_area:
        for lat, lon in ag.vertices(samples):
            if point_in_parts(lat, lon, tg.parts):
                return 0.0, (lat, lon), (lat, lon)
    best = (math.inf, None, None)
    for lat, lon in tg.vertices(samples):
        d, clat, clon = dist_point_geom(lat, lon, ag)
        if d < best[0]:
            best = (d, (clat, clon), (lat, lon))
    for lat, lon in ag.vertices(samples):
        d, clat, clon = dist_point_geom(lat, lon, tg)
        if d < best[0]:
            best = (d, (lat, lon), (clat, clon))
    return best


def encode_geom(parts):
    total = sum(len(p) for p in parts)
    step = max(1, math.ceil(total / MAX_GEOM_POINTS))
    out = []
    for p in parts:
        q = p
        if step > 1 and len(p) > 2:
            q = p[::step]
            if q[-1] != p[-1]:
                q.append(p[-1])
        flat = []
        for lat, lon in q:
            flat.append(round(lat, 6))
            flat.append(round(lon, 6))
        out.append(flat)
    return json.dumps(out, separators=(",", ":"))


def decode_geom(text):
    return [[(p[i], p[i + 1]) for i in range(0, len(p), 2)] for p in json.loads(text)]


# --------------------------------------------------------------------------
# Classification of OSM objects
# --------------------------------------------------------------------------

def classify(tags, include_addresses=False):
    """Return the primary 'key=value' category for objects worth indexing, else None."""
    name = tags.get("name")
    for key in PRIMARY_KEYS:
        v = tags.get(key)
        if not v or v == "no":
            continue
        if key == "amenity":
            if v in AMENITY_NOISE and not name:
                return None
        elif key == "tourism":
            if v in TOURISM_NOISE and not name:
                continue
        elif key == "leisure":
            if not name and v not in LEISURE_UNNAMED_OK:
                continue
        elif key == "natural":
            if v in NATURAL_NEVER or (not name and v not in NATURAL_UNNAMED_OK):
                continue
        elif key == "waterway":
            if not (v == "waterfall" or (name and v in WATERWAY_LINES)):
                continue
        elif key == "highway":
            if v in HIGHWAY_POIS or (v in TRAIL_HIGHWAYS and name):
                return f"highway={v}"
            continue
        elif key == "route":
            if v in ROUTE_TYPES and (name or tags.get("ref")):
                return f"route={v}"
            continue
        elif key == "place":
            if not (name and v in PLACE_OK):
                continue
        elif key == "boundary":
            if not (name and v in ("national_park", "protected_area")):
                continue
        elif key == "landuse":
            if not (name and v in LANDUSE_OK):
                continue
        elif key == "railway":
            if not (name and v in RAILWAY_OK):
                continue
        elif key == "aeroway":
            if v not in AEROWAY_OK:
                continue
        elif key in NAMED_ONLY_KEYS:
            if not name:
                continue
        return f"{key}={v}"
    if include_addresses and tags.get("addr:housenumber") and tags.get("addr:street"):
        return "addr=address"
    return None


def geom_kind(category):
    """'line', 'area', or None when only a single point is stored."""
    key, _, val = category.partition("=")
    if key == "highway" and val in TRAIL_HIGHWAYS:
        return "line"
    if key == "route":
        return "line"
    if key == "waterway" and val in WATERWAY_LINES:
        return "line"
    if key == "natural" and val in NATURAL_LINES:
        return "line"
    if key == "natural" and val in NATURAL_AREAS:
        return "area"
    if key == "leisure" and val in LEISURE_AREAS:
        return "area"
    if key in ("boundary", "landuse"):
        return "area"
    if key == "tourism" and val in TOURISM_AREAS:
        return "area"
    if key == "place" and val in ("island", "islet"):
        return "area"
    return None


def clean_tags(taglist):
    out = {}
    for t in taglist:
        k = t.k
        if k.startswith(TAG_DROP_PREFIXES):
            continue
        v = t.v
        out[k] = v if len(v) <= 500 else v[:500]
    return out


def display_name(tags, category):
    if category == "addr=address":
        return f"{tags.get('addr:housenumber', '')} {tags.get('addr:street', '')}".strip()
    return (tags.get("name") or tags.get("brand") or tags.get("ref")
            or tags.get("operator") or category_label(category))


def build_search_text(tags, category):
    words = [tags[k] for k in SEARCH_TAGS if tags.get(k)]
    if category == "addr=address":
        words.append(tags.get("addr:housenumber", ""))
    for k in PRIMARY_KEYS:
        v = tags.get(k)
        if v and v not in ("yes", "no"):
            words.append(v.replace("_", " "))
    words.append(SYNONYMS.get(category, ""))
    return " ".join(w for w in words if w)


# --------------------------------------------------------------------------
# Import: .osm.pbf -> region SQLite database
# --------------------------------------------------------------------------

REGION_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE features(
    id INTEGER PRIMARY KEY,
    osm_type TEXT NOT NULL,
    osm_id INTEGER NOT NULL,
    name TEXT,
    category TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL,
    minlat REAL, maxlat REAL, minlon REAL, maxlon REAL,
    is_area INTEGER NOT NULL DEFAULT 0,
    geom TEXT,
    tags TEXT,
    search_text TEXT
);
CREATE TABLE feature_tags(fid INTEGER NOT NULL, key TEXT NOT NULL, value TEXT);
CREATE VIRTUAL TABLE features_rtree USING rtree(id, minlat, maxlat, minlon, maxlon);
"""
FTS_SCHEMA = ("CREATE VIRTUAL TABLE features_fts USING fts5(search_text, content='features', "
              "content_rowid='id', tokenize='unicode61 remove_diacritics 2')")


def sqlite_has_fts5():
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.Error:
        return False


class _FeatureWriter:
    BATCH = 5000

    def __init__(self, conn):
        self.conn = conn
        self.reset(clear_db=False)

    def reset(self, clear_db=True):
        if clear_db:
            for t in ("features", "feature_tags", "features_rtree"):
                self.conn.execute(f"DELETE FROM {t}")
        self.next_id = 1
        self.count = 0
        self.rows, self.rtree, self.tags = [], [], []

    def add(self, osm_type, osm_id, tags, category, lat, lon, parts=None, is_area=False):
        fid = self.next_id
        self.next_id += 1
        geom_json = None
        if parts:
            geom_json = encode_geom(parts)
            lats = [c[0] for p in parts for c in p]
            lons = [c[1] for p in parts for c in p]
            bbox = (min(lats), max(lats), min(lons), max(lons))
        else:
            bbox = (lat, lat, lon, lon)
        self.rows.append((
            fid, osm_type, osm_id, display_name(tags, category), category, lat, lon,
            bbox[0], bbox[1], bbox[2], bbox[3], 1 if (is_area and parts) else 0, geom_json,
            json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
            build_search_text(tags, category),
        ))
        self.rtree.append((fid,) + bbox)
        self.tags.extend((fid, k, v) for k, v in tags.items())
        self.count += 1
        if len(self.rows) >= self.BATCH:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        c = self.conn
        c.executemany("INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self.rows)
        c.executemany("INSERT INTO features_rtree VALUES (?,?,?,?,?)", self.rtree)
        c.executemany("INSERT INTO feature_tags VALUES (?,?,?)", self.tags)
        self.rows, self.rtree, self.tags = [], [], []


class _LocationsMissing(Exception):
    pass


def _mean(coords):
    pts = coords[:-1] if len(coords) > 2 and coords[0] == coords[-1] else coords
    return (sum(c[0] for c in pts) / len(pts), sum(c[1] for c in pts) / len(pts))


def _collect_relations(pbf_path, include_addresses, log, cancel):
    rels, way_rels = {}, defaultdict(list)
    n = 0
    for r in osmium.FileProcessor(pbf_path, osmium.osm.RELATION):
        n += 1
        if n % 20000 == 0:
            if cancel.is_set():
                raise Cancelled()
            log(f"Pass 1/2: scanned {n:,} relations, kept {len(rels):,}")
        if not len(r.tags):
            continue
        tags = clean_tags(r.tags)
        rtype = tags.get("type")
        if rtype == "route":
            cat = classify(tags, include_addresses)
            if not cat or not cat.startswith("route="):
                continue
            kind = "line"
        elif rtype in ("multipolygon", "boundary"):
            cat = classify(tags, include_addresses)
            if not cat or cat.startswith("route="):
                continue
            kind = "area" if geom_kind(cat) == "area" else None
        else:
            continue
        ways = [m.ref for m in r.members if m.type == "w"]
        if not ways:
            continue
        rels[r.id] = {"tags": tags, "cat": cat, "kind": kind, "parts": []}
        for wid in ways:
            way_rels[wid].append(r.id)
    log(f"Pass 1/2 done: {len(rels):,} relations (routes and areas) kept")
    return rels, way_rels


def _scan_nodes_ways(pbf_path, writer, rels, way_rels, include_addresses, log, cancel, use_filter):
    fp = osmium.FileProcessor(pbf_path, osmium.osm.NODE | osmium.osm.WAY).with_locations()
    if use_filter:
        # Drop untagged nodes in C++ instead of Python. The location cache still sees them.
        keys = list(PRIMARY_KEYS) + (["addr:housenumber"] if include_addresses else [])
        kf = osmium.filter.KeyFilter(*keys)
        kf.enable_for(osmium.osm.NODE)
        fp = fp.with_filter(kf)
    n = checked = missing = 0
    for obj in fp:
        n += 1
        if n % 250000 == 0:
            if cancel.is_set():
                raise Cancelled()
            log(f"Pass 2/2: read {n:,} objects, indexed {writer.count:,} features")
        if obj.is_node():
            if not len(obj.tags):
                continue
            tags = clean_tags(obj.tags)
            cat = classify(tags, include_addresses)
            if not cat:
                continue
            loc = obj.location
            if loc.valid():
                writer.add("n", obj.id, tags, cat, loc.lat, loc.lon)
            continue
        # ways
        rel_ids = way_rels.get(obj.id)
        tags = clean_tags(obj.tags) if len(obj.tags) else {}
        cat = classify(tags, include_addresses) if tags else None
        if not cat and not rel_ids:
            continue
        coords = [(nd.lat, nd.lon) for nd in obj.nodes if nd.location.valid()]
        checked += 1
        if len(coords) < len(obj.nodes):
            missing += 1
        if use_filter and checked == 2000 and missing > 1000:
            raise _LocationsMissing()
        if not coords:
            continue
        if rel_ids:
            for rid in rel_ids:
                rels[rid]["parts"].append(coords)
        if not cat:
            continue
        kind = geom_kind(cat)
        closed = len(coords) > 3 and coords[0] == coords[-1]
        if kind == "line":
            mid = coords[len(coords) // 2]
            writer.add("w", obj.id, tags, cat, mid[0], mid[1], [coords], False)
        elif kind == "area":
            lat, lon = _mean(coords)
            writer.add("w", obj.id, tags, cat, lat, lon, [coords], closed)
        else:
            lat, lon = _mean(coords)
            writer.add("w", obj.id, tags, cat, lat, lon)
    return n


def pbf_timestamp(path):
    try:
        ts = osmium.FileProcessor(path, osmium.osm.NOTHING).header.get("osmosis_replication_timestamp")
        return ts or None
    except Exception:
        return None


def import_pbf(pbf_path, out_path, meta, include_addresses, log, cancel):
    """Build a region database from an OSM file. Writes to a temp file and swaps it in atomically,
    so a failed or cancelled update leaves the old data untouched."""
    if not HAVE_OSMIUM:
        raise RuntimeError("pyosmium is not installed. Run: pip install osmium")
    tmp = out_path + ".tmp"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    started = time.time()
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(REGION_SCHEMA)
        fts = sqlite_has_fts5()
        if fts:
            conn.execute(FTS_SCHEMA)
        else:
            log("SQLite on this system lacks FTS5; text search will use slower LIKE matching.")

        rels, way_rels = _collect_relations(pbf_path, include_addresses, log, cancel)
        writer = _FeatureWriter(conn)
        try:
            n = _scan_nodes_ways(pbf_path, writer, rels, way_rels, include_addresses, log, cancel, True)
        except _LocationsMissing:
            log("Node filter interfered with way geometry on this pyosmium build; rescanning without it.")
            writer.reset()
            for r in rels.values():
                r["parts"] = []
            n = _scan_nodes_ways(pbf_path, writer, rels, way_rels, include_addresses, log, cancel, False)
        log(f"Pass 2/2 done: {n:,} objects read. Assembling {len(rels):,} relations...")

        for rid, r in rels.items():
            parts = r["parts"]
            if not parts:
                continue
            verts = [c for p in parts for c in p]
            if r["kind"] == "line":
                longest = max(parts, key=len)
                lat, lon = longest[len(longest) // 2]
                writer.add("r", rid, r["tags"], r["cat"], lat, lon, parts, False)
            elif r["kind"] == "area":
                lat, lon = _mean(verts)
                writer.add("r", rid, r["tags"], r["cat"], lat, lon, parts, True)
            else:
                lat, lon = _mean(verts)
                writer.add("r", rid, r["tags"], r["cat"], lat, lon)
        writer.flush()
        if cancel.is_set():
            raise Cancelled()

        log("Building indexes...")
        conn.execute("CREATE INDEX idx_tags_kv ON feature_tags(key, value)")
        conn.execute("CREATE INDEX idx_tags_fid ON feature_tags(fid)")
        conn.execute("CREATE INDEX idx_features_osm ON features(osm_type, osm_id)")
        if fts:
            conn.execute("INSERT INTO features_fts(features_fts) VALUES('rebuild')")
        meta = dict(meta)
        meta.update({
            "schema_version": SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "imported_at": now_iso(),
            "data_timestamp": pbf_timestamp(pbf_path) or meta.get("data_timestamp") or "",
            "feature_count": str(writer.count),
            "include_addresses": "1" if include_addresses else "0",
        })
        conn.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
        conn.commit()
        conn.execute("ANALYZE")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    except BaseException:
        conn.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    conn.close()
    os.replace(tmp, out_path)
    log(f"Import complete: {writer.count:,} features in {time.time() - started:.0f}s")
    return writer.count


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def http_open(url, method="GET", timeout=60):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def http_download(url, dest, progress, cancel):
    """Download url to dest. progress(fraction_or_None, text). Returns the Last-Modified header."""
    tmp = dest + ".part"
    with http_open(url) as r:
        total = int(r.headers.get("Content-Length") or 0)
        last_mod = r.headers.get("Last-Modified", "")
        done = 0
        t0 = time.time()
        with open(tmp, "wb") as f:
            while True:
                if cancel.is_set():
                    f.close()
                    os.remove(tmp)
                    raise Cancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                rate = done / max(0.1, time.time() - t0)
                text = f"Downloading {human_size(done)}"
                if total:
                    text += f" of {human_size(total)}"
                progress(done / total if total else None, f"{text} ({human_size(rate)}/s)")
    os.replace(tmp, dest)
    return last_mod


def remote_last_modified(url):
    with http_open(url, method="HEAD", timeout=30) as r:
        return r.headers.get("Last-Modified", "")


def is_newer(remote, local):
    if not remote:
        return None
    if not local:
        return True
    try:
        return parsedate_to_datetime(remote) > parsedate_to_datetime(local)
    except (TypeError, ValueError):
        return remote != local


# --------------------------------------------------------------------------
# Storage: app database (saved places, settings) and region files
# --------------------------------------------------------------------------

class Store:
    def __init__(self, root=None):
        self.root = root or default_data_dir()
        self.regions_dir = os.path.join(self.root, "regions")
        self.downloads_dir = os.path.join(self.root, "downloads")
        self.index_path = os.path.join(self.root, "geofabrik-index.json")
        self.app_db = os.path.join(self.root, "app.db")
        for d in (self.root, self.regions_dir, self.downloads_dir):
            os.makedirs(d, exist_ok=True)
        with self._db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS places(id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                      "lat REAL NOT NULL, lon REAL NOT NULL, note TEXT DEFAULT '')")
            c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

    @contextmanager
    def _db(self):
        c = sqlite3.connect(self.app_db)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def region_paths(self):
        return sorted(str(p) for p in Path(self.regions_dir).glob("*.db"))

    def region_path_for(self, region_id):
        return os.path.join(self.regions_dir, safe_name(region_id) + ".db")

    @staticmethod
    def region_meta(path):
        try:
            c = sqlite3.connect(ro_uri(path), uri=True)
            try:
                return dict(c.execute("SELECT key, value FROM meta"))
            finally:
                c.close()
        except sqlite3.Error:
            return {}

    def places(self):
        with self._db() as c:
            return c.execute("SELECT id, name, lat, lon, note FROM places ORDER BY name COLLATE NOCASE").fetchall()

    def add_place(self, name, lat, lon, note=""):
        with self._db() as c:
            c.execute("INSERT INTO places(name, lat, lon, note) VALUES (?,?,?,?)", (name, lat, lon, note))

    def delete_place(self, pid):
        with self._db() as c:
            c.execute("DELETE FROM places WHERE id=?", (pid,))

    def get_setting(self, key, default=None):
        with self._db() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))


# --------------------------------------------------------------------------
# Query engine
# --------------------------------------------------------------------------

class Feature:
    __slots__ = ("region", "fid", "osm_type", "osm_id", "name", "category", "lat", "lon",
                 "is_area", "_geom_json", "_tags_json", "_geom", "_tags")

    def __init__(self, region, row):
        (self.fid, self.osm_type, self.osm_id, self.name, self.category, self.lat, self.lon,
         is_area, self._geom_json, self._tags_json) = row
        self.region = region
        self.is_area = bool(is_area)
        self._geom = None
        self._tags = None

    @property
    def key(self):
        return (self.osm_type, self.osm_id)

    @property
    def geom(self):
        if self._geom is None:
            parts = decode_geom(self._geom_json) if self._geom_json else None
            self._geom = Geom(self.lat, self.lon, parts, self.is_area)
        return self._geom

    @property
    def tags(self):
        if self._tags is None:
            self._tags = json.loads(self._tags_json) if self._tags_json else {}
        return self._tags

    def where(self):
        t = self.tags
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        city = t.get("addr:city") or t.get("is_in:city") or ""
        return ", ".join(x for x in (street, city) if x)

    def osm_url(self):
        kind = {"n": "node", "w": "way", "r": "relation"}[self.osm_type]
        return f"https://www.openstreetmap.org/{kind}/{self.osm_id}"


class Anchor:
    """Something distances are measured from."""
    __slots__ = ("label", "geom", "key")

    def __init__(self, label, geom, key=None):
        self.label, self.geom, self.key = label, geom, key

    @classmethod
    def from_point(cls, label, lat, lon):
        return cls(label, Geom(lat, lon))

    @classmethod
    def from_feature(cls, f):
        return cls(f.name or category_label(f.category), f.geom, f.key)


class Result:
    __slots__ = ("feature", "dist", "anchor", "anchor_pt", "target_pt")

    def __init__(self, feature, dist, anchor, anchor_pt, target_pt):
        self.feature, self.dist, self.anchor = feature, dist, anchor
        self.anchor_pt, self.target_pt = anchor_pt, target_pt

    def direction(self):
        if self.dist < 5:
            return "here"
        return compass(initial_bearing(*self.anchor_pt, *self.target_pt))


def measure(anchor, f):
    """Returns (meters, anchor, point_on_anchor, point_on_target)."""
    ag, tg = anchor.geom, f.geom
    if not tg.parts:
        d, clat, clon = dist_point_geom(f.lat, f.lon, ag)
        return d, anchor, (clat, clon), (f.lat, f.lon)
    if not ag.parts:
        d, clat, clon = dist_point_geom(ag.lat, ag.lon, tg)
        return d, anchor, (ag.lat, ag.lon), (clat, clon)
    d, pa, pt = dist_geom_geom(ag, tg)
    return d, anchor, pa, pt


class AnchorIndex:
    """Uniform grid over anchor geometry, so each candidate is only measured against nearby anchors."""

    def __init__(self, anchors):
        self.anchors = anchors
        bb = union_bbox(a.geom.bbox for a in anchors)
        extent = max(bb[1] - bb[0], bb[3] - bb[2])
        self.cell = max(0.02, extent / 48.0)
        edge = min(89.0, max(abs(bb[0]), abs(bb[1])))
        # Smallest real-world size of a cell side: a conservative lower bound for ring pruning.
        self.cell_m = self.cell * M_PER_DEG_MIN * max(0.01, math.cos(math.radians(edge)))
        self.grid = defaultdict(list)
        for i, a in enumerate(anchors):
            for c in self._cells_for(a.geom):
                self.grid[c].append(i)
        ys = [c[0] for c in self.grid]
        xs = [c[1] for c in self.grid]
        self.bounds = (min(ys), max(ys), min(xs), max(xs))

    def _cell(self, lat, lon):
        return (math.floor(lat / self.cell), math.floor(lon / self.cell))

    def _cell_range(self, bb):
        y0, x0 = self._cell(bb[0], bb[2])
        y1, x1 = self._cell(bb[1], bb[3])
        return y0, y1, x0, x1

    def _cells_for(self, g):
        cells = set()
        if not g.parts:
            cells.add(self._cell(g.lat, g.lon))
            return cells
        if g.is_area:  # interior points are distance 0, so cover the whole box
            y0, y1, x0, x1 = self._cell_range(g.bbox)
            return {(y, x) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)}
        for part in g.parts:
            prev = None
            for lat, lon in part:
                if prev is None:
                    cells.add(self._cell(lat, lon))
                else:
                    y0, y1, x0, x1 = self._cell_range((min(lat, prev[0]), max(lat, prev[0]),
                                                       min(lon, prev[1]), max(lon, prev[1])))
                    for y in range(y0, y1 + 1):
                        for x in range(x0, x1 + 1):
                            cells.add((y, x))
                prev = (lat, lon)
        return cells

    def _measure_cells(self, cells, f, best, seen):
        for c in cells:
            for i in self.grid.get(c, ()):
                if i in seen:
                    continue
                seen.add(i)
                res = measure(self.anchors[i], f)
                if best is None or res[0] < best[0]:
                    best = res
        return best

    def best_within(self, f, radius):
        y0, y1, x0, x1 = self._cell_range(expand_bbox(f.geom.bbox, radius))
        by0, by1, bx0, bx1 = self.bounds
        y0, y1, x0, x1 = max(y0, by0), min(y1, by1), max(x0, bx0), min(x1, bx1)
        if y0 > y1 or x0 > x1:
            return None
        if (y1 - y0 + 1) * (x1 - x0 + 1) > len(self.grid):
            cells = [c for c in self.grid if y0 <= c[0] <= y1 and x0 <= c[1] <= x1]
        else:
            cells = ((y, x) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
        return self._measure_cells(cells, f, None, set())

    def nearest(self, f):
        y0, y1, x0, x1 = self._cell_range(f.geom.bbox)
        by0, by1, bx0, bx1 = self.bounds
        maxk = max(y0 - by0, by1 - y1, x0 - bx0, bx1 - x1, 0) + 1
        best, seen = None, set()
        for k in range(maxk + 1):
            if best is not None and best[0] <= max(0, k - 1) * self.cell_m:
                break
            if k == 0:
                ring = [(y, x) for y in range(max(y0, by0), min(y1, by1) + 1)
                        for x in range(max(x0, bx0), min(x1, bx1) + 1)]
            else:
                ring = []
                ya, yb, xa, xb = y0 - k, y1 + k, x0 - k, x1 + k
                for x in range(max(xa, bx0), min(xb, bx1) + 1):
                    if by0 <= ya <= by1:
                        ring.append((ya, x))
                    if by0 <= yb <= by1:
                        ring.append((yb, x))
                for y in range(max(ya + 1, by0), min(yb - 1, by1) + 1):
                    if bx0 <= xa <= bx1:
                        ring.append((y, xa))
                    if bx0 <= xb <= bx1:
                        ring.append((y, xb))
            best = self._measure_cells(ring, f, best, seen)
        return best


class TargetSpec:
    """What to find: free text, a category preset, and raw tag filters."""

    def __init__(self, text="", preset=PRESET_ANY, tagfilter=""):
        self.text = text.strip()
        self.preset = preset or PRESET_ANY
        self.tagfilter = tagfilter.strip()
        self.tag_clauses = parse_tag_filter(self.tagfilter)

    def is_empty(self):
        return not self.text and self.preset == PRESET_ANY and not self.tag_clauses

    def describe(self):
        bits = []
        if self.text:
            bits.append(f"'{self.text}'")
        if self.preset != PRESET_ANY:
            bits.append(self.preset)
        if self.tagfilter:
            bits.append(f"[{self.tagfilter}]")
        return " + ".join(bits) or "anything"


def parse_tag_filter(s):
    """Clauses separated by ';'. Forms: key=value, key=a|b, key=*, key!=value, key~text, !key, key."""
    clauses = []
    for raw in s.split(";"):
        c = raw.strip()
        if not c:
            continue
        if c.startswith("!") and "=" not in c:
            clauses.append(("absent", c[1:].strip(), None))
            continue
        m = re.match(r"^([^=!~]+?)\s*(!=|=|~)\s*(.+)$", c)
        if not m:
            if re.match(r"^[\w:.\-]+$", c):
                clauses.append(("present", c, None))
                continue
            raise ValueError(f"Can't understand tag filter clause: {c!r}")
        key, op, val = m.group(1).strip(), m.group(2), m.group(3).strip()
        if op == "=" and val == "*":
            clauses.append(("present", key, None))
            continue
        values = [v.strip() for v in val.split("|") if v.strip()]
        clauses.append(({"=": "in", "!=": "notin", "~": "like"}[op], key, values))
    return clauses


def fts_query(text):
    parts = []
    for tok in re.findall(r"\w+", text, re.UNICODE):
        if tok == "OR" and parts and parts[-1] != "OR":
            parts.append("OR")
        elif tok != "OR":
            parts.append('"' + tok.replace('"', '""') + '"*')
    while parts and parts[-1] == "OR":
        parts.pop()
    return " ".join(parts)


class RegionDB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(ro_uri(path), uri=True, check_same_thread=False)
        self.meta = dict(self.conn.execute("SELECT key, value FROM meta"))
        self.region = self.meta.get("name") or Path(path).stem
        try:
            self.conn.execute("SELECT rowid FROM features_fts WHERE features_fts MATCH 'a' LIMIT 0")
            self.has_fts = True
        except sqlite3.Error:
            self.has_fts = False

    def close(self):
        self.conn.close()

    def build_where(self, spec):
        clauses, params = [], []
        if spec.text:
            if self.has_fts:
                q = fts_query(spec.text)
                if q:
                    clauses.append("f.id IN (SELECT rowid FROM features_fts WHERE features_fts MATCH ?)")
                    params.append(q)
            else:
                for w in re.findall(r"\w+", spec.text):
                    clauses.append("f.search_text LIKE ?")
                    params.append(f"%{w}%")
        preset = PRESETS.get(spec.preset) or []
        if preset:
            ors = []
            for key, values in preset:
                if values:
                    ors.append(f"(key=? AND value IN ({','.join('?' * len(values))}))")
                    params.extend([key] + list(values))
                else:
                    ors.append("(key=?)")
                    params.append(key)
            clauses.append(f"f.id IN (SELECT fid FROM feature_tags WHERE {' OR '.join(ors)})")
        for op, key, values in spec.tag_clauses:
            if op in ("present", "absent"):
                sub, p = "SELECT fid FROM feature_tags WHERE key=?", [key]
            elif op in ("in", "notin"):
                sub = (f"SELECT fid FROM feature_tags WHERE key=? AND value COLLATE NOCASE "
                       f"IN ({','.join('?' * len(values))})")
                p = [key] + values
            else:  # like
                sub = (f"SELECT fid FROM feature_tags WHERE key=? AND "
                       f"({' OR '.join(['value LIKE ?'] * len(values))})")
                p = [key] + [f"%{v}%" for v in values]
            neg = op in ("absent", "notin")
            clauses.append(f"f.id {'NOT IN' if neg else 'IN'} ({sub})")
            params.extend(p)
        return (" AND ".join(clauses) or "1"), params

    COLS = "f.id, f.osm_type, f.osm_id, f.name, f.category, f.lat, f.lon, f.is_area, f.geom, f.tags"

    def fetch(self, where, params, bbox=None, limit=MAX_CANDIDATES, order=False, geom_only=False):
        sql = f"SELECT {self.COLS} FROM features f WHERE {where}"
        params = list(params)
        if bbox is not None:
            sql += (" AND f.id IN (SELECT id FROM features_rtree WHERE minlat<=? AND maxlat>=? "
                    "AND minlon<=? AND maxlon>=?)")
            params += [bbox[1], bbox[0], bbox[3], bbox[2]]
        if geom_only:
            sql += " AND f.geom IS NOT NULL"
        if order:
            sql += " ORDER BY f.name COLLATE NOCASE"
        sql += " LIMIT ?"
        params.append(limit)
        return [Feature(self.region, r) for r in self.conn.execute(sql, params)]


class SearchEngine:
    def __init__(self, store):
        self.store = store

    def _open(self):
        dbs = []
        for p in self.store.region_paths():
            try:
                dbs.append(RegionDB(p))
            except sqlite3.Error:
                pass
        return dbs

    def lookup(self, spec, limit):
        """Features matching spec across all regions (deduplicated), ordered by name."""
        dbs = self._open()
        try:
            out = {}
            for db in dbs:
                where, params = db.build_where(spec)
                for f in db.fetch(where, params, limit=limit, order=True):
                    out.setdefault(f.key, f)
            return sorted(out.values(), key=lambda f: (f.name or "").lower())[:limit]
        finally:
            for db in dbs:
                db.close()

    def search(self, spec, anchors, min_m, max_m, limit, cancel, progress):
        if not anchors:
            raise ValueError("No anchor to measure from.")
        dbs = self._open()
        if not dbs:
            raise ValueError("No regions are installed. Download one on the Regions tab.")
        notes = []
        try:
            cands = {}
            for db in dbs:
                progress(f"Querying {db.region}...")
                where, params = db.build_where(spec)
                if max_m is not None:
                    if len(anchors) <= 25:
                        boxes = [expand_bbox(a.geom.bbox, max_m) for a in anchors]
                    else:
                        boxes = [expand_bbox(union_bbox(a.geom.bbox for a in anchors), max_m)]
                else:
                    boxes = [None]
                for bb in boxes:
                    if cancel.is_set():
                        raise Cancelled()
                    rows = db.fetch(where, params, bb, MAX_CANDIDATES + 1)
                    if len(rows) > MAX_CANDIDATES:
                        notes.append(f"candidate list capped at {MAX_CANDIDATES:,} in {db.region}")
                        rows = rows[:MAX_CANDIDATES]
                    for f in rows:
                        cands.setdefault(f.key, f)
            for a in anchors:
                if a.key:
                    cands.pop(a.key, None)

            progress(f"Measuring {len(cands):,} candidates against {len(anchors):,} anchor(s)...")
            index = AnchorIndex(anchors) if len(anchors) > 1 else None
            results = []
            for i, f in enumerate(cands.values()):
                if i % 500 == 0:
                    if cancel.is_set():
                        raise Cancelled()
                    if i:
                        progress(f"Measured {i:,} of {len(cands):,}...")
                if index is None:
                    best = measure(anchors[0], f)
                elif max_m is not None:
                    best = index.best_within(f, max_m)
                else:
                    best = index.nearest(f)
                if best is None:
                    continue
                d, a, pa, pt = best
                if max_m is not None and d > max_m:
                    continue
                if min_m and d < min_m:
                    continue
                results.append(Result(f, d, a, pa, pt))
            results.sort(key=lambda r: r.dist)
            total = len(results)
            if total > limit:
                notes.append(f"showing nearest {limit:,} of {total:,}")
                results = results[:limit]
            return results, notes
        finally:
            for db in dbs:
                db.close()


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_csv(path, results, unit):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "name", "category", f"distance_{unit}", "direction", "measured_from",
                    "lat", "lon", "closest_lat", "closest_lon", "address", "phone", "website",
                    "opening_hours", "region", "osm_url"])
        for i, r in enumerate(results, 1):
            f, t = r.feature, r.feature.tags
            w.writerow([i, f.name, category_label(f.category), f"{r.dist / UNITS[unit]:.3f}",
                        r.direction(), r.anchor.label, f"{f.lat:.6f}", f"{f.lon:.6f}",
                        f"{r.target_pt[0]:.6f}", f"{r.target_pt[1]:.6f}", f.where(),
                        t.get("phone") or t.get("contact:phone", ""),
                        t.get("website") or t.get("contact:website", ""),
                        t.get("opening_hours", ""), f.region, f.osm_url()])


def export_gpx(path, results, unit):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<gpx version="1.1" creator="{APP_NAME} {APP_VERSION}" xmlns="http://www.topografix.com/GPX/1/1">']
    for r in results:
        f = r.feature
        lat, lon = r.target_pt if f.geom.parts else (f.lat, f.lon)
        desc = f"{category_label(f.category)}, {fmt_distance(r.dist, unit)} {r.direction()} of {r.anchor.label}"
        lines.append(f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}"><name>{xml_escape(f.name or "")}</name>'
                     f'<desc>{xml_escape(desc)}</desc><type>{xml_escape(f.category or "")}</type></wpt>')
    lines.append("</gpx>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(store):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    if sys.platform == "win32":
        try:  # group the taskbar button under our own icon instead of python.exe
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NullAngst.OffGridFinder")
        except Exception:
            pass

    class App(tk.Tk):
        def __init__(self):
            super().__init__(className=APP_NAME)  # WM_CLASS, matches the .desktop file
            self._set_icon()
            self.store = store
            self.engine = SearchEngine(store)
            self.title(f"{APP_NAME} {APP_VERSION}")
            self.geometry("1320x840")
            self.minsize(980, 620)
            style = ttk.Style(self)
            if "clam" in style.theme_names() and sys.platform.startswith("linux"):
                style.theme_use("clam")
            self._queue = queue.Queue()
            self.after(50, self._poll)

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True)
            self.search = SearchPanel(self.nb, self)
            self.plot = PlotPanel(self.nb, self)
            self.regions = RegionsPanel(self.nb, self)
            self.places = PlacesPanel(self.nb, self)
            self.nb.add(self.search, text="Search")
            self.nb.add(self.plot, text="Plot")
            self.nb.add(self.places, text="Saved places")
            self.nb.add(self.regions, text="Regions")

            self.status_var = tk.StringVar()
            ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8, 3)).pack(fill="x")
            n = len(store.region_paths())
            self.set_status(f"{n} region(s) installed. Data folder: {store.root}" if n else
                            "No regions installed yet. Open the Regions tab to download one.")
            if not HAVE_OSMIUM:
                self.set_status("pyosmium not installed: searching works, importing does not "
                                "(pip install osmium).")

        def _set_icon(self):
            try:
                self._icon = tk.PhotoImage(file=resource_path("assets", "icon-256.png"))
                self.iconphoto(True, self._icon)
            except tk.TclError:
                pass
            if sys.platform == "win32":
                try:
                    self.iconbitmap(default=resource_path("assets", "icon.ico"))
                except tk.TclError:
                    pass

        def ui(self, fn, *args):
            """Schedule fn(*args) on the Tk thread. Safe to call from workers."""
            self._queue.put((fn, args))

        def _poll(self):
            try:
                while True:
                    fn, args = self._queue.get_nowait()
                    try:
                        fn(*args)
                    except Exception:
                        traceback.print_exc()
            except queue.Empty:
                pass
            self.after(50, self._poll)

        def set_status(self, text):
            self.status_var.set(text)

        def regions_changed(self):
            self.regions.refresh_installed()
            self.plot.invalidate_dbs()

        def places_changed(self):
            self.places.refresh()
            self.search.refresh_places()

    # ---------------------------------------------------------------- Search
    class SearchPanel(ttk.Frame):
        COLS = (("rank", "#", 44), ("name", "Name", 230), ("category", "Category", 140),
                ("distance", "Distance", 90), ("dir", "Dir", 48), ("from", "From", 150),
                ("where", "Address", 190), ("region", "Region", 110))

        def __init__(self, master, app):
            super().__init__(master)
            self.app = app
            self.results = []
            self.sort_state = ("distance", False)
            self.chosen_anchors = []
            self.cancel_event = None
            self.busy = False
            self.last_query = None
            paned = ttk.Panedwindow(self, orient="horizontal")
            paned.pack(fill="both", expand=True)
            left = ttk.Frame(paned, padding=8)
            right = ttk.Frame(paned, padding=(4, 8, 8, 8))
            paned.add(left, weight=0)
            paned.add(right, weight=1)
            self._build_controls(left)
            self._build_results(right)
            self.refresh_places()

        # -- controls
        def _build_controls(self, f):
            what = ttk.LabelFrame(f, text="1. What to find", padding=6)
            what.pack(fill="x")
            ttk.Label(what, text="Name or keyword").grid(row=0, column=0, sticky="w")
            self.text_var = tk.StringVar()
            e = ttk.Entry(what, textvariable=self.text_var, width=36)
            e.grid(row=1, column=0, sticky="ew")
            e.bind("<Return>", lambda _e: self.start_search())
            ttk.Label(what, text="Category").grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.preset_var = tk.StringVar(value=PRESET_ANY)
            ttk.Combobox(what, textvariable=self.preset_var, values=list(PRESETS), state="readonly",
                         height=24).grid(row=3, column=0, sticky="ew")
            ttk.Label(what, text="Tag filter (optional)").grid(row=4, column=0, sticky="w", pady=(6, 0))
            self.tag_var = tk.StringVar()
            e = ttk.Entry(what, textvariable=self.tag_var)
            e.grid(row=5, column=0, sticky="ew")
            e.bind("<Return>", lambda _e: self.start_search())
            ttk.Label(what, text="e.g. brand=Dollar General; opening_hours=*; fee!=yes",
                      foreground="gray").grid(row=6, column=0, sticky="w")
            what.columnconfigure(0, weight=1)

            dist = ttk.LabelFrame(f, text="2. Distance", padding=6)
            dist.pack(fill="x", pady=(8, 0))
            ttk.Label(dist, text="At least").grid(row=0, column=0, sticky="w")
            self.min_var = tk.StringVar()
            ttk.Entry(dist, textvariable=self.min_var, width=7).grid(row=0, column=1, padx=4)
            ttk.Label(dist, text="at most").grid(row=0, column=2)
            self.max_var = tk.StringVar(value="10")
            ttk.Entry(dist, textvariable=self.max_var, width=7).grid(row=0, column=3, padx=4)
            self.unit_var = tk.StringVar(value=self.app.store.get_setting("unit", "mi"))
            ttk.Combobox(dist, textvariable=self.unit_var, values=list(UNITS), width=4,
                         state="readonly").grid(row=0, column=4)
            ttk.Label(dist, text="Leave 'at most' empty to list every match, nearest first.",
                      foreground="gray", wraplength=300).grid(row=1, column=0, columnspan=5, sticky="w")

            frm = ttk.LabelFrame(f, text="3. Measured from", padding=6)
            frm.pack(fill="x", pady=(8, 0))
            self.anchor_mode = tk.StringVar(value="coords")
            ttk.Radiobutton(frm, text="Coordinates", variable=self.anchor_mode,
                            value="coords").grid(row=0, column=0, sticky="w")
            self.coords_var = tk.StringVar(value=self.app.store.get_setting("last_coords", ""))
            ce = ttk.Entry(frm, textvariable=self.coords_var)
            ce.grid(row=1, column=0, sticky="ew", padx=(20, 0))
            ce.bind("<FocusIn>", lambda _e: self.anchor_mode.set("coords"))
            ttk.Label(frm, text="34.5773, -83.3324  or  34°34'38\"N 83°19'57\"W",
                      foreground="gray").grid(row=2, column=0, sticky="w", padx=(20, 0))
            ttk.Radiobutton(frm, text="Saved place", variable=self.anchor_mode,
                            value="place").grid(row=3, column=0, sticky="w", pady=(6, 0))
            self.place_var = tk.StringVar()
            self.place_combo = ttk.Combobox(frm, textvariable=self.place_var, state="readonly")
            self.place_combo.grid(row=4, column=0, sticky="ew", padx=(20, 0))
            self.place_combo.bind("<<ComboboxSelected>>", lambda _e: self.anchor_mode.set("place"))
            ttk.Radiobutton(frm, text="Chosen feature(s)", variable=self.anchor_mode,
                            value="chosen").grid(row=5, column=0, sticky="w", pady=(6, 0))
            row = ttk.Frame(frm)
            row.grid(row=6, column=0, sticky="ew", padx=(20, 0))
            ttk.Button(row, text="Pick features...", command=self.open_picker).pack(side="left")
            self.chosen_var = tk.StringVar(value="nothing chosen")
            ttk.Label(frm, textvariable=self.chosen_var, foreground="#555", wraplength=280).grid(
                row=7, column=0, sticky="w", padx=(20, 0))
            frm.columnconfigure(0, weight=1)

            go = ttk.Frame(f, padding=(0, 8, 0, 0))
            go.pack(fill="x")
            ttk.Label(go, text="Max results").pack(side="left")
            self.limit_var = tk.StringVar(value="500")
            ttk.Spinbox(go, from_=10, to=100000, increment=100, textvariable=self.limit_var,
                        width=7).pack(side="left", padx=4)
            self.stop_btn = ttk.Button(go, text="Stop", command=self.stop, state="disabled")
            self.stop_btn.pack(side="right")
            self.go_btn = ttk.Button(go, text="Search", command=self.start_search)
            self.go_btn.pack(side="right", padx=4)

        # -- results
        def _build_results(self, f):
            top = ttk.Frame(f)
            top.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(top, columns=[c[0] for c in self.COLS], show="headings",
                                     selectmode="browse")
            for key, title, width in self.COLS:
                self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
                self.tree.column(key, width=width, anchor="e" if key in ("rank", "distance") else "w",
                                 stretch=key in ("name", "where"))
            ys = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=ys.set)
            self.tree.pack(side="left", fill="both", expand=True)
            ys.pack(side="right", fill="y")
            self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select())

            bar = ttk.Frame(f, padding=(0, 4))
            bar.pack(fill="x")
            self.count_var = tk.StringVar(value="No search yet.")
            ttk.Label(bar, textvariable=self.count_var).pack(side="left")
            ttk.Button(bar, text="Export GPX", command=lambda: self.export("gpx")).pack(side="right")
            ttk.Button(bar, text="Export CSV", command=lambda: self.export("csv")).pack(side="right", padx=4)
            ttk.Button(bar, text="Use all results as anchor",
                       command=self.results_as_anchor).pack(side="right")

            det = ttk.LabelFrame(f, text="Details", padding=4)
            det.pack(fill="x")
            self.details = tk.Text(det, height=11, wrap="word", state="disabled")
            self.details.pack(fill="x")
            btns = ttk.Frame(det)
            btns.pack(fill="x", pady=(4, 0))
            ttk.Button(btns, text="Copy coordinates", command=self.copy_coords).pack(side="left")
            ttk.Button(btns, text="Use as anchor", command=self.selected_as_anchor).pack(side="left", padx=4)
            ttk.Button(btns, text="Save as place", command=self.save_selected_place).pack(side="left")
            ttk.Button(btns, text="Show on plot", command=self.show_on_plot).pack(side="left", padx=4)
            ttk.Button(btns, text="Open on openstreetmap.org (online)",
                       command=self.open_osm).pack(side="right")

        # -- anchors
        def refresh_places(self):
            self._places = {p[1]: p for p in self.app.store.places()}
            self.place_combo["values"] = list(self._places)

        def set_chosen(self, anchors, label):
            self.chosen_anchors = anchors
            self.chosen_var.set(label)
            self.anchor_mode.set("chosen")

        def open_picker(self):
            FeaturePicker(self.app, self.set_chosen)

        def resolve_anchors(self):
            mode = self.anchor_mode.get()
            if mode == "coords":
                lat, lon = parse_coords(self.coords_var.get())
                self.app.store.set_setting("last_coords", self.coords_var.get().strip())
                return [Anchor.from_point(f"{lat:.5f}, {lon:.5f}", lat, lon)]
            if mode == "place":
                p = self._places.get(self.place_var.get())
                if not p:
                    raise ValueError("Choose a saved place (add them on the Saved places tab).")
                return [Anchor.from_point(p[1], p[2], p[3])]
            if not self.chosen_anchors:
                raise ValueError("No features chosen. Click 'Pick features...' or use a result as the anchor.")
            return self.chosen_anchors

        # -- search
        def start_search(self):
            if self.busy:
                return
            unit = self.unit_var.get()
            try:
                spec = TargetSpec(self.text_var.get(), self.preset_var.get(), self.tag_var.get())
                min_m = parse_distance(self.min_var.get(), unit, "At least")
                max_m = parse_distance(self.max_var.get(), unit, "At most")
                if min_m is not None and max_m is not None and min_m > max_m:
                    raise ValueError("'At least' is larger than 'at most'.")
                if spec.is_empty() and max_m is None:
                    raise ValueError("Give a keyword, category or tag filter, or set a maximum distance. "
                                     "Otherwise this would list every feature in every region.")
                limit = max(1, int(self.limit_var.get()))
                anchors = self.resolve_anchors()
            except ValueError as e:
                messagebox.showerror("Search", str(e), parent=self)
                return
            self.app.store.set_setting("unit", unit)
            self.busy = True
            self.go_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.cancel_event = threading.Event()
            cancel = self.cancel_event
            self.last_query = (spec, anchors, min_m, max_m)
            self.app.set_status("Searching...")
            t0 = time.time()

            def work():
                try:
                    res, notes = self.app.engine.search(
                        spec, anchors, min_m, max_m, limit, cancel,
                        lambda msg: self.app.ui(self.app.set_status, msg))
                except Cancelled:
                    self.app.ui(self._done, None, ["cancelled"], 0)
                except Exception as e:
                    traceback.print_exc()
                    self.app.ui(self._failed, str(e))
                else:
                    self.app.ui(self._done, res, notes, time.time() - t0)

            threading.Thread(target=work, daemon=True).start()

        def stop(self):
            if self.cancel_event:
                self.cancel_event.set()

        def _finish(self):
            self.busy = False
            self.go_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

        def _failed(self, msg):
            self._finish()
            self.app.set_status("Search failed.")
            messagebox.showerror("Search failed", msg, parent=self)

        def _done(self, results, notes, secs):
            self._finish()
            if results is None:
                self.app.set_status("Search cancelled.")
                return
            self.results = results
            self.sort_state = ("distance", False)
            self.populate()
            spec, anchors, _mn, max_m = self.last_query
            where = anchors[0].label if len(anchors) == 1 else f"{len(anchors):,} anchors"
            msg = f"{len(results):,} result(s) for {spec.describe()} from {where}"
            if notes:
                msg += " (" + "; ".join(notes) + ")"
            self.count_var.set(msg)
            self.app.set_status(f"Search finished in {secs:.1f}s.")
            self.app.plot.set_scene(anchors, results, max_m, self.unit_var.get())
            if results:
                first = self.tree.get_children()[0]
                self.tree.selection_set(first)
                self.tree.see(first)

        def populate(self):
            self.tree.delete(*self.tree.get_children())
            unit = self.unit_var.get()
            for i, r in enumerate(self.results):
                f = r.feature
                self.tree.insert("", "end", iid=str(i), values=(
                    i + 1, f.name, category_label(f.category), fmt_distance(r.dist, unit),
                    r.direction(), r.anchor.label, f.where(), f.region))

        def sort_by(self, col):
            if not self.results:
                return
            prev, rev = self.sort_state
            rev = (not rev) if prev == col else False
            keys = {
                "rank": lambda r: r.dist, "distance": lambda r: r.dist,
                "name": lambda r: (r.feature.name or "").lower(),
                "category": lambda r: category_label(r.feature.category),
                "dir": lambda r: r.direction(), "from": lambda r: r.anchor.label.lower(),
                "where": lambda r: r.feature.where().lower(), "region": lambda r: r.feature.region,
            }
            self.results.sort(key=keys[col], reverse=rev)
            self.sort_state = (col, rev)
            self.populate()
            self.app.plot.set_results(self.results)

        def selected(self):
            sel = self.tree.selection()
            return self.results[int(sel[0])] if sel else None

        def select_index(self, i):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.tree.see(iid)

        def on_select(self):
            r = self.selected()
            self.details.configure(state="normal")
            self.details.delete("1.0", "end")
            if r:
                f, unit = r.feature, self.unit_var.get()
                lines = [
                    f"{f.name}    [{category_label(f.category)}  {f.category}]",
                    f"{fmt_distance(r.dist, unit)} {r.direction()} of {r.anchor.label}",
                    f"Point: {f.lat:.6f}, {f.lon:.6f}",
                ]
                if r.anchor.geom.parts:
                    lines.append(f"Closest point on anchor: {r.anchor_pt[0]:.6f}, {r.anchor_pt[1]:.6f}")
                if f.geom.parts:
                    lines.append(f"Closest point on this feature: {r.target_pt[0]:.6f}, {r.target_pt[1]:.6f}")
                lines.append(f"Region: {f.region}    OSM: {f.osm_url()}")
                lines.append("")
                lines.extend(f"{k} = {v}" for k, v in sorted(f.tags.items()))
                self.details.insert("1.0", "\n".join(lines))
            self.details.configure(state="disabled")
            if r:
                self.app.plot.set_selected(self.results.index(r))

        # -- actions
        def _need(self):
            r = self.selected()
            if not r:
                messagebox.showinfo(APP_NAME, "Select a result first.", parent=self)
            return r

        def copy_coords(self):
            r = self._need()
            if r:
                lat, lon = r.target_pt if r.feature.geom.parts else (r.feature.lat, r.feature.lon)
                self.clipboard_clear()
                self.clipboard_append(f"{lat:.6f}, {lon:.6f}")
                self.app.set_status(f"Copied {lat:.6f}, {lon:.6f}")

        def selected_as_anchor(self):
            r = self._need()
            if r:
                self.set_chosen([Anchor.from_feature(r.feature)], r.feature.name)

        def results_as_anchor(self):
            if not self.results:
                return
            spec = self.last_query[0]
            self.set_chosen([Anchor.from_feature(r.feature) for r in self.results],
                            f"{len(self.results):,} results of the previous search ({spec.describe()})")
            self.app.set_status("Anchor set to previous results. Change 'What to find' and search again.")

        def save_selected_place(self):
            r = self._need()
            if r:
                self.app.places.prefill(r.feature.name, *(r.target_pt if r.feature.geom.parts
                                                           else (r.feature.lat, r.feature.lon)))
                self.app.nb.select(self.app.places)

        def show_on_plot(self):
            self.app.nb.select(self.app.plot)

        def open_osm(self):
            r = self._need()
            if r:
                webbrowser.open(r.feature.osm_url())

        def export(self, kind):
            if not self.results:
                messagebox.showinfo(APP_NAME, "Nothing to export.", parent=self)
                return
            path = filedialog.asksaveasfilename(
                parent=self, defaultextension=f".{kind}", filetypes=[(kind.upper(), f"*.{kind}")],
                initialfile=f"offgrid-results.{kind}")
            if not path:
                return
            (export_csv if kind == "csv" else export_gpx)(path, self.results, self.unit_var.get())
            self.app.set_status(f"Exported {len(self.results):,} results to {path}")

    # ---------------------------------------------------------------- Picker
    class FeaturePicker(tk.Toplevel):
        def __init__(self, app, on_choose):
            super().__init__(app)
            self.app, self.on_choose = app, on_choose
            self.title("Pick features to measure from")
            self.geometry("820x560")
            self.transient(app)
            self.features = []
            top = ttk.Frame(self, padding=8)
            top.pack(fill="x")
            ttk.Label(top, text="Keyword").grid(row=0, column=0, sticky="w")
            self.text_var = tk.StringVar()
            e = ttk.Entry(top, textvariable=self.text_var, width=30)
            e.grid(row=1, column=0, sticky="ew", padx=(0, 6))
            e.bind("<Return>", lambda _e: self.search())
            e.focus_set()
            ttk.Label(top, text="Category").grid(row=0, column=1, sticky="w")
            self.preset_var = tk.StringVar(value=PRESET_ANY)
            ttk.Combobox(top, textvariable=self.preset_var, values=list(PRESETS), state="readonly",
                         width=30, height=24).grid(row=1, column=1, sticky="ew", padx=(0, 6))
            ttk.Label(top, text="Tag filter").grid(row=0, column=2, sticky="w")
            self.tag_var = tk.StringVar()
            ttk.Entry(top, textvariable=self.tag_var, width=24).grid(row=1, column=2, sticky="ew", padx=(0, 6))
            ttk.Button(top, text="Search", command=self.search).grid(row=1, column=3)
            top.columnconfigure(0, weight=1)

            mid = ttk.Frame(self, padding=(8, 0))
            mid.pack(fill="both", expand=True)
            cols = (("name", "Name", 260), ("cat", "Category", 140), ("where", "Location", 250),
                    ("region", "Region", 110))
            self.tree = ttk.Treeview(mid, columns=[c[0] for c in cols], show="headings")
            for k, t, w in cols:
                self.tree.heading(k, text=t)
                self.tree.column(k, width=w, stretch=k in ("name", "where"))
            ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=ys.set)
            self.tree.pack(side="left", fill="both", expand=True)
            ys.pack(side="right", fill="y")
            self.tree.bind("<Double-1>", lambda _e: self.use_selected())

            bot = ttk.Frame(self, padding=8)
            bot.pack(fill="x")
            self.status = tk.StringVar(value="Search for a trail, park, town, store... "
                                             "Select one or more rows (Ctrl/Shift-click).")
            ttk.Label(bot, textvariable=self.status).pack(side="left")
            ttk.Button(bot, text="Close", command=self.destroy).pack(side="right")
            ttk.Button(bot, text="Use ALL matches", command=self.use_all).pack(side="right", padx=4)
            ttk.Button(bot, text="Use selected", command=self.use_selected).pack(side="right")

        def _spec(self):
            spec = TargetSpec(self.text_var.get(), self.preset_var.get(), self.tag_var.get())
            if spec.is_empty():
                raise ValueError("Enter a keyword, category or tag filter.")
            return spec

        def search(self):
            try:
                spec = self._spec()
            except ValueError as e:
                messagebox.showerror("Pick features", str(e), parent=self)
                return
            self.status.set("Searching...")

            def work():
                try:
                    feats = self.app.engine.lookup(spec, PICKER_DISPLAY_LIMIT + 1)
                except Exception as e:
                    self.app.ui(self.status.set, f"Error: {e}")
                    return
                self.app.ui(self._show, feats)

            threading.Thread(target=work, daemon=True).start()

        def _show(self, feats):
            if not self.winfo_exists():
                return
            more = len(feats) > PICKER_DISPLAY_LIMIT
            self.features = feats[:PICKER_DISPLAY_LIMIT]
            self.tree.delete(*self.tree.get_children())
            for i, f in enumerate(self.features):
                loc = f.where()
                loc = f"{loc}  ({f.lat:.4f}, {f.lon:.4f})" if loc else f"{f.lat:.4f}, {f.lon:.4f}"
                self.tree.insert("", "end", iid=str(i),
                                 values=(f.name, category_label(f.category), loc, f.region))
            self.status.set(f"{len(self.features):,} match(es)" +
                            (f" shown; more exist. 'Use ALL matches' takes up to {MAX_ANCHORS:,}." if more else "."))

        def use_selected(self):
            sel = [self.features[int(i)] for i in self.tree.selection()]
            if not sel:
                messagebox.showinfo("Pick features", "Select at least one row.", parent=self)
                return
            label = sel[0].name if len(sel) == 1 else f"{len(sel)} selected features ({sel[0].name}, ...)"
            self.on_choose([Anchor.from_feature(f) for f in sel], label)
            self.destroy()

        def use_all(self):
            try:
                spec = self._spec()
            except ValueError as e:
                messagebox.showerror("Pick features", str(e), parent=self)
                return
            self.status.set("Loading all matches...")

            def work():
                feats = self.app.engine.lookup(spec, MAX_ANCHORS)
                self.app.ui(self._use_all_done, feats, spec)

            threading.Thread(target=work, daemon=True).start()

        def _use_all_done(self, feats, spec):
            if not feats:
                self.status.set("No matches.")
                return
            self.on_choose([Anchor.from_feature(f) for f in feats],
                           f"any of {len(feats):,} features matching {spec.describe()}")
            self.destroy()

    # ---------------------------------------------------------------- Plot
    class PlotPanel(ttk.Frame):
        """Schematic offline view: anchors, radius, results, and optional trails/water/parks context."""

        def __init__(self, master, app):
            super().__init__(master)
            self.app = app
            self.anchors, self.results, self.max_m, self.unit = [], [], None, "mi"
            self.selected = None
            self.center = (0.0, 0.0)
            self.mpp = 100.0  # meters per pixel
            self._dbs = None
            self._drag = None
            self._redraw_pending = False
            bar = ttk.Frame(self, padding=4)
            bar.pack(fill="x")
            ttk.Button(bar, text="Fit results", command=self.fit).pack(side="left")
            self.ctx_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text="Draw trails, water and parks for context",
                            variable=self.ctx_var, command=self.redraw).pack(side="left", padx=8)
            ttk.Label(bar, text="Drag to pan, wheel to zoom, click a dot to select it. "
                                "This is a schematic, not a street map.",
                      foreground="gray").pack(side="left")
            self.canvas = tk.Canvas(self, background="#fbfaf6", highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            c = self.canvas
            c.bind("<Configure>", lambda _e: self.redraw())
            c.bind("<ButtonPress-1>", self._press)
            c.bind("<B1-Motion>", self._motion)
            c.bind("<ButtonRelease-1>", self._release)
            c.bind("<MouseWheel>", lambda e: self._zoom(e, 1 if e.delta > 0 else -1))
            c.bind("<Button-4>", lambda e: self._zoom(e, 1))
            c.bind("<Button-5>", lambda e: self._zoom(e, -1))

        def invalidate_dbs(self):
            if self._dbs:
                for db in self._dbs:
                    db.close()
            self._dbs = None

        def set_scene(self, anchors, results, max_m, unit):
            self.anchors, self.results, self.max_m, self.unit = anchors, results, max_m, unit
            self.selected = 0 if results else None
            self.fit()

        def set_results(self, results):
            self.results = results
            self.redraw()

        def set_selected(self, i):
            self.selected = i
            self.redraw()

        def fit(self):
            boxes = [a.geom.bbox for a in self.anchors[:5000]]
            boxes += [(r.target_pt[0], r.target_pt[0], r.target_pt[1], r.target_pt[1]) for r in self.results]
            if not boxes:
                self.redraw()
                return
            bb = union_bbox(boxes)
            if self.max_m and len(self.anchors) == 1 and not self.anchors[0].geom.parts:
                bb = union_bbox([bb, expand_bbox(self.anchors[0].geom.bbox, self.max_m)])
            self.center = ((bb[0] + bb[1]) / 2, (bb[2] + bb[3]) / 2)
            w = max(200, self.canvas.winfo_width())
            h = max(200, self.canvas.winfo_height())
            span_y = (bb[1] - bb[0]) * M_PER_DEG
            span_x = (bb[3] - bb[2]) * M_PER_DEG * math.cos(math.radians(self.center[0]))
            self.mpp = max(0.5, span_x / (w - 60), span_y / (h - 60), 2.0)
            self.redraw()

        def project(self, lat, lon):
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            x = w / 2 + (lon - self.center[1]) * M_PER_DEG * math.cos(math.radians(self.center[0])) / self.mpp
            y = h / 2 - (lat - self.center[0]) * M_PER_DEG / self.mpp
            return x, y

        def unproject(self, x, y):
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            lat = self.center[0] - (y - h / 2) * self.mpp / M_PER_DEG
            lon = self.center[1] + (x - w / 2) * self.mpp / (M_PER_DEG * math.cos(math.radians(self.center[0])))
            return lat, lon

        def _press(self, e):
            self._drag = (e.x, e.y, self.center, False)

        def _motion(self, e):
            if not self._drag:
                return
            x0, y0, c0, _ = self._drag
            if abs(e.x - x0) + abs(e.y - y0) > 3:
                self._drag = (x0, y0, c0, True)
                self.center = c0
                lat0, lon0 = self.unproject(x0, y0)
                lat1, lon1 = self.unproject(e.x, e.y)
                self.center = (c0[0] + (lat0 - lat1), c0[1] + (lon0 - lon1))
                self.schedule_redraw()

        def _release(self, e):
            if self._drag and not self._drag[3]:
                hits = self.canvas.find_overlapping(e.x - 6, e.y - 6, e.x + 6, e.y + 6)
                for item in reversed(hits):
                    for tag in self.canvas.gettags(item):
                        if tag.startswith("res:"):
                            self.app.search.select_index(int(tag[4:]))
                            return
            self._drag = None

        def _zoom(self, e, direction):
            lat, lon = self.unproject(e.x, e.y)
            self.mpp = min(200000.0, max(0.3, self.mpp * (0.8 if direction > 0 else 1.25)))
            lat2, lon2 = self.unproject(e.x, e.y)
            self.center = (self.center[0] + lat - lat2, self.center[1] + lon - lon2)
            self.schedule_redraw()

        def schedule_redraw(self):
            if not self._redraw_pending:
                self._redraw_pending = True
                self.after(40, self.redraw)

        def _line(self, parts, **kw):
            for part in parts:
                if len(part) < 2:
                    continue
                pts = []
                for lat, lon in part:
                    pts.extend(self.project(lat, lon))
                self.canvas.create_line(*pts, **kw)

        def _context(self):
            if self._dbs is None:
                self._dbs = []
                for p in self.app.store.region_paths():
                    try:
                        self._dbs.append(RegionDB(p))
                    except sqlite3.Error:
                        pass
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
            lat_top, lon_left = self.unproject(0, 0)
            lat_bot, lon_right = self.unproject(w, h)
            bb = (lat_bot, lat_top, lon_left, lon_right)
            for db in self._dbs:
                try:
                    feats = db.fetch("1", [], bb, limit=3000, geom_only=True)
                except sqlite3.Error:
                    continue
                for f in feats:
                    key = f.category.partition("=")[0]
                    g = f.geom
                    if g.is_area:
                        color = "#9fc79a" if key in ("leisure", "boundary", "landuse", "tourism") else "#a9c8e8"
                        self._line(g.parts, fill=color, width=1)
                    elif key in ("waterway", "natural"):
                        self._line(g.parts, fill="#7fa9d6", width=1)
                    else:
                        self._line(g.parts, fill="#b99a74", width=1, dash=(4, 2))

        def redraw(self):
            self._redraw_pending = False
            c = self.canvas
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
            if not self.anchors:
                c.create_text(w / 2, h / 2, text="Run a search to plot results here.", fill="#777")
                return
            if self.ctx_var.get():
                self._context()
            # anchors
            pt_anchors = [a for a in self.anchors if not a.geom.parts]
            for a in self.anchors[:5000]:
                if a.geom.parts:
                    self._line(a.geom.parts, fill="#d9731a", width=3)
            if self.max_m and len(pt_anchors) <= 50:
                r = self.max_m / self.mpp
                for a in pt_anchors:
                    x, y = self.project(a.geom.lat, a.geom.lon)
                    c.create_oval(x - r, y - r, x + r, y + r, outline="#d9731a", dash=(6, 4))
            for a in pt_anchors[:5000]:
                x, y = self.project(a.geom.lat, a.geom.lon)
                c.create_polygon(x, y - 8, x - 7, y + 5, x + 7, y + 5, fill="#d9731a", outline="#8a4510")
            # results
            for i, r in enumerate(self.results):
                x, y = self.project(*r.target_pt)
                if -20 < x < w + 20 and -20 < y < h + 20:
                    c.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#2463b8", outline="white",
                                  tags=(f"res:{i}",))
                    if i < 40:
                        c.create_text(x + 7, y - 7, text=str(i + 1), anchor="w", fill="#1b3f73",
                                      font=("TkDefaultFont", 8))
            if self.selected is not None and self.selected < len(self.results):
                r = self.results[self.selected]
                ax, ay = self.project(*r.anchor_pt)
                x, y = self.project(*r.target_pt)
                c.create_line(ax, ay, x, y, fill="#b8246b", dash=(3, 3), width=2)
                c.create_oval(x - 7, y - 7, x + 7, y + 7, outline="#b8246b", width=3)
                label = f"{r.feature.name}  {fmt_distance(r.dist, self.unit)}"
                c.create_text(x + 10, y + 10, text=label, anchor="nw", fill="#b8246b",
                              font=("TkDefaultFont", 10, "bold"))
            self._scale_bar(w, h)
            c.create_text(w - 6, h - 6, text="Data (c) OpenStreetMap contributors, ODbL",
                          anchor="se", fill="#888", font=("TkDefaultFont", 8))

        def _scale_bar(self, w, h):
            unit_m = UNITS[self.unit]
            target = 120 * self.mpp / unit_m
            mag = 10 ** math.floor(math.log10(target)) if target > 0 else 1
            nice = min((m * mag for m in (1, 2, 5, 10)), key=lambda v: abs(v - target))
            px = nice * unit_m / self.mpp
            x0, y0 = 16, h - 18
            self.canvas.create_line(x0, y0, x0 + px, y0, width=3)
            self.canvas.create_line(x0, y0 - 5, x0, y0 + 5)
            self.canvas.create_line(x0 + px, y0 - 5, x0 + px, y0 + 5)
            txt = f"{nice:g} {self.unit}"
            self.canvas.create_text(x0 + px / 2, y0 - 10, text=txt)

    # ---------------------------------------------------------------- Places
    class PlacesPanel(ttk.Frame):
        def __init__(self, master, app):
            super().__init__(master, padding=8)
            self.app = app
            form = ttk.LabelFrame(self, text="Add a place (a campsite, trailhead, home, anything)", padding=6)
            form.pack(fill="x")
            self.name_var, self.coord_var, self.note_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
            for col, (label, var, width) in enumerate((("Name", self.name_var, 24),
                                                       ("Coordinates", self.coord_var, 30),
                                                       ("Note", self.note_var, 30))):
                ttk.Label(form, text=label).grid(row=0, column=col, sticky="w")
                ttk.Entry(form, textvariable=var, width=width).grid(row=1, column=col, sticky="ew", padx=(0, 6))
            ttk.Button(form, text="Add", command=self.add).grid(row=1, column=3)
            cols = (("name", "Name", 220), ("lat", "Lat", 110), ("lon", "Lon", 110), ("note", "Note", 360))
            self.tree = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings")
            for k, t, wd in cols:
                self.tree.heading(k, text=t)
                self.tree.column(k, width=wd, stretch=k == "note")
            self.tree.pack(fill="both", expand=True, pady=6)
            bar = ttk.Frame(self)
            bar.pack(fill="x")
            ttk.Button(bar, text="Use as anchor", command=self.use).pack(side="left")
            ttk.Button(bar, text="Copy coordinates", command=self.copy).pack(side="left", padx=4)
            ttk.Button(bar, text="Delete", command=self.delete).pack(side="right")
            self.refresh()

        def prefill(self, name, lat, lon):
            self.name_var.set(name)
            self.coord_var.set(f"{lat:.6f}, {lon:.6f}")

        def refresh(self):
            self.tree.delete(*self.tree.get_children())
            for pid, name, lat, lon, note in self.app.store.places():
                self.tree.insert("", "end", iid=str(pid), values=(name, f"{lat:.6f}", f"{lon:.6f}", note))

        def add(self):
            name = self.name_var.get().strip()
            try:
                if not name:
                    raise ValueError("Give the place a name.")
                lat, lon = parse_coords(self.coord_var.get())
            except ValueError as e:
                messagebox.showerror("Saved places", str(e), parent=self)
                return
            self.app.store.add_place(name, lat, lon, self.note_var.get().strip())
            for v in (self.name_var, self.coord_var, self.note_var):
                v.set("")
            self.app.places_changed()

        def _sel(self):
            sel = self.tree.selection()
            return self.tree.item(sel[0], "values") if sel else None

        def use(self):
            v = self._sel()
            if v:
                self.app.search.set_chosen([Anchor.from_point(v[0], float(v[1]), float(v[2]))], v[0])
                self.app.nb.select(self.app.search)

        def copy(self):
            v = self._sel()
            if v:
                self.clipboard_clear()
                self.clipboard_append(f"{v[1]}, {v[2]}")

        def delete(self):
            sel = self.tree.selection()
            if sel and messagebox.askyesno("Saved places", "Delete the selected place?", parent=self):
                self.app.store.delete_place(int(sel[0]))
                self.app.places_changed()

    # ---------------------------------------------------------------- Regions
    class RegionsPanel(ttk.Frame):
        def __init__(self, master, app):
            super().__init__(master, padding=8)
            self.app = app
            self.index = {}
            self.update_status = {}
            self.job = None
            self.cancel_event = None

            paned = ttk.Panedwindow(self, orient="horizontal")
            paned.pack(fill="both", expand=True)
            left = ttk.LabelFrame(paned, text="Available downloads (Geofabrik OpenStreetMap extracts)", padding=6)
            right = ttk.LabelFrame(paned, text="Installed regions (usable offline)", padding=6)
            paned.add(left, weight=1)
            paned.add(right, weight=2)

            bar = ttk.Frame(left)
            bar.pack(fill="x")
            ttk.Label(bar, text="Filter").pack(side="left")
            self.filter_var = tk.StringVar()
            self.filter_var.trace_add("write", lambda *_a: self.populate_index())
            ttk.Entry(bar, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(bar, text="Refresh list (online)", command=self.refresh_index).pack(side="left")
            tf = ttk.Frame(left)
            tf.pack(fill="both", expand=True, pady=4)
            self.avail = ttk.Treeview(tf, show="tree", selectmode="extended")
            ys = ttk.Scrollbar(tf, orient="vertical", command=self.avail.yview)
            self.avail.configure(yscrollcommand=ys.set)
            self.avail.pack(side="left", fill="both", expand=True)
            ys.pack(side="right", fill="y")
            opts = ttk.Frame(left)
            opts.pack(fill="x")
            self.addr_var = tk.BooleanVar(value=self.app.store.get_setting("include_addresses", False))
            ttk.Checkbutton(opts, text="Include street addresses (much larger database)",
                            variable=self.addr_var).pack(anchor="w")
            self.keep_var = tk.BooleanVar(value=self.app.store.get_setting("keep_pbf", False))
            ttk.Checkbutton(opts, text="Keep downloaded .osm.pbf (allows re-import without data)",
                            variable=self.keep_var).pack(anchor="w")
            ttk.Button(left, text="Download and import selected", command=self.install_selected).pack(
                fill="x", pady=(4, 0))

            cols = (("data", "OSM data as of", 150), ("imported", "Imported", 150),
                    ("features", "Features", 80), ("size", "Size", 80), ("status", "Update status", 150))
            self.inst = ttk.Treeview(right, columns=[c[0] for c in cols], show="tree headings",
                                     selectmode="extended")
            self.inst.heading("#0", text="Region")
            self.inst.column("#0", width=180)
            for k, t, wd in cols:
                self.inst.heading(k, text=t)
                self.inst.column(k, width=wd, stretch=False)
            self.inst.pack(fill="both", expand=True)
            b = ttk.Frame(right)
            b.pack(fill="x", pady=(4, 0))
            ttk.Button(b, text="Check for updates", command=self.check_updates).pack(side="left")
            ttk.Button(b, text="Update selected", command=lambda: self.update_regions(False)).pack(side="left", padx=4)
            ttk.Button(b, text="Update all outdated", command=lambda: self.update_regions(True)).pack(side="left")
            ttk.Button(b, text="Remove", command=self.remove_selected).pack(side="right")
            ttk.Button(b, text="Re-import kept .pbf", command=self.reimport_selected).pack(side="right", padx=4)
            ttk.Button(b, text="Import local file...", command=self.import_local).pack(side="right")

            low = ttk.Frame(self)
            low.pack(fill="x", pady=(8, 0))
            pr = ttk.Frame(low)
            pr.pack(fill="x")
            self.prog = ttk.Progressbar(pr, maximum=1000)
            self.prog.pack(side="left", fill="x", expand=True)
            self.cancel_btn = ttk.Button(pr, text="Cancel job", command=self.cancel, state="disabled")
            self.cancel_btn.pack(side="left", padx=(6, 0))
            self.prog_text = tk.StringVar(value="Idle.")
            ttk.Label(low, textvariable=self.prog_text).pack(anchor="w")
            self.log = tk.Text(low, height=8, state="disabled", wrap="word")
            self.log.pack(fill="x")

            self.load_index()
            self.refresh_installed()

        # -- logging / progress (thread-safe wrappers)
        def log_line(self, text):
            def do():
                self.log.configure(state="normal")
                self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
                self.prog_text.set(text)
            self.app.ui(do)

        def progress(self, frac, text):
            def do():
                if frac is None:
                    if str(self.prog["mode"]) != "indeterminate":
                        self.prog.configure(mode="indeterminate")
                        self.prog.start(15)
                else:
                    if str(self.prog["mode"]) != "determinate":
                        self.prog.stop()
                        self.prog.configure(mode="determinate")
                    self.prog["value"] = frac * 1000
                self.prog_text.set(text)
            self.app.ui(do)

        def run_job(self, title, fn):
            if self.job and self.job.is_alive():
                messagebox.showinfo(APP_NAME, "Another download/import job is running.", parent=self)
                return
            self.cancel_event = threading.Event()
            cancel = self.cancel_event
            self.cancel_btn.configure(state="normal")
            self.log_line(f"== {title}")

            def wrapper():
                try:
                    fn(cancel)
                    self.log_line(f"== {title}: done")
                except Cancelled:
                    self.log_line(f"== {title}: cancelled")
                except urllib.error.URLError as e:
                    self.log_line(f"== {title}: network error: {e}. Are you online?")
                except Exception as e:
                    traceback.print_exc()
                    self.log_line(f"== {title}: failed: {e}")
                finally:
                    self.app.ui(self._job_finished)

            self.job = threading.Thread(target=wrapper, daemon=True)
            self.job.start()

        def _job_finished(self):
            self.cancel_btn.configure(state="disabled")
            self.prog.stop()
            self.prog.configure(mode="determinate")
            self.prog["value"] = 0
            self.app.regions_changed()

        def cancel(self):
            if self.cancel_event:
                self.cancel_event.set()
                self.log_line("Cancelling...")

        # -- available list
        def load_index(self):
            self.index = {}
            if os.path.exists(self.app.store.index_path):
                try:
                    with open(self.app.store.index_path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    for feat in data.get("features", []):
                        p = feat.get("properties", {})
                        if p.get("id") and p.get("urls", {}).get("pbf"):
                            self.index[p["id"]] = p
                except (OSError, ValueError) as e:
                    self.log_line(f"Could not read cached region list: {e}")
            self.populate_index()

        def populate_index(self):
            t = self.avail
            t.delete(*t.get_children())
            if not self.index:
                t.insert("", "end", iid="__hint", text="Click 'Refresh list (online)' to load regions.")
                return
            flt = self.filter_var.get().strip().lower()
            if flt:
                for rid, p in sorted(self.index.items(), key=lambda kv: kv[1].get("name", "")):
                    if flt in p.get("name", "").lower() or flt in rid.lower():
                        parent = self.index.get(p.get("parent"), {}).get("name", "")
                        t.insert("", "end", iid=rid, text=f"{p['name']}" + (f"  ({parent})" if parent else ""))
                return
            children = defaultdict(list)
            for rid, p in self.index.items():
                children[p.get("parent") if p.get("parent") in self.index else ""].append(rid)

            def add(parent_iid, key):
                for rid in sorted(children.get(key, []), key=lambda r: self.index[r].get("name", "")):
                    t.insert(parent_iid, "end", iid=rid, text=self.index[rid].get("name", rid))
                    add(rid, rid)

            add("", "")

        def refresh_index(self):
            def job(cancel):
                self.progress(None, "Downloading region list from Geofabrik...")
                http_download(GEOFABRIK_INDEX_URL, self.app.store.index_path,
                              lambda f, txt: self.progress(f, txt), cancel)
                self.app.ui(self.load_index)
                self.log_line("Region list updated.")
            self.run_job("Refresh region list", job)

        def install_selected(self):
            ids = [i for i in self.avail.selection() if i in self.index]
            if not ids:
                messagebox.showinfo(APP_NAME, "Select one or more regions on the left.", parent=self)
                return
            big = [self.index[i]["name"] for i in ids if not self.index[i].get("parent")]
            if big and not messagebox.askyesno(
                    APP_NAME, "These are continent-level extracts and are very large "
                              f"(many GB, long imports): {', '.join(big)}. Continue?", parent=self):
                return
            addr, keep = self.addr_var.get(), self.keep_var.get()
            self.app.store.set_setting("include_addresses", addr)
            self.app.store.set_setting("keep_pbf", keep)
            props = [self.index[i] for i in ids]

            def job(cancel):
                for p in props:
                    self._install(p["id"], p["name"], p["urls"]["pbf"], addr, keep, cancel)
            self.run_job(f"Install {len(props)} region(s)", job)

        def _install(self, rid, name, url, addr, keep, cancel):
            dest = os.path.join(self.app.store.downloads_dir, safe_name(rid) + ".osm.pbf")
            self.log_line(f"Downloading {name}: {url}")
            last_mod = http_download(url, dest, self.progress, cancel)
            self._import(dest, rid, name, url, last_mod, addr, keep, cancel)

        def _import(self, pbf, rid, name, url, last_mod, addr, keep, cancel):
            self.progress(None, f"Importing {name}...")
            self.app.plot.invalidate_dbs()  # Windows cannot replace a file that is open
            meta = {"region_id": rid, "name": name, "source_url": url,
                    "remote_last_modified": last_mod or "", "pbf_path": pbf if keep else ""}
            import_pbf(pbf, self.app.store.region_path_for(rid), meta, addr, self.log_line, cancel)
            if not keep and pbf.startswith(self.app.store.downloads_dir):
                try:
                    os.remove(pbf)
                except OSError:
                    pass
            self.update_status.pop(rid, None)

        # -- installed list
        def refresh_installed(self):
            t = self.inst
            t.delete(*t.get_children())
            for path in self.app.store.region_paths():
                m = Store.region_meta(path)
                rid = m.get("region_id", Path(path).stem)
                t.insert("", "end", iid=path, text=m.get("name", rid), values=(
                    (m.get("data_timestamp") or "unknown").replace("T", " ").replace("Z", " UTC"),
                    (m.get("imported_at") or "")[:16].replace("T", " "),
                    f"{int(m.get('feature_count', 0)):,}",
                    human_size(os.path.getsize(path)),
                    self.update_status.get(rid, "")))

        def _selected_installed(self):
            return [(p, Store.region_meta(p)) for p in self.inst.selection()]

        def check_updates(self):
            items = [(p, Store.region_meta(p)) for p in self.app.store.region_paths()]

            def job(cancel):
                for path, m in items:
                    if cancel.is_set():
                        raise Cancelled()
                    rid = m.get("region_id", Path(path).stem)
                    url = m.get("source_url")
                    if not url:
                        self.update_status[rid] = "local file (no URL)"
                        continue
                    remote = remote_last_modified(url)
                    newer = is_newer(remote, m.get("remote_last_modified"))
                    self.update_status[rid] = ("update available" if newer else
                                               "up to date" if newer is False else "unknown")
                    self.log_line(f"{m.get('name', rid)}: {self.update_status[rid]} (server: {remote or 'n/a'})")
            self.run_job("Check for updates", job)

        def update_regions(self, only_outdated):
            items = ([(p, Store.region_meta(p)) for p in self.app.store.region_paths()]
                     if only_outdated else self._selected_installed())
            if not items:
                messagebox.showinfo(APP_NAME, "Select installed regions to update.", parent=self)
                return
            keep = self.keep_var.get()

            def job(cancel):
                for path, m in items:
                    rid = m.get("region_id", Path(path).stem)
                    url = m.get("source_url")
                    if not url:
                        self.log_line(f"Skipping {m.get('name', rid)}: imported from a local file.")
                        continue
                    if only_outdated:
                        remote = remote_last_modified(url)
                        if is_newer(remote, m.get("remote_last_modified")) is False:
                            self.log_line(f"{m.get('name', rid)} is up to date.")
                            continue
                    self._install(rid, m.get("name", rid), url, m.get("include_addresses") == "1", keep, cancel)
            self.run_job("Update regions", job)

        def reimport_selected(self):
            items = self._selected_installed()
            usable = [(p, m) for p, m in items if m.get("pbf_path") and os.path.exists(m["pbf_path"])]
            if not usable:
                messagebox.showinfo(APP_NAME, "None of the selected regions has a kept .pbf file.", parent=self)
                return

            def job(cancel):
                for _p, m in usable:
                    self._import(m["pbf_path"], m["region_id"], m.get("name", m["region_id"]),
                                 m.get("source_url", ""), m.get("remote_last_modified", ""),
                                 m.get("include_addresses") == "1", True, cancel)
            self.run_job("Re-import", job)

        def import_local(self):
            path = filedialog.askopenfilename(parent=self, title="Choose an OSM file", filetypes=[
                ("OSM data", "*.osm.pbf *.pbf *.osm *.osm.bz2 *.osm.gz"), ("All files", "*")])
            if not path:
                return
            stem = re.sub(r"(\.osm)?(\.pbf|\.bz2|\.gz)?$", "", os.path.basename(path))
            addr = self.addr_var.get()

            def job(cancel):
                self._import(path, "local-" + stem, stem, "", "", addr, True, cancel)
            self.run_job(f"Import {os.path.basename(path)}", job)

        def remove_selected(self):
            items = self._selected_installed()
            if not items:
                return
            names = ", ".join(m.get("name", Path(p).stem) for p, m in items)
            if not messagebox.askyesno(APP_NAME, f"Delete offline data for: {names}?", parent=self):
                return
            self.app.plot.invalidate_dbs()
            for p, m in items:
                try:
                    os.remove(p)
                    kept = m.get("pbf_path")
                    if kept and kept.startswith(self.app.store.downloads_dir) and os.path.exists(kept):
                        os.remove(kept)
                except OSError as e:
                    messagebox.showerror(APP_NAME, f"Could not delete {p}: {e}", parent=self)
            self.app.regions_changed()

    App().mainloop()


# --------------------------------------------------------------------------
# Self-test (used by CI against both the source and the frozen executable)
# --------------------------------------------------------------------------

SELF_TEST_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="OffGridFinder self-test">
 <node id="1" lat="34.5000" lon="-83.3000"><tag k="shop" v="variety_store"/><tag k="name" v="Dollar General"/><tag k="brand" v="Dollar General"/></node>
 <node id="2" lat="34.6000" lon="-83.3000"><tag k="shop" v="variety_store"/><tag k="name" v="Dollar General"/><tag k="brand" v="Dollar General"/></node>
 <node id="3" lat="34.9000" lon="-83.3000"><tag k="shop" v="variety_store"/><tag k="name" v="Dollar General"/></node>
 <node id="4" lat="34.5500" lon="-83.2500"><tag k="tourism" v="camp_site"/><tag k="name" v="Pine Camp"/></node>
 <node id="5" lat="34.5201" lon="-83.3101"><tag k="amenity" v="bench"/></node>
 <node id="10" lat="34.50" lon="-83.20"/><node id="11" lat="34.60" lon="-83.20"/><node id="12" lat="34.70" lon="-83.20"/>
 <node id="20" lat="34.40" lon="-83.50"/><node id="21" lat="34.40" lon="-83.40"/>
 <node id="22" lat="34.45" lon="-83.40"/><node id="23" lat="34.45" lon="-83.50"/>
 <way id="100"><nd ref="10"/><nd ref="11"/><nd ref="12"/><tag k="highway" v="path"/><tag k="name" v="Test Ridge Trail"/></way>
 <way id="101"><nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="20"/></way>
 <relation id="500"><member type="way" ref="101" role="outer"/><tag k="type" v="multipolygon"/><tag k="leisure" v="park"/><tag k="name" v="Big Test Park"/></relation>
</osm>
"""


def self_test(log_path=None):
    """Import a tiny embedded dataset and run representative searches. Returns a process exit code.
    log_path also receives the output, because windowed Windows builds have no usable stdout."""
    import tempfile
    failures = []
    log = open(log_path, "w", encoding="utf-8") if log_path else None

    def say(line):
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    def check(cond, what):
        say(("PASS " if cond else "FAIL ") + what)
        if not cond:
            failures.append(what)

    try:
        check(HAVE_OSMIUM, "pyosmium importable")
        check(sqlite_has_fts5(), "SQLite FTS5 available")
        try:
            import tkinter
            tkinter.Tcl().eval("info patchlevel")
            check(True, "Tcl/Tk runtime present")
        except Exception as e:
            check(False, f"Tcl/Tk runtime present ({e})")
        check(os.path.exists(resource_path("assets", "icon-256.png")), "icon asset bundled")
        with tempfile.TemporaryDirectory() as d:
            osm = os.path.join(d, "sample.osm")
            with open(osm, "w", encoding="utf-8") as fh:
                fh.write(SELF_TEST_OSM)
            store = Store(os.path.join(d, "data"))
            n = import_pbf(osm, store.region_path_for("selftest"),
                           {"region_id": "selftest", "name": "Self test", "source_url": ""},
                           False, lambda _m: None, threading.Event())
            check(n == 6, f"import kept 6 features (got {n})")
            eng = SearchEngine(store)
            cancel, quiet = threading.Event(), (lambda _m: None)
            camp = [Anchor.from_point("camp", 34.55, -83.25)]
            res, _ = eng.search(TargetSpec("dollar general"), camp, None, 10 * UNITS["mi"], 50, cancel, quiet)
            check(len(res) == 2, f"radius search found 2 stores (got {len(res)})")
            res, _ = eng.search(TargetSpec("", "Dollar / variety store"), camp, None, None, 50, cancel, quiet)
            check(len(res) == 3 and res[0].dist <= res[-1].dist, "unbounded search sorted by distance")
            trail = eng.lookup(TargetSpec("Test Ridge Trail"), 5)
            check(len(trail) == 1 and trail[0].geom.parts is not None, "trail stored with geometry")
            res, _ = eng.search(TargetSpec("", tagfilter="brand=dollar general"),
                                [Anchor.from_feature(f) for f in trail], None, 7 * UNITS["mi"], 50, cancel, quiet)
            check(len(res) == 2 and abs(res[0].dist / UNITS["mi"] - 5.69) < 0.05,
                  "distance to trail line is correct")
            res, _ = eng.search(TargetSpec("big test park"), [Anchor.from_point("in", 34.42, -83.45)],
                                None, None, 5, cancel, quiet)
            check(len(res) == 1 and res[0].dist == 0.0, "point inside park area measures 0")
    except Exception as e:
        say(traceback.format_exc())
        failures.append(f"exception: {e}")
    say("SELF-TEST " + ("FAILED: " + "; ".join(failures) if failures else "OK"))
    if log:
        log.close()
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog=APP_NAME,
                                 description="Offline proximity search over OpenStreetMap data.")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    ap.add_argument("--self-test", action="store_true", help="run built-in checks and exit (0 = pass)")
    ap.add_argument("--log", metavar="FILE", help="also write --self-test output to FILE")
    ap.add_argument("--data-dir", help=f"where regions and settings live (default: {default_data_dir()})")
    ap.add_argument("--import-file", metavar="PBF", help="import an OSM file without opening the GUI")
    ap.add_argument("--name", help="region name to use with --import-file")
    ap.add_argument("--addresses", action="store_true", help="include street addresses with --import-file")
    # parse_known_args: macOS may pass extra arguments (e.g. -psn_...) to app bundles
    args, _unknown = ap.parse_known_args()
    if args.version:
        print(APP_VERSION)
        return 0
    if args.self_test:
        return self_test(args.log)
    store = Store(args.data_dir)
    if args.import_file:
        stem = args.name or re.sub(r"(\.osm)?(\.pbf|\.bz2|\.gz)?$", "", os.path.basename(args.import_file))
        import_pbf(args.import_file, store.region_path_for("local-" + stem),
                   {"region_id": "local-" + stem, "name": stem, "source_url": "",
                    "remote_last_modified": "", "pbf_path": os.path.abspath(args.import_file)},
                   args.addresses, lambda s: print(s, flush=True), threading.Event())
        return 0
    try:
        run_gui(store)
    except ImportError as e:
        sys.exit(f"Tkinter is not available ({e}). On openSUSE: sudo zypper install python3-tk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
