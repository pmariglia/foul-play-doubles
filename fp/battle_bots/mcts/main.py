import itertools
import logging
import random
from copy import deepcopy

from data.pkmn_sets import SmogonSets
from fp.battle import Battle
from config import FoulPlayConfig

from ..poke_engine_helpers import (
    get_payoff_matrix_from_mcts,
    battle_to_poke_engine_state,
)

logger = logging.getLogger(__name__)


class BattleBot(Battle):
    def __init__(self, *args, **kwargs):
        super(BattleBot, self).__init__(*args, **kwargs)

    def find_best_move(self):
        # this is where set prediction / filling in unknowns happens

        if self.team_preview:
            self.user.slot_a.active = self.user.reserve.pop(0)
            self.user.slot_b.active = self.user.reserve.pop(0)
            self.opponent.slot_a.active = self.opponent.reserve.pop(0)
            self.opponent.slot_b.active = self.opponent.reserve.pop(0)

        battles = prepare_battles(self)

        logger.info("Searching for a move using MCTS...")
        choice, win_percentage, num_iterations = get_payoff_matrix_from_mcts(
            battle_to_poke_engine_state(battle), FoulPlayConfig.search_time_ms
        )
        logger.info("Choice: {}, {}".format(choice, win_percentage))
        logger.info("Iterations: {}".format(num_iterations))

        if self.team_preview:
            self.user.reserve.insert(0, self.user.slot_a.active)
            self.user.reserve.insert(0, self.user.slot_b.active)
            self.user.slot_a.active = None
            self.user.slot_b.active = None
            self.opponent.reserve.insert(0, self.opponent.slot_a.active)
            self.opponent.reserve.insert(0, self.opponent.slot_b.active)
            self.opponent.slot_a.active = None
            self.opponent.slot_b.active = None

        return choice
