# Running this will re-create custom set data for foulplay to use
# run with the Makefile:
# `make create_set_data FOLDERS="folder1 folder2 <...>"
import argparse
import json
import os

from teams.team_converter import export_to_dict

OUT_DIR = "data/pkmn_sets_cache"
os.makedirs(OUT_DIR, exist_ok=True)
OUT_FILE = os.path.join(OUT_DIR, "custom_set_data.json")

parser = argparse.ArgumentParser(description="Process folders")
parser.add_argument(
    "folders",
    nargs="+",
    help="One or more folder paths to process"
)

args = parser.parse_args()

result = {}
pokemon_parsed = 0
for folder in args.folders:
    for file in os.listdir(folder):
        full_path = os.path.join(folder, file)
        with open(full_path, "r") as f:
            content = f.read()
            team_dict = export_to_dict(content)
            for pkmn_dict in team_dict:
                if pkmn_dict["species"] not in result:
                    result[pkmn_dict["species"]] = []
                result[pkmn_dict["species"]].append(pkmn_dict)
                pokemon_parsed += 1

with open(OUT_FILE, "w") as f:
    json.dump(result, f, indent=2)

print(f"Done parsing {pokemon_parsed} pokemon")
