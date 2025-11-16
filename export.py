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

# CONFIG
EXPORT_FOLDER_NAME = os.getenv("EXPORT_FOLDER", "EarthEngineExports")
EXPORT_FOLDER_ID = os.getenv("EXPORT_FOLDER_ID", "")
EE_SA_KEY = os.getenv("EE_SA_KEY")
EE_SA_EMAIL = os.getenv("EE_SA_EMAIL")

POLL_INTERVAL = 20
POLL_TIMEOUT = 60 * 30

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

if not EE_SA_KEY or not EE_SA_EMAIL:
    raise SystemExit("ERROR: EE_SA_KEY or EE_SA_EMAIL missing.")

# INIT EARTH ENGINE
with open("sa-key.json", "w") as f:
    f.write(EE_SA_KEY)

credentials = ee.ServiceAccountCredentials(EE_SA_EMAIL, "sa-key.json")
ee.Initialize(credentials)

# INIT GOOGLE DRIVE API
drive_creds = service_account.Credentials.from_service_account_file(
    'sa-key.json', scopes=['https://www.googleapis.com/auth/drive']
)
drive = build('drive', 'v3', credentials=drive_creds)

def get_drive_folder_id():
    if EXPORT_FOLDER_ID:
        return EXPORT_FOLDER_ID

    query = f"name='{EXPORT_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder'"
    res = drive.files().list(q=query, fields="files(id)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": EXPORT_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder"
    }
    new_folder = drive.files().create(body=metadata, fields="id").execute()
    return new_folder["id"]

def cloud_mask(image):
    qa = image.select('QA60')
    cloud_bit_mask = 1 << 10
    cirrus_bit_mask = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit_mask).eq(0).And(qa.bitwiseAnd(cirrus_bit_mask).eq(0))
    return image.updateMask(mask).divide(10000)

def add_ndvi(image):
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
    return image.addBands(ndvi)

def zonal_stats(image, regions):
    def compute(f):
        stats = image.select('NDVI').reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=f.geometry(),
            scale=10,
            maxPixels=1e13
        )
        return f.set(stats).set("date", image.get("date"))
    return regions.map(compute)

def start_export_tasks(folder_id):
    # Telangana/Andhra Pradesh region
    region = ee.Geometry.Rectangle([77.0, 12.5, 84.8, 19.9])
    
    # Load administrative boundaries
    mandals = ee.FeatureCollection("projects/compact-marker-441912-a5/assets/new")
    districts = ee.FeatureCollection("projects/compact-marker-441912-a5/assets/TS_District_Boundary_33_FINAL")
    
    # Get last 7 days of Sentinel-2 data
    today = ee.Date(datetime.utcnow())
    start = today.advance(-7, "day")
    
    collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filterDate(start, today) \
        .filterBounds(region) \
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
        .map(cloud_mask) \
        .map(add_ndvi)
    
    # Get weekly composite
    weekly_ndvi = collection.select('NDVI').mean().set("date", today.format("YYYY-MM-dd"))
    
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
    
    tasks = []
    tasks.append(run_task(zonal_stats(weekly_ndvi, mandals), "Mandals_Sentinel2_NDVI"))
    tasks.append(run_task(zonal_stats(weekly_ndvi, districts), "Districts_Sentinel2_NDVI"))
    
    return tasks

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

def main():
    folder_id = get_drive_folder_id()
    print("Drive Folder ID:", folder_id)
    
    tasks = start_export_tasks(folder_id)
    wait_for_tasks(tasks)
    download_csvs(folder_id)
    
    print("Sentinel-2 NDVI export complete!")

if __name__ == "__main__":
    main()