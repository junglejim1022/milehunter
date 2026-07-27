#!/usr/bin/env python3
"""
MileHunter — importerar sevärdheter från en OSM-fil (.osm.pbf) till Supabase.

Ersätter den gamla lösningen där varje spelares telefon frågade Overpass live.
Körs schemalagt (se .github/workflows/import-landmarks.yml), inte manuellt vid
varje spelares besök — det är hela poängen.

FÖRUTSÄTTNINGAR (en gång, innan första körningen):
  1. pip install osmium shapely requests --break-system-packages
  2. En fil data/swedish-municipalities.geojson med Sveriges kommungränser, där varje
     Feature har properties.lan_code (SCB:s länskod, t.ex. "04") och en Polygon/
     MultiPolygon-geometri. (T.ex. okfse/sweden-geojson på GitHub.)
  3. Miljövariabler (sätts som GitHub Actions-secrets, se workflow-filen):
     SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Kategori-reglerna nedan är hämtade rakt av från den live Overpass-frågan som redan
används i milehunter.html, så resultatet blir konsekvent med det appen redan samlat in.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

import osmium
import shapely.geometry as geom
from shapely.prepared import prep

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PBF_PATH = os.environ.get("PBF_PATH", "sweden-latest.osm.pbf")
MUNICIPALITIES_PATH = os.environ.get("MUNICIPALITIES_PATH", "data/swedish-municipalities.geojson")
BATCH_SIZE = 500

# SCB:s standardiserade länskoder — stabila, ändras i praktiken aldrig. Namnen är skrivna
# exakt så som appen redan använder dem på andra ställen (t.ex. "Södermanlands län").
LAN_CODE_TO_NAME = {
    "01": "Stockholms län", "03": "Uppsala län", "04": "Södermanlands län",
    "05": "Östergötlands län", "06": "Jönköpings län", "07": "Kronobergs län",
    "08": "Kalmar län", "09": "Gotlands län", "10": "Blekinge län",
    "12": "Skåne län", "13": "Hallands län", "14": "Västra Götalands län",
    "17": "Värmlands län", "18": "Örebro län", "19": "Västmanlands län",
    "20": "Dalarnas län", "21": "Gävleborgs län", "22": "Västernorrlands län",
    "23": "Jämtlands län", "24": "Västerbottens län", "25": "Norrbottens län",
}

# Kategori-nycklarna OCH tagg-reglerna matchar EXAKT den logik som redan används live i
# milehunter.html (fetchAchievementsForRegion) — hämtat direkt ur den koden, inte gissat.
def match_category(tags):
    if tags.get("tourism") == "castle" or tags.get("historic") == "castle":
        return "castle"
    if tags.get("natural") == "waterfall":
        return "waterfall"
    if tags.get("tourism") == "museum":
        return "museum"
    if tags.get("leisure") == "nature_reserve":
        return "nature_reserve"
    if tags.get("boundary") == "national_park":
        return "national_park"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if tags.get("abandoned") == "yes" or tags.get("ruins") == "yes":
        return "abandoned"
    if tags.get("natural") == "cave_entrance":
        return "cave"
    if tags.get("water") == "quarry":
        return "swimspot"
    if tags.get("natural") == "water" and "brott" in (tags.get("name") or "").lower():
        return "swimspot"
    return None


class LandmarkHandler(osmium.SimpleHandler):
    def __init__(self, municipalities):
        super().__init__()
        self.municipalities = municipalities  # list of (länsnamn, prepared_shapely_polygon för en kommun)
        self.results = []

    def region_for(self, lat, lon):
        pt = geom.Point(lon, lat)
        for lan_name, prepared_poly in self.municipalities:
            if prepared_poly.contains(pt):
                return lan_name
        return None

    def _handle(self, osm_type, tags, lat, lon):
        cat = match_category(tags)
        if not cat or "name" not in tags:
            return
        self.results.append({
            "id": f"osm_{osm_type}_{tags['_id']}",  # matchar exakt formatet det gamla live-systemet redan använder
            "name": tags.get("name"),
            "cat": cat,
            "lat": lat,
            "lon": lon,
            "region_name": self.region_for(lat, lon),
            "wikipedia": tags.get("wikipedia") or tags.get("wikipedia:sv"),
            "wikidata": tags.get("wikidata"),
            "source": "osm_import",
        })

    def node(self, n):
        tags = {t.k: t.v for t in n.tags}
        tags["_id"] = n.id
        if n.location.valid():
            self._handle("node", tags, n.location.lat, n.location.lon)

    def way(self, w):
        tags = {t.k: t.v for t in w.tags}
        tags["_id"] = w.id
        # Enkel approximation för en "centrumpunkt": mittersta noden i vägen.
        # apply_file(..., locations=True) gör att w.nodes[i].location redan är uppslaget.
        try:
            mid = w.nodes[len(w.nodes) // 2]
            if mid.location.valid():
                self._handle("way", tags, mid.location.lat, mid.location.lon)
        except (IndexError, RuntimeError):
            pass  # väg utan upplösta koordinater — hoppa över


def load_municipalities(path):
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    municipalities = []
    for feature in fc["features"]:
        lan_code = feature["properties"].get("lan_code")
        name = LAN_CODE_TO_NAME.get(lan_code)
        if not name:
            continue  # okänd/saknad länskod — hoppa över den kommunen, resten funkar ändå
        shape = geom.shape(feature["geometry"])
        municipalities.append((name, prep(shape)))
    return municipalities


def upsert_batch(rows):
    url = f"{SUPABASE_URL}/rest/v1/landmarks_catalog?on_conflict=id"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        print(f"Supabase-fel: {e.code} {e.read().decode()}", file=sys.stderr)
        raise


def main():
    if not os.path.exists(MUNICIPALITIES_PATH):
        print(f"Saknar {MUNICIPALITIES_PATH} — se docstringen högst upp i filen.", file=sys.stderr)
        sys.exit(1)

    print("Läser kommungränser...")
    municipalities = load_municipalities(MUNICIPALITIES_PATH)
    print(f"  {len(municipalities)} kommuner inlästa.")

    print(f"Läser {PBF_PATH} (kan ta några minuter för hela Sverige)...")
    handler = LandmarkHandler(municipalities)
    handler.apply_file(PBF_PATH, locations=True)
    print(f"  Hittade {len(handler.results)} sevärdheter.")

    print("Laddar upp till Supabase...")
    for i in range(0, len(handler.results), BATCH_SIZE):
        batch = handler.results[i:i + BATCH_SIZE]
        upsert_batch(batch)
        print(f"  {min(i + BATCH_SIZE, len(handler.results))}/{len(handler.results)}")
        time.sleep(0.2)  # snäll mot API:et

    print("Klart!")


if __name__ == "__main__":
    main()
