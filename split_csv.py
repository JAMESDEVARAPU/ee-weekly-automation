import pandas as pd
import os

def split_csv(file_path, max_size_mb=24):
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # Read CSV
    df = pd.read_csv(file_path)
    
    # Get file info
    file_size = os.path.getsize(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    dir_name = os.path.dirname(file_path)
    
    print(f"File: {base_name}, Size: {file_size / (1024*1024):.1f}MB")
    
    if file_size <= max_size_bytes:
        print("File already under 24MB")
        return
    
    # Calculate rows per chunk
    rows_per_chunk = int(len(df) * max_size_bytes / file_size)
    
    # Split and save
    for i, chunk in enumerate(pd.read_csv(file_path, chunksize=rows_per_chunk)):
        chunk_file = os.path.join(dir_name, f"{base_name}_part{i+1}.csv")
        chunk.to_csv(chunk_file, index=False)
        print(f"Created: {chunk_file} ({len(chunk)} rows)")

# Split both files
files = [
    r"C:\Users\james\Downloads\ee-weekly-automation\data\Mandals_AllVars_Last2Months_Daily (1).csv",
    r"C:\Users\james\Downloads\ee-weekly-automation\data\Mandals_AllVars_Last2Months_Daily.csv"
]

for file_path in files:
    if os.path.exists(file_path):
        split_csv(file_path)
    else:
        print(f"File not found: {file_path}")