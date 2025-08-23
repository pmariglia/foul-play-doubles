import importlib
import json
import asyncio
import concurrent.futures
from copy import deepcopy
import logging

import constants
from config import FoulPlayConfig, SaveReplay
from data.pkmn_sets import SmogonSets
from fp.battle import LastUsedMove, Pokemon, Battle
from fp.battle_bots.helpers import format_decision
from fp.battle_modifier import async_update_battle
from fp.helpers import normalize_name

from fp.websocket_client import PSWebsocketClient

logger = logging.getLogger(__name__)


def battle_is_finished(battle_tag, msg):
    return (
        msg.startswith(">{}".format(battle_tag))
        and (constants.WIN_STRING in msg or constants.TIE_STRING in msg)
        and constants.CHAT_STRING not in msg
    )


def bo3_is_finished(bo3_tag, msg):
    return (
        msg.startswith(">{}".format(bo3_tag))
        and (constants.WIN_STRING in msg or constants.TIE_STRING in msg)
        and constants.CHAT_STRING not in msg
    )


def extract_battle_factory_tier_from_msg(msg):
    start = msg.find("Battle Factory Tier: ") + len("Battle Factory Tier: ")
    end = msg.find("</b>", start)
    tier_name = msg[start:end]

    return normalize_name(tier_name)


async def async_pick_move(battle):
    battle_copy = deepcopy(battle)
    if not battle_copy.team_preview:
        battle_copy.user.update_from_request_json(battle_copy.request_json)

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        choice_a, choice_b = await loop.run_in_executor(
            pool, battle_copy.find_best_move
        )
    battle.user.slot_a.last_selected_move = LastUsedMove(
        battle.user.slot_a.active.name,
        choice_a.split(",")[0],
        battle.turn,
    )
    battle.user.slot_b.last_selected_move = LastUsedMove(
        battle.user.slot_b.active.name,
        choice_b.split(",")[0],
        battle.turn,
    )
    decision_a = format_decision(battle_copy, battle_copy.user.slot_a, choice_a)
    decision_b = format_decision(battle_copy, battle_copy.user.slot_b, choice_b)
    return decision_a, decision_b


async def handle_team_preview(battle, ps_websocket_client):
    battle_copy = deepcopy(battle)
    battle_copy.user.slot_a.active = Pokemon.get_dummy()
    battle_copy.user.slot_b.active = Pokemon.get_dummy()
    battle_copy.opponent.slot_a.active = Pokemon.get_dummy()
    battle_copy.opponent.slot_b.active = Pokemon.get_dummy()
    battle_copy.team_preview = True

    best_move_a, best_move_b = await async_pick_move(battle_copy)
    logger.info("Best move A: {}".format(best_move_a))
    logger.info("Best move B: {}".format(best_move_b))

    # because we copied the battle before sending it in, we need to update the last selected move here
    pkmn_name_a = battle.user.find_pkmn_by_index(int(best_move_a.split()[1])).name
    pkmn_name_b = battle.user.find_pkmn_by_index(int(best_move_b.split()[1])).name
    logger.info(f"{pkmn_name_a=} {pkmn_name_b=}")
    battle.user.slot_a.last_selected_move = LastUsedMove(
        "teampreview", "switch {}".format(pkmn_name_a), battle.turn
    )
    battle.user.slot_b.last_selected_move = LastUsedMove(
        "teampreview", "switch {}".format(pkmn_name_b), battle.turn
    )

    choice_digit_a = int(best_move_a.split()[-1])
    choice_digit_b = int(best_move_b.split()[-1])

    # choose the other two pokemon with the highest average effectiveness
    # I don't think this is the best way to do it, will come back to this
    remaining_pkmn_by_effectiveness = sorted(
        [
            (
                p.name,
                p.index,
                sum(SmogonSets.raw_pkmn_sets[p.name]["effectiveness"].values())
                / len(SmogonSets.raw_pkmn_sets[p.name]["effectiveness"].values())
                if p.name in SmogonSets.raw_pkmn_sets
                and len(SmogonSets.raw_pkmn_sets[p.name]["effectiveness"]) > 0
                else 0,
            )
            for p in battle.user.reserve
            if p.index not in [choice_digit_a, choice_digit_b]
            and p.name not in constants.RESTRICTED_POKEMON
        ],
        key=lambda x: x[2],
        reverse=True,
    )

    additional_choices = []
    for pkmn in battle.user.reserve:
        if (
            pkmn.index not in [choice_digit_a, choice_digit_b]
            and pkmn.name in constants.RESTRICTED_POKEMON
        ):
            additional_choices.append((pkmn.name, pkmn.index, 0))

    while len(additional_choices) < 2:
        additional_choices.append(remaining_pkmn_by_effectiveness.pop(0))

    choice_digit_reserve_1 = additional_choices[0][1]
    choice_digit_reserve_2 = additional_choices[1][1]
    logger.info(
        "Chosen reserve pokemon: {}, {}".format(
            additional_choices[0][0], additional_choices[1][0]
        )
    )
    message = [
        "/team {}{}{}{}|{}".format(
            choice_digit_a,
            choice_digit_b,
            choice_digit_reserve_1,
            choice_digit_reserve_2,
            battle.rqid,
        )
    ]
    chosen_pkmn = [
        additional_choices[0][0],
        additional_choices[1][0],
        pkmn_name_a,
        pkmn_name_b,
    ]
    logger.info(
        "Chosen pokemon: {}, {}, {}, {}".format(
            additional_choices[0][0],
            additional_choices[1][0],
            pkmn_name_a,
            pkmn_name_b,
        )
    )
    for pkmn in battle.user.reserve:
        if pkmn.name not in chosen_pkmn:
            logger.info("Setting {} as fainted as it is not chosen".format(pkmn.name))
            pkmn.hp = 0
            pkmn.name = "none"  # poke-engine uses none to identify pokemon that are not in the battle

    await ps_websocket_client.send_message(battle.battle_tag, message)


async def get_battle_tag_and_opponent(ps_websocket_client: PSWebsocketClient):
    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split("|")
        first_msg = split_msg[0]
        if "battle" in first_msg:
            battle_tag = first_msg.replace(">", "").strip()
            user_name = split_msg[-1].replace("☆", "").replace("‽", "").strip()
            opponent_name = (
                split_msg[4].replace(user_name, "").replace("vs.", "").strip()
            )
            return battle_tag, opponent_name


async def start_battle_common(
    ps_websocket_client: PSWebsocketClient, pokemon_battle_type
):
    battle_tag, opponent_name = await get_battle_tag_and_opponent(ps_websocket_client)
    if FoulPlayConfig.log_to_file:
        FoulPlayConfig.file_log_handler.do_rollover(
            "{}_{}.log".format(battle_tag, opponent_name)
        )

    battle = importlib.import_module(
        "fp.battle_bots.{}.main".format(FoulPlayConfig.battle_bot_module)
    ).BattleBot(battle_tag)
    battle.opponent.account_name = opponent_name
    battle.generation = pokemon_battle_type[:4]

    # wait until the opponent's identifier is received. This will be `p1` or `p2`.
    #
    # e.g.
    # '>battle-gen9randombattle-44733
    # |player|p1|OpponentName|2|'
    while True:
        msg = await ps_websocket_client.receive_message()
        if "|player|" in msg and battle.opponent.account_name in msg:
            battle.opponent.name = msg.split("|")[2]
            battle.user.name = constants.ID_LOOKUP[battle.opponent.name]
            break

    return battle, msg


async def get_first_request_json(
    ps_websocket_client: PSWebsocketClient, battle: Battle
):
    while True:
        msg = await ps_websocket_client.receive_message()
        msg_split = msg.split("|")
        if msg_split[1].strip() == "request" and msg_split[2].strip():
            user_json = json.loads(msg_split[2].strip("'"))
            battle.request_json = user_json
            battle.user.initialize_first_turn_user_from_json(user_json)
            battle.rqid = user_json[constants.RQID]
            return


async def start_standard_battle(
    ps_websocket_client: PSWebsocketClient,
    pokemon_battle_type,
    first_battle,
):
    battle, msg = await start_battle_common(ps_websocket_client, pokemon_battle_type)
    battle.battle_type = constants.STANDARD_BATTLE

    while constants.START_TEAM_PREVIEW not in msg:
        msg = await ps_websocket_client.receive_message()

    opponent_showteam = [
        line
        for line in msg.split("\n")
        if line.startswith(f"|showteam|{battle.opponent.name}")
    ][0][13:]
    battle.opponent.from_packed_string(opponent_showteam)

    await get_first_request_json(ps_websocket_client, battle)
    battle.during_team_preview()

    if first_battle:
        SmogonSets.initialize(pokemon_battle_type, battle)

    SmogonSets.load_speed_ranges(battle)
    battle.user.reserve.insert(0, battle.user.slot_a.active)
    battle.user.reserve.insert(0, battle.user.slot_b.active)
    battle.user.slot_a.active = None
    battle.user.slot_b.active = None
    await handle_team_preview(battle, ps_websocket_client)
    return battle


async def start_battle(ps_websocket_client, pokemon_battle_type, first_battle):
    battle = await start_standard_battle(
        ps_websocket_client, pokemon_battle_type, first_battle
    )

    await ps_websocket_client.send_message(battle.battle_tag, ["/timer on"])

    return battle


async def pokemon_battle(
    ps_websocket_client, pokemon_battle_type, best_of_3_room_name, first_battle
):
    battle = await start_battle(ps_websocket_client, pokemon_battle_type, first_battle)
    while True:
        msg = await ps_websocket_client.receive_message()
        if battle_is_finished(battle.battle_tag, msg):
            if constants.WIN_STRING in msg:
                winner = msg.split(constants.WIN_STRING)[-1].split("\n")[0].strip()
            else:
                winner = None
            logger.info("Winner: {}".format(winner))
            if FoulPlayConfig.save_replay == SaveReplay.Always or (
                FoulPlayConfig.save_replay == SaveReplay.OnLoss
                and winner != FoulPlayConfig.username
            ):
                await ps_websocket_client.save_replay(battle.battle_tag)
            await ps_websocket_client.leave_battle(battle.battle_tag)
            SmogonSets.save_speed_ranges(battle)
            return winner, False
        elif bo3_is_finished(best_of_3_room_name, msg):
            if constants.WIN_STRING in msg:
                winner = msg.split(constants.WIN_STRING)[-1].split("\n")[0].strip()
            else:
                winner = None
            logger.info("Bo3 Winner: {}".format(winner))
            if FoulPlayConfig.save_replay == SaveReplay.Always or (
                FoulPlayConfig.save_replay == SaveReplay.OnLoss
                and winner != FoulPlayConfig.username
            ):
                await ps_websocket_client.save_replay(battle.battle_tag)
            await ps_websocket_client.leave_battle(battle.battle_tag)
            return winner, True
        else:
            action_required = await async_update_battle(battle, msg)
            if action_required and not battle.wait:
                best_move_a, best_move_b = await async_pick_move(battle)
                best_move = [f"/choose {best_move_a},{best_move_b}", str(battle.rqid)]
                await ps_websocket_client.send_message(battle.battle_tag, best_move)
