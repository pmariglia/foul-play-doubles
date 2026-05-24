import logging
import random
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from itertools import combinations

from poke_engine.poke_engine import TeamPreviewFilters

import constants
from data.pkmn_sets import SmogonSets
from fp.battle import Battle, Pokemon, BattleData, Battler
from config import FoulPlayConfig, TeamLeads

from poke_engine import (
    State as PokeEngineState,
    monte_carlo_tree_search,
    MctsResult,
    monte_carlo_tree_search_team_preview,
)

from fp.search.poke_engine_helpers import battle_to_poke_engine_state

logger = logging.getLogger(__name__)


def team_preview_shuffle(battle):
    """
    poke-engine needs pkmn in the active slots all the time, even during team preview
    """
    battle.user.slot_a.active = battle.user.reserve.pop(0)
    battle.user.slot_b.active = battle.user.reserve.pop(0)
    battle.opponent.slot_a.active = battle.opponent.reserve.pop(0)
    battle.opponent.slot_b.active = battle.opponent.reserve.pop(0)


def sample_pkmn_to_remove(pkmn_list: list[Pokemon]):
    # sample a non-restricted pokemon to remove if available, otherwise sample any pokemon
    pkmn_to_sample_from = (
        [
            p
            for p in pkmn_list
            if not p.revealed
            and p.can_mega  # always remove megas first - a mega would've been sampled before this
        ]
        or [
            p
            for p in pkmn_list
            if p.name not in constants.RESTRICTED_POKEMON
            and not p.revealed
            and not p.can_mega
        ]
        or [p for p in pkmn_list if not p.revealed]
    )
    return random.choice(pkmn_to_sample_from)


def sample_mega(pkmn_list: list[Pokemon]) -> Pokemon | None:
    # if no mega has been revealed, and there are still pokemon to sample
    # sample a mega-pkmn
    revealed_pkmn = [p for p in pkmn_list if p.revealed]
    revealed_mega_pkmn = [p for p in revealed_pkmn if p.can_mega or p.is_mega]
    if revealed_mega_pkmn:
        return None

    un_revealed_megas = [p for p in pkmn_list if not p.revealed and p.can_mega]
    if un_revealed_megas:
        return random.choice(un_revealed_megas)

    return None


def sample_unrevealed_pkmn(battle: Battle, num_teams: int) -> list[(Battle, float)]:
    battle = deepcopy(battle)
    num_reserves = 2

    battles = []
    for i in range(num_teams):
        battle_copy = deepcopy(battle)
        sampled_mega = sample_mega(
            battle_copy.opponent.reserve
            + [battle_copy.opponent.slot_a.active, battle_copy.opponent.slot_b.active]
        )
        if sampled_mega and battle_copy.opponent.num_revealed_pkmn() < 4:
            sampled_mega.revealed = True
        while len(battle_copy.opponent.reserve) > num_reserves:
            pkmn = sample_pkmn_to_remove(battle_copy.opponent.reserve)
            battle_copy.opponent.reserve.remove(pkmn)

        assert len(battle_copy.opponent.reserve) == 2
        populate_spreads(battle_copy, i)
        battle_copy.opponent.slot_a.lock_moves()
        battle_copy.opponent.slot_b.lock_moves()
        battles.append((battle_copy, 1 / num_teams))

    return battles


def get_battles_for_team_preview(battle: Battle, num_battles: int) -> list[Battle]:
    battles = []
    for i in range(num_battles):
        battle_copy = deepcopy(battle)
        team_preview_shuffle(battle_copy)
        populate_spreads(battle_copy, i)
        battle_copy.opponent.slot_a.lock_moves()
        battle_copy.opponent.slot_b.lock_moves()
        battles.append((battle_copy, 1 / num_battles))
    return battles


def populate_spreads(battle: Battle, index: int):
    logger.info("Battle {}".format(index))
    for pkmn in [
        battle.opponent.slot_a.active,
        battle.opponent.slot_b.active,
    ] + battle.opponent.reserve:
        if pkmn.hp <= 0:
            continue

        pkmn_spread = SmogonSets.get_random_spread(pkmn)
        if pkmn_spread is None:
            logger.warning("\tNo spread found for {}".format(pkmn.name))
        else:
            pkmn.set_spread(pkmn_spread.nature, pkmn_spread.evs)
            logger.info(
                "\tPredicted Set: {} {} for {}".format(
                    pkmn_spread.nature.ljust(7), str(pkmn.evs).ljust(25), pkmn.name
                )
            )


def select_move_from_mcts_results(mcts_results: list[(MctsResult, float, int)]) -> str:
    final_policy = {}
    for mcts_result, sample_chance, index in mcts_results:
        this_policy = max(mcts_result.side_one, key=lambda x: x.visits)
        logger.info(
            "Policy {}: {} visited {}% avg_score={} sample_chance_multiplier={}".format(
                index,
                this_policy.move_choice,
                round(100 * this_policy.visits / mcts_result.total_visits, 2),
                round(this_policy.total_score / this_policy.visits, 3),
                round(sample_chance, 3),
            )
        )
        for s1_option in mcts_result.side_one:
            final_policy[s1_option.move_choice] = final_policy.get(
                s1_option.move_choice, 0
            ) + (sample_chance * (s1_option.visits / mcts_result.total_visits))

    final_policy = sorted(final_policy.items(), key=lambda x: x[1], reverse=True)

    logger.info("Top 5 moves from MCTS results:")
    for i, policy in enumerate(final_policy[:5]):
        logger.info(f"\t{round(policy[1] * 100, 3)}%: {policy[0]}")

    # Consider all moves that are close to the best move
    highest_percentage = final_policy[0][1]
    final_policy = [i for i in final_policy if i[1] >= highest_percentage * 0.75]
    logger.info("Considered Choices:")
    for i, policy in enumerate(final_policy):
        logger.info(f"\t{round(policy[1] * 100, 3)}%: {policy[0]}")

    choice = random.choices(final_policy, weights=[p[1] for p in final_policy])[0]
    return choice[0]


def get_result_from_mcts(state: str, search_time_ms: int, index: int) -> MctsResult:
    logger.debug("Calling with {} state: {}".format(index, state))
    poke_engine_state = PokeEngineState.from_string(state)
    res = monte_carlo_tree_search(poke_engine_state, search_time_ms, threads=2)
    logger.info("Iterations {}: {}".format(index, res.total_visits))
    return res


def get_default_team_preview_options(
    pkmn_list: list[Pokemon],
) -> list[(int, int, int, int)]:
    pokemon_indices = [0, 1, 2, 3, 4, 5]
    all_teams = [tuple(i) for i in combinations(pokemon_indices, 4)]

    mega_count = sum(1 for p in pkmn_list if p.can_mega)
    if 2 <= mega_count <= 3:
        mega_indices = {i for i, p in enumerate(pkmn_list) if p.can_mega}
        all_teams = [
            team for team in all_teams if sum(1 for i in team if i in mega_indices) == 1
        ]

    result = []
    for team in all_teams:
        all_leads = combinations(team, 2)
        for lead in all_leads:
            team_with_leads = lead + tuple([i for i in team if i not in lead])
            result.append(team_with_leads)
    return result


def opponent_team_preview_side_filter(
    battle: Battle, previous_battle_data: BattleData
) -> list[(str, str, str, str)]:
    def pkmn_to_indices(
        battler: Battler, tp_selections: list[(str, str, str, str)]
    ) -> list[(int, int, int, int)]:
        name_to_index = {p.name: index for (index, p) in enumerate(battler.reserve)}
        return [
            tuple(name_to_index[name] for name in selection)
            for selection in tp_selections
        ]

    pkmn_choices = tuple(previous_battle_data.opponent_leads)
    for pkmn in list(previous_battle_data.opponent_picks):
        pkmn_choices += (pkmn,)

    if len(pkmn_choices) == 4:
        return pkmn_to_indices(battle.opponent, [pkmn_choices])

    remaining_pkmn = [
        p.name for p in battle.opponent.reserve if p.name not in pkmn_choices
    ]
    slots_needed = 4 - len(pkmn_choices)

    return pkmn_to_indices(
        battle.opponent,
        [pkmn_choices + combo for combo in combinations(remaining_pkmn, slots_needed)],
    )


def get_teampreview_filters(
    battle: Battle, our_side_filters: list[(str, str, str, str)], parallelism: int
) -> list[TeamPreviewFilters]:
    previous_battle_data = battle.previous_battle_data

    result = []
    for pbd in previous_battle_data:
        side_two_filter = opponent_team_preview_side_filter(battle, pbd)
        result.append(
            TeamPreviewFilters(
                side_one=our_side_filters,
                side_two=side_two_filter,
            )
        )
        if not pbd.win:
            result.append(
                TeamPreviewFilters(
                    side_one=our_side_filters,
                    side_two=side_two_filter,
                )
            )

    result = result[-parallelism:]
    while len(result) < parallelism:
        result.append(
            TeamPreviewFilters(
                side_one=our_side_filters,
                side_two=get_default_team_preview_options(battle.opponent.reserve),
            )
        )

    return result


def get_result_from_teampreview_mcts(
    state: str, search_time_ms: int, index: int, team_preview_filter: TeamPreviewFilters
) -> MctsResult:
    logger.info(f"Choices ({index=}): {team_preview_filter.debug_print()}")
    logger.debug("Calling with {} state: {}".format(index, state))
    poke_engine_state = PokeEngineState.from_string(state)
    res = monte_carlo_tree_search_team_preview(
        poke_engine_state, team_preview_filter, search_time_ms, threads=2
    )
    logger.info("Iterations {}: {}".format(index, res.total_visits))
    return res


def find_best_move(battle):
    num_teams = 4
    parallelism = FoulPlayConfig.parallelism
    search_time_per_battle = FoulPlayConfig.search_time_ms
    battles = sample_unrevealed_pkmn(battle, num_teams)

    num_battles = len(battles)

    logger.info("Searching for a move using MCTS...")
    logger.info(
        "Sampling {} battles at {}ms each".format(num_battles, search_time_per_battle)
    )

    with ProcessPoolExecutor(max_workers=parallelism) as executor:
        futures = []
        for index, (b, chance) in enumerate(battles):
            fut = executor.submit(
                get_result_from_mcts,
                battle_to_poke_engine_state(b).to_string(),
                search_time_per_battle,
                index,
            )
            futures.append((fut, chance, index))

    mcts_results = [(fut.result(), chance, index) for (fut, chance, index) in futures]

    choice = select_move_from_mcts_results(mcts_results)
    logger.info("Choice: {}".format(choice))

    return choice


def find_best_move_teampreview(battle):
    parallelism = FoulPlayConfig.parallelism // 2
    search_time_per_battle = min(40_000, FoulPlayConfig.search_time_ms * 2)

    battles = get_battles_for_team_preview(battle, parallelism)

    our_side_filter = TeamLeads.team_lead_indices or get_default_team_preview_options(
        battle.user.reserve
    )
    team_preview_filters = get_teampreview_filters(battle, our_side_filter, parallelism)

    num_battles = len(battles)
    logger.info("Searching for a move using MCTS...")
    logger.info(
        "Sampling {} battles at {}ms each".format(num_battles, search_time_per_battle)
    )

    with ProcessPoolExecutor(max_workers=parallelism) as executor:
        futures = []
        for index, ((b, chance), tp_filter) in enumerate(
            zip(battles, team_preview_filters)
        ):
            fut = executor.submit(
                get_result_from_teampreview_mcts,
                battle_to_poke_engine_state(b).to_string(),
                search_time_per_battle,
                index,
                tp_filter,
            )
            futures.append((fut, chance, index))

    mcts_results = [(fut.result(), chance, index) for (fut, chance, index) in futures]

    choice = select_move_from_mcts_results(mcts_results)
    logger.info("Choice: {}".format(choice))

    return choice
