import csv
import io
import urllib.request
import zipfile
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from optimal_route.models import FuelStation

GEONAMES_ZIP = "https://download.geonames.org/export/zip/US.zip"


def _norm(city):
    return " ".join(city.lower().replace(".", "").replace("saint ", "st ").split())


class Command(BaseCommand):
    help = "Load fuel stations from the CSV, geocoding each by city/state (GeoNames postal centroids)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")

    def handle(self, csv_path, **options):
        with urllib.request.urlopen(GEONAMES_ZIP, timeout=60) as resp:
            archive = zipfile.ZipFile(io.BytesIO(resp.read()))
        sums = defaultdict(lambda: [0.0, 0.0, 0])
        for line in archive.read("US.txt").decode("utf-8").splitlines():
            f = line.split("\t")
            s = sums[(_norm(f[2]), f[4])]
            s[0] += float(f[9])
            s[1] += float(f[10])
            s[2] += 1

        best = {}
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row["OPIS Truckstop ID"], row["Address"].strip(), row["City"].strip().lower())
                price = Decimal(row["Retail Price"]).quantize(Decimal("0.001"))
                if key not in best or price < best[key][1]:
                    best[key] = (row, price)

        stations, missed = [], 0
        for row, price in best.values():
            s = sums.get((_norm(row["City"]), row["State"].strip()))
            if not s:
                missed += 1
                continue
            stations.append(FuelStation(
                opis_id=int(row["OPIS Truckstop ID"]),
                name=row["Truckstop Name"].strip(),
                address=row["Address"].strip(),
                city=row["City"].strip(),
                state=row["State"].strip(),
                price=price,
                latitude=s[0] / s[2],
                longitude=s[1] / s[2],
            ))
        FuelStation.objects.all().delete()
        FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(f"Loaded {len(stations)} stations; {missed} skipped (city not found).")
