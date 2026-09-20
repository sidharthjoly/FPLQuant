"""Reading a solved integer program back out.

One function, because getting it wrong is silent. A binary the solver
considers 1 is a float, and comparing it to 1 exactly works right up until the
run where it doesn't.
"""

import pulp


def is_set(variable: pulp.LpVariable) -> bool:
    """Whether a binary came back from the solver as 1.

    CBC returns floats, and a variable the solver considers 1 can come back as
    0.9999999998, so this compares against a midpoint rather than to 1.

    The exact comparison this replaces does not fail loudly. A dropped
    variable means a squad that is short a player, and the caller has already
    checked the solver said "Optimal" — so the program looks like it solved
    and simply hands back fourteen names. Measured against HiGHS, which
    returns 0.9999999999 where CBC happens to return 1.0, `optimize_squad`
    returned eight players for a problem it had solved perfectly.
    """
    value = variable.value()
    return value is not None and value > 0.5
