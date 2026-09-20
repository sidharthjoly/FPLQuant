"""Reading binaries back out of a solved program."""

import pulp

from fplquant.optimizer.solution import is_set


def test_a_binary_just_short_of_one_counts_as_set() -> None:
    """The defect this exists to prevent, and it is not hypothetical: HiGHS
    returns 0.9999999999 where CBC happens to return 1.0, and an exact
    comparison silently dropped seven players out of a fifteen-man squad the
    solver had called optimal."""
    variable = pulp.LpVariable("pick", cat="Binary")
    variable.varValue = 0.9999999999
    assert is_set(variable)


def test_a_binary_just_above_zero_does_not() -> None:
    variable = pulp.LpVariable("pick", cat="Binary")
    variable.varValue = 1e-9
    assert not is_set(variable)


def test_an_unsolved_variable_is_not_set() -> None:
    assert not is_set(pulp.LpVariable("pick", cat="Binary"))
