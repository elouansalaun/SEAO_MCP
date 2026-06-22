import requests
import xml.etree.ElementTree as ET
import os
from datetime import datetime
import re
from pathlib import Path

# Configuration
#FEED_URL = "https://open.canada.ca/data/en/feeds/dataset/d23b2e02-085d-43e5-9e6e-e1d558ebfdd5.atom"
#NAMESPACE = {'atom': 'http://www.w3.org/2005/Atom'}



DOWNLOAD_DIR = Path("DATA")

def fetch_and_download(DOWNLOAD_DIR=str(DOWNLOAD_DIR),
                       FEED_URL="https://open.canada.ca/data/en/feeds/dataset/d23b2e02-085d-43e5-9e6e-e1d558ebfdd5.atom",
                       NAMESPACE={'atom': 'http://www.w3.org/2005/Atom'}):
    ''' Fetch the Atom feed, parse it, and download weekly JSON files.'''
    
    # Create download directory if it doesn't exist
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    print(f"Fetching feed from: {FEED_URL}")
    try:
        response = requests.get(FEED_URL)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching feed: {e}")
        return

    # Parse XML
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        return

    # Iterate through entries
    for entry in root.findall('atom:entry', NAMESPACE):
        title_elem = entry.find('atom:title', NAMESPACE)
        if title_elem is None:
            continue

        title = title_elem.text
        
        # Check condition: starts with 'week_XXXX' (where XXXX >= 2020) and ends with '.json'
        match = re.match(r'^week_(\d{4})', title) if title else None
        if match and int(match.group(1)) >= 2020 and title.endswith('.json'):
            #print(f"Found matching entry: {title}")
            
            # Check if file exists and compare timestamps
            filepath = os.path.join(DOWNLOAD_DIR, title)
            updated_elem = entry.find('atom:updated', NAMESPACE)
            
            if os.path.exists(filepath) and updated_elem is not None:
                try:
                    # Parse Atom feed timestamp (ISO 8601)
                    # Use replace to handle 'Z' if present, though feed has +00:00
                    remote_dt = datetime.fromisoformat(updated_elem.text.replace('Z', '+00:00'))
                    remote_ts = remote_dt.timestamp()
                    local_ts = os.path.getmtime(filepath)
                    
                    # If remote timestamp is not newer than local, skip download
                    if remote_ts <= local_ts:
                        #print(f" - Local file is already up to date or newer. Skipping.")
                        #print("-"*150)
                        continue
                    else:
                        continue
                        #print(f" - Remote file is newer. Proceeding to overwrite.")
                except ValueError as e:
                    print(f" - Date parsing error: {e}. Downloading to be safe.")

            # Find the resource API link (rel="enclosure")
            # The feed provides an API link to details which contains the text download URL
            enclosure_link = entry.find("atom:link[@rel='enclosure']", NAMESPACE)
            
            if enclosure_link is not None:
                api_url = enclosure_link.attrib.get('href')
                process_resource_api(api_url, title, DOWNLOAD_DIR)
            else:
                print(f" - No API link found for {title}")
    
    files= os.listdir(DOWNLOAD_DIR)
    files = [f for f in files if f.startswith('week_') and f.endswith('.json')]
    print(f" - Total weeks in download directory: {len(files)}")
    print("Feed processing complete.")



def process_resource_api(api_url, filename, DOWNLOAD_DIR):
    ''' Process the CKAN resource API to get the actual download URL and download the file.'''
    try:
        resp = requests.get(api_url)
        resp.raise_for_status()
        data = resp.json()
        # Extract the actual download URL from the CKAN API response
        if data.get('success') and 'result' in data:
            real_download_url = data['result'].get('url')
            if real_download_url:
                download_file(real_download_url, filename, DOWNLOAD_DIR)
            else:
                print(" - 'url' field not found in API response")
        else:
            print(" - API request was not successful")
    except Exception as e:
        print(f" - Error processing API: {e}")



def download_file(url, filename, DOWNLOAD_DIR=str(DOWNLOAD_DIR)):
    ''' Download a file from a URL to the specified download directory.'''
    
    # Check if file already exists
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        pass

    # File download
    print(f" - Downloading file : {filepath.replace(DOWNLOAD_DIR + os.sep, '')}")
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(filepath, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):     # Ensure RAM efficiency
                    f.write(chunk)
    except Exception as e:
        print(f" - Error downloading file: {e}")
   
        

if __name__ == "__main__":
    fetch_and_download()
