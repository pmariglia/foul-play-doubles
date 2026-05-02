# Running this will re-create custom set data for foulplay to use
# run with the Makefile:
# `make create_set_data FOLDERS="folder1 folder2 <...>"
import argparse
import json
import os

from fp.battle import Pokemon
from teams.team_converter import export_to_dict

OUT_DIR = "data/pkmn_sets_cache"
os.makedirs(OUT_DIR, exist_ok=True)
OUT_FILE = os.path.join(OUT_DIR, "custom_set_data.json")

parser = argparse.ArgumentParser(description="Process folders")
parser.add_argument("folders", nargs="+", help="One or more folder paths to process")

args = parser.parse_args()


def try_parse_int(value, default=None):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def is_valid(pkmn_dict: dict) -> bool:
    if len(pkmn_dict["moves"]) != 4 and pkmn_dict["species"] != "ditto":
        print(f"Invalid {pkmn_dict['species']}: '{pkmn_dict['moves']=}'")
        return False

    if not pkmn_dict["ability"] or not pkmn_dict["item"]:
        print(
            f"Invalid {pkmn_dict['species']}: '{pkmn_dict['ability']=}', '{pkmn_dict['item']=}'"
        )
        return False

    if not pkmn_dict["nature"]:
        print(f"Invalid Nature for {pkmn_dict['species']}: '{pkmn_dict['nature']}'")
        return False

    for k, v in pkmn_dict["evs"].items():
        if k not in {"hp", "atk", "def", "spa", "spd", "spe"}:
            print(f"Invalid EV key: '{k}'")
            return False
        int_ev = try_parse_int(v)
        if v != "" and not int_ev:
            print(f"Invalid EV val: '{v}'")
            return False

    return True


def convert_mega(team: list[dict]):
    for pkmn_dict in team:
        pkmn = Pokemon(pkmn_dict["species"], 50)
        pkmn.item = pkmn_dict["item"]
        if pkmn.get_mega() is not None:
            pkmn_dict["species"] = pkmn.get_mega()


result = {}
pokemon_parsed = 0
for folder in args.folders:
    for file in os.listdir(folder):
        full_path = os.path.join(folder, file)
        with open(full_path, "r") as f:
            valid = True
            content = f.read()
            team_dict = export_to_dict(content)
            convert_mega(team_dict)
            for pkmn_dict in team_dict:
                if pkmn_dict["species"] not in result:
                    result[pkmn_dict["species"]] = []
                if is_valid(pkmn_dict):
                    result[pkmn_dict["species"]].append(pkmn_dict)
                    pokemon_parsed += 1
                else:
                    valid = False
            if not valid:
                print(f"Invalid: {file}, removing...")
                os.remove(full_path)

with open(OUT_FILE, "w") as f:
    json.dump(result, f, indent=2)

print(f"Done parsing {pokemon_parsed} pokemon")
