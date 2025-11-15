import os
import json
import time
from datetime import datetime
from pathlib import Path

import ee
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# ==========================
# CONFIG
# ==========================
EXPORT_FOLDER_NAME = os.getenv("EXPORT_FOLDER", "EarthEngineExports")
EXPORT_FOLDER_ID = os.getenv("EXPORT_FOLDER_ID", "")  # optional
EE_SA_KEY = os.getenv("EE_SA_KEY")
EE_SA_EMAIL = os.getenv("EE_SA_EMAIL")

POLL_INTERVAL = 20       # seconds
POLL_TIMEOUT = 60 * 30   # 30 minutes

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ==========================
# VALIDATE SECRETS
# ==========================
if not EE_SA_KEY or not EE_SA_EMAIL:
    raise SystemExit("ERROR: EE_SA_KEY or EE_SA_EMAIL missing. Add them as GitHub Secrets.")


# ==========================
# INIT EARTH ENGINE
# ==========================
with open("sa-key.json", "w") as f:
    f.write(EE_SA_KEY)

credentials = ee.ServiceAccountCredentials(EE_SA_EMAIL, "sa-key.json")
ee.Initialize(credentials)

# ==========================
# INIT GOOGLE DRIVE API
# ==========================
drive_creds = service_account.Credentials.from_service_account_file(
    'sa-key.json', scopes=['https://www.googleapis.com/auth/drive']
)
drive = build('drive', 'v3', credentials=drive_creds)


# --------------------------------------------------------
# HELPER: Find or create Google Drive folder
# --------------------------------------------------------
def get_drive_folder_id():
    if EXPORT_FOLDER_ID:
        return EXPORT_FOLDER_ID

    query = f"name='{EXPORT_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder'"
    res = drive.files().list(q=query, fields="files(id)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    # Create if not exist
    metadata = {
        "name": EXPORT_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder"
    }
    new_folder = drive.files().create(body=metadata, fields="id").execute()
    return new_folder["id"]


# --------------------------------------------------------
# CREATE EXPORT IMAGE WITH CORRECT UNITS + HUMIDITY
# --------------------------------------------------------
def combine_image(ndvi_image, era5, smap):
    date = ee.Date(ndvi_image.get('system:time_start'))

    weather = era5.filterDate(date, date.advance(1, 'day')).mean()
    soil = smap.filterDate(date, date.advance(1, 'day')).mean()

    # Convert temperatures (K → °C)
    tmax_C = weather.select('temperature_2m_max').subtract(273.15).rename('temperature_max_C')
    tmin_C = weather.select('temperature_2m_min').subtract(273.15).rename('temperature_min_C')
    tmean_C = tmax_C.add(tmin_C).divide(2).rename('temperature_mean_C')

    # Dewpoint → °C
    dew_C = weather.select('dewpoint_temperature_2m').subtract(273.15)

    # Relative humidity calculation (Magnus formula)
    a = 17.625
    b = 243.04
    rh = ee.Image().expression(
        "100 * (exp(a * Td/(b + Td)) / exp(a * T/(b + T)))",
        {"a": a, "b": b, "Td": dew_C, "T": tmean_C}
    ).rename("relative_humidity_percent")

    # Precipitation (m → mm)
    precip_mm = weather.select('total_precipitation_sum').multiply(1000).rename("precipitation_mm")

    # Windspeed = sqrt(u² + v²)
    u = weather.select('u_component_of_wind_10m_max')
    v = weather.select('v_component_of_wind_10m_max')
    windspeed = u.pow(2).add(v.pow(2)).sqrt().rename("windspeed_10m_max")

    # Soil moisture
    soil_moisture = soil.select('ssm').rename("soil_moisture")

    return ee.Image.cat([
        ndvi_image.rename("NDVI"),
        soil_moisture,
        tmax_C, tmin_C, tmean_C,
        rh,
        precip_mm,
        weather.select('potential_evaporation_sum').rename("evapotranspiration"),
        windspeed
    ]).set("date", date.format("YYYY-MM-dd"))


# --------------------------------------------------------
# COMPUTE ZONAL STATS
# --------------------------------------------------------
def zonal_stats(image, regions):
    def compute(f):
        stats = image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=f.geometry(),
            scale=250,
            maxPixels=1e13
        )
        return f.set(stats).set("date", image.get("date"))

    return regions.map(compute)


# --------------------------------------------------------
# START EXPORT JOBS
# --------------------------------------------------------
def start_export_tasks(folder_id):
    asset_mandals   = "projects/compact-marker-441912-a5/assets/new"
    asset_districts = "projects/compact-marker-441912-a5/assets/TS_District_Boundary_33_FINAL"

    mandals = ee.FeatureCollection(asset_mandals)
    districts = ee.FeatureCollection(asset_districts)

    # ERA5 + SMAP
    era5 = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
    smap = ee.ImageCollection('NASA_USDA/HSL/SMAP10KM_soil_moisture')

    # NDVI 2 months
    today = ee.Date(datetime.utcnow())
    start = today.advance(-2, "month")

    dates = ee.ImageCollection("MODIS/061/MOD13Q1") \
                .select("NDVI") \
                .filterDate(start, today) \
                .aggregate_array("system:time_start")

    def build(t):
        date = ee.Date(t)
        ndvi = ee.ImageCollection("MODIS/061/MOD13Q1") \
                  .select("NDVI") \
                  .filterDate(date, date.advance(1, "day")) \
                  .mean() \
                  .multiply(0.0001) \
                  .set("system:time_start", t)
        return combine_image(ndvi, era5, smap)

    coll = ee.ImageCollection(dates.map(build))

    # Task descriptions
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M")

    def run_task(collection, desc):
        task = ee.batch.Export.table.toDrive(
            collection=collection,
            folder=EXPORT_FOLDER_NAME,
            description=f"{desc}_{timestamp}",
            fileFormat="CSV"
        )
        task.start()
        return task

    # 4 tasks
    tasks = []
    tasks.append(run_task(coll.map(lambda i: zonal_stats(i, mandals)).flatten(),
                          "Mandals_AllVars"))
    tasks.append(run_task(coll.map(lambda i: zonal_stats(i.select("NDVI"), mandals)).flatten(),
                          "Mandals_NDVI"))
    tasks.append(run_task(coll.map(lambda i: zonal_stats(i, districts)).flatten(),
                          "Districts_AllVars"))
    tasks.append(run_task(coll.map(lambda i: zonal_stats(i.select("NDVI"), districts)).flatten(),
                          "Districts_NDVI"))

    return tasks


# --------------------------------------------------------
# POLL FOR COMPLETION
# --------------------------------------------------------
def wait_for_tasks(tasks):
    print("Polling Earth Engine export tasks...")

    start = time.time()

    while True:
        all_done = True
        for t in tasks:
            status = t.status()
            print(status)

            if status["state"] not in ["COMPLETED", "FAILED"]:
                all_done = False

        if all_done:
            break

        if time.time() - start > POLL_TIMEOUT:
            raise TimeoutError("Timed out waiting for EE tasks.")

        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------
# DOWNLOAD FILES FROM DRIVE → data/
# --------------------------------------------------------
def download_csvs(folder_id):
    query = f"'{folder_id}' in parents and mimeType='text/csv'"
    results = drive.files().list(q=query, fields="files(id, name)").execute()

    for f in results["files"]:
        file_id = f["id"]
        name = f["name"]
        print("Downloading:", name)

        request = drive.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        with open(DATA_DIR / name, "wb") as out:
            out.write(fh.getvalue())


# --------------------------------------------------------
# MAIN
# --------------------------------------------------------
def main():
    folder_id = get_drive_folder_id()
    print("Drive Folder ID:", folder_id)

    tasks = start_export_tasks(folder_id)

    wait_for_tasks(tasks)

    download_csvs(folder_id)

    print("All done! CSVs saved in data/ folder.")


if __name__ == "__main__":
    main()
