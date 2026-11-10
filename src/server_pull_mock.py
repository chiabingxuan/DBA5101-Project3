import random
from operator import index

ARMS = range(24)
VISITORS_PER_PULL = 100
MAX_PULLS = 1_000_000

# Artificial normal distributions for testing only: (mean sign-ups, std dev)
ARM_DISTRIBUTIONS = {
    arm: (30 + arm, 5)
    for arm in ARMS
}

_accounts = {}
_rng = random.Random(42)


class UncertainPullError(RuntimeError):
    """Included for API compatibility; this mock does not create uncertain pulls."""


def configure(base_url):
    """Accepts the same setup call as the real API; makes no network request."""
    if not isinstance(base_url, str):
        raise ValueError("base_url must be a string.")


def _get_account(username):
    if username not in _accounts:
        _accounts[username] = {"PullsUsed": 0, "NetReward": 0}
    return _accounts[username]


def status(username, password, *, timeout=10.0):
    account = _get_account(username)
    return {
        "PullsUsed": account["PullsUsed"],
        "NetReward": account["NetReward"],
        "MaxPulls": MAX_PULLS,
        "Open": account["PullsUsed"] < MAX_PULLS,
    }


def pull(
    username,
    password,
    arm,
    *,
    timeout=10.0,
    request_id=None,
    expected_pull=None,
):
    try:
        arm_id = index(arm)
    except TypeError:
        raise ValueError("arm must be an integer from 0 to 23.") from None

    if isinstance(arm, bool) or arm_id not in ARMS:
        raise ValueError("arm must be an integer from 0 to 23.")

    account = _get_account(username)
    pull_number = account["PullsUsed"]

    if expected_pull is not None and expected_pull != pull_number:
        raise RuntimeError(
            f"Expected simulated pull {expected_pull}, "
            f"but the next pull is {pull_number}."
        )

    if pull_number >= MAX_PULLS:
        raise RuntimeError("The mock account has used all available pulls.")

    mean, std_dev = ARM_DISTRIBUTIONS[arm_id]
    reward = round(_rng.gauss(mean, std_dev))
    reward = max(0, min(VISITORS_PER_PULL, reward))

    account["PullsUsed"] += 1
    account["NetReward"] += reward

    return {
        "Arm": str(arm_id),
        "NetReward": account["NetReward"],
        "Pull": pull_number,  # Zero-based, matching the real API
        "Reward": reward,
    }