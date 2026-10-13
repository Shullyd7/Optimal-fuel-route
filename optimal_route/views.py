import json
import math
import urllib.parse
import urllib.request
from functools import lru_cache

from django.http import HttpResponse, JsonResponse
from django.views import View

from .models import FuelStation

MAX_RANGE_MILES = 500
MPG = 10
MAX_OFF_ROUTE_MILES = 5
POINT_SPACING_MILES = 2
GRID_DEG = 0.2
EARTH_MILES = 3958.8
USER_AGENT = {"User-Agent": "spotter-optimal-route/1.0"} #required by Norminatim 

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"


class RouteError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _get_json(url):
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_MILES * math.asin(math.sqrt(a))


@lru_cache(maxsize=1024)
def _geocode(text):
    """Return (lat, lon) for a 'lat,lon' string or a free-text US location (1 Nominatim call)."""
    parts = text.split(",")
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    query = urllib.parse.urlencode({"q": text, "format": "json", "limit": 1, "countrycodes": "us"})
    results = _get_json(f"{NOMINATIM_URL}?{query}")
    if not results:
        raise RouteError(f"Could not find a US location for '{text}'.")
    return float(results[0]["lat"]), float(results[0]["lon"])


@lru_cache(maxsize=256)
def _fetch_route(start, finish):
    """Single OSRM call. Returns ((lat, lon) points, true road distance in miles)."""
    coords = f"{start[1]},{start[0]};{finish[1]},{finish[0]}"
    data = _get_json(OSRM_URL.format(coords=coords) + "?overview=simplified&geometries=geojson")
    if data.get("code") != "Ok":
        raise RouteError("No drivable route found between those locations.")
    route = data["routes"][0]
    points = tuple((lat, lon) for lon, lat in route["geometry"]["coordinates"])
    return points, route["distance"] / 1609.344


def _densify(points, road_miles):
    """Insert points so spacing is <= POINT_SPACING_MILES; return points and cumulative miles
    (scaled so the total equals the true road distance, since simplified geometry cuts corners)."""
    pts, miles = [points[0]], [0.0]
    for (la1, lo1), (la2, lo2) in zip(points, points[1:]):
        seg = _haversine(la1, lo1, la2, lo2)
        steps = max(1, math.ceil(seg / POINT_SPACING_MILES))
        for i in range(1, steps + 1):
            f = i / steps
            pts.append((la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f))
            miles.append(miles[-1] + seg / steps)
    scale = road_miles / miles[-1] if miles[-1] else 1
    return pts, [m * scale for m in miles]


_stations_cache = None


def _all_stations():
    global _stations_cache
    if _stations_cache is None:
        _stations_cache = list(FuelStation.objects.all())
    return _stations_cache


def _stations_on_route(pts, miles):
    """Stations within MAX_OFF_ROUTE_MILES of the route, with their mile marker. Grid lookup, no per-station DB hit."""
    grid = {}
    for idx, (lat, lon) in enumerate(pts):
        grid.setdefault((int(lat // GRID_DEG), int(lon // GRID_DEG)), []).append(idx)

    found = []
    for st in _all_stations():
        cx, cy = int(st.latitude // GRID_DEG), int(st.longitude // GRID_DEG)
        best_d, best_i = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx in grid.get((cx + dx, cy + dy), ()):
                    d = _haversine(st.latitude, st.longitude, *pts[idx])
                    if best_d is None or d < best_d:
                        best_d, best_i = d, idx
        if best_d is not None and best_d <= MAX_OFF_ROUTE_MILES:
            found.append((miles[best_i], float(st.price), st))
    found.sort(key=lambda s: s[0])
    return found


def _plan_fuel_stops(candidates, total_miles):
    """Cost-minimising refuel plan. Tank starts empty; the starting fuel is priced at the first station's price."""
    if not candidates:
        raise RouteError("No fuel stations found along this route.", 422)
    # virtual start (mile 0) so the initial tank is accounted for
    nodes = [(0.0, candidates[0][1], None)] + candidates
    stops = []
    fuel = 0.0  # miles of range in tank
    for i, (mile, price, st) in enumerate(nodes):
        next_mile = nodes[i + 1][0] if i + 1 < len(nodes) else total_miles
        if next_mile - mile > MAX_RANGE_MILES:
            raise RouteError("Gap between fuel stations exceeds the 500 mile range.", 422)
        target = None
        for mile_j, price_j, _ in nodes[i + 1:]:
            if mile_j - mile > MAX_RANGE_MILES:
                break
            if price_j < price:
                target = mile_j - mile
                break
        if target is None:
            target = min(MAX_RANGE_MILES, total_miles - mile)
        buy_miles = max(0.0, target - fuel)
        if buy_miles > 0:
            gallons = buy_miles / MPG
            stops.append({"station": st, "mile": mile, "price": price,
                          "gallons": gallons, "cost": gallons * price})
            fuel += buy_miles
        fuel -= next_mile - mile
    return stops


def _station_dict(stop):
    st = stop["station"]
    return {
        "name": st.name, "address": st.address, "city": st.city, "state": st.state,
        "latitude": st.latitude, "longitude": st.longitude,
        "miles_from_start": round(stop["mile"], 1),
        "price_per_gallon": round(stop["price"], 3),
        "gallons": round(stop["gallons"], 2),
        "cost": round(stop["cost"], 2),
    }


MAP_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Route</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0}</style></head><body><div id="map"></div>
<script>
var data = __DATA__;
var map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
var route = L.geoJSON(data.route.features[0]).addTo(map);
map.fitBounds(route.getBounds());
data.route.features.slice(1).forEach(function(f){
  var p=f.properties, c=f.geometry.coordinates;
  L.marker([c[1],c[0]]).addTo(map).bindPopup('<b>'+p.label+'</b><br>'+(p.detail||''));
});
</script></body></html>"""


class OptimalRouteView(View):
    """GET /api/route/?start=<place or lat,lon>&finish=<place or lat,lon>[&format=html]"""

    def get(self, request):
        start_q = request.GET.get("start", "").strip()
        finish_q = request.GET.get("finish", "").strip()
        if not start_q or not finish_q:
            return JsonResponse({"error": "Both 'start' and 'finish' query params are required."}, status=400)
        try:
            start, finish = _geocode(start_q), _geocode(finish_q)
            pts, miles = _densify(*_fetch_route(start, finish))
            total_miles = miles[-1]
            plan = _plan_fuel_stops(_stations_on_route(pts, miles), total_miles)
        except RouteError as e:
            return JsonResponse({"error": str(e)}, status=e.status)
        except OSError:
            return JsonResponse({"error": "Map/routing service unavailable."}, status=502)
        starting_tank = next((s for s in plan if s["station"] is None), None)
        stops = [s for s in plan if s["station"] is not None]

        step = max(1, len(pts) // 2000)
        line = [[lon, lat] for lat, lon in pts[::step]] + [[pts[-1][1], pts[-1][0]]]
        features = [{"type": "Feature", "properties": {"label": "Route"},
                     "geometry": {"type": "LineString", "coordinates": line}}]
        for label, (lat, lon) in (("Start", start), ("Finish", finish)):
            features.append({"type": "Feature", "properties": {"label": label},
                             "geometry": {"type": "Point", "coordinates": [lon, lat]}})
        for s in stops:
            st = s["station"]
            features.append({"type": "Feature",
                             "properties": {"label": f"Fuel: {st.name}",
                                            "detail": f"${s['price']:.3f}/gal, {s['gallons']:.1f} gal, ${s['cost']:.2f}"},
                             "geometry": {"type": "Point", "coordinates": [st.longitude, st.latitude]}})

        payload = {
            "start": {"query": start_q, "latitude": start[0], "longitude": start[1]},
            "finish": {"query": finish_q, "latitude": finish[0], "longitude": finish[1]},
            "distance_miles": round(total_miles, 1),
            "total_gallons": round(total_miles / MPG, 2),
            "total_fuel_cost": round(sum(s["cost"] for s in plan), 2),
            "starting_tank": starting_tank and {
                "note": "Initial fuel, priced at the first station on the route.",
                "price_per_gallon": round(starting_tank["price"], 3),
                "gallons": round(starting_tank["gallons"], 2),
                "cost": round(starting_tank["cost"], 2),
            },
            "fuel_stops": [_station_dict(s) for s in stops],
            "route": {"type": "FeatureCollection", "features": features},
        }
        if request.GET.get("format") == "html":
            return HttpResponse(MAP_HTML.replace("__DATA__", json.dumps(payload)))
        return JsonResponse(payload)
