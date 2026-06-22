import json
from pathlib import Path
import os
import re

def get_records(file,FILTER_TITLE_IN,FILTER_TITLE_OFF,UNSPSC_CODES_IN,UNSPSC_CODES_OFF):
    '''Returns the filtered records (as list) from the given JSON file based on the provided FILTER and FILTER_OFF patterns.'''

    with open(file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    records = data['releases']
    filtered_records = []

    title_filter_in_pattern = re.compile(FILTER_TITLE_IN, re.IGNORECASE)
    title_filter_off_pattern = re.compile(FILTER_TITLE_OFF, re.IGNORECASE)

    for record in records:
            title = record.get('tender', {}).get('title', '')
            unspsc = record.get('tender', {}).get('items', [{}])[0].get('id', '')
            if title and title_filter_in_pattern.search(title) and not title_filter_off_pattern.search(title) and unspsc not in UNSPSC_CODES_OFF:
                filtered_records.append(record)
            elif unspsc and unspsc in UNSPSC_CODES_IN and unspsc not in UNSPSC_CODES_OFF and not title_filter_off_pattern.search(title):
                filtered_records.append(record)
    return filtered_records


def filter_records(folder, FILTER_TITLE_IN, FILTER_TITLE_OFF, UNSPSC_CODES_IN, UNSPSC_CODES_OFF):
    all_records = []
    for file in os.listdir(folder):
        if file.startswith('week') and file.endswith('.json'):
            file_path = os.path.join(folder, file)
            records = get_records(file_path, FILTER_TITLE_IN, FILTER_TITLE_OFF, UNSPSC_CODES_IN, UNSPSC_CODES_OFF)
            all_records.extend(records)
    seen_ocids = set()
    all_records = [r for r in all_records if not (r.get('ocid') in seen_ocids or seen_ocids.add(r.get('ocid')))]
    return all_records


def build_raw_json_db(folder, FILTER_TITLE_IN, FILTER_TITLE_OFF, UNSPSC_CODES_IN, UNSPSC_CODES_OFF):
    '''Builds a raw JSON database by filtering records from all JSON files in the specified folder based on the provided FILTER and FILTER_OFF patterns. 
        The filtered records are saved to a new JSON file named "filtered_records.json" in the same folder.'''
    records = filter_records(folder, FILTER_TITLE_IN, FILTER_TITLE_OFF, UNSPSC_CODES_IN, UNSPSC_CODES_OFF)
    output_file = Path(folder) / "filtered_records.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(records)} records to {output_file}")


if __name__ == "__main__":
    FILTER_TITLE_IN='gardiennage|agence de sécurité|agents de sécurité|services de sécurité|service de sécurité|personnel de sécurité|service de surveillance|patrouill'   #? Filter tenders on title
    FILTER_TITLE_OFF='incendie|Incendie'                                                                                                                                #? Filter off title  
    UNSPSC_CODES_IN = [
        '92000000',   #Services de défense nationale, d'ordre public, de secours et de sécurité
        '92100000',   #Sécurité et ordre publics
        '92120000',   #Sécurité des personnes et des biens
        '92121500',   #Services de gardiennage
        '92121502',   #Services de protection contre le cambriolage
        '92121504',   #Services de gardiennage
    ]
    UNSPSC_CODES_OFF = [
    ]

    build_raw_json_db(Path("DATA"), FILTER_TITLE_IN, FILTER_TITLE_OFF, UNSPSC_CODES_IN, UNSPSC_CODES_OFF)