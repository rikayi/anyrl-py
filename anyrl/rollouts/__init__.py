"""
Various ways to gather trajectories in RL environments.
"""

from .rollout import Rollout
from .rollers import Roller, BasicRoller
from .list import mean_total_reward