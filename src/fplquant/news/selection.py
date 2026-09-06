"""What the news says about being *picked*, as opposed to being fit.

Everywhere else in this codebase a player's news reaches the model as one
number: `chance_of_playing`, applied as a gate on expected points. That number
answers the question FPL asks — *will he play?* — and the engine spends it
answering a different one: *will he start?* For a fully fit player the two are
the same question and this module does nothing at all. For a player carrying a
knock they come apart, and they come apart in a direction that matters.

A manager with a doubtful player has a third option between playing him and
leaving him out, and it is the one they usually take: name him on the bench and
bring him on if the game needs it. So a 75% chance of *featuring* is not a 75%
chance of *starting* — it is a decent chance of half an hour. Reading FPL's
percentage as a start probability therefore overrates exactly the players a
manager is being careful with, which is a systematic error rather than a noisy
one, and it lands on the ownable end of the pool: Caicedo, Maddison, Mount and
Bruno Guimarães were all carrying one on 2026-09-06.

The correction is a single monotone transform of the availability the engine
already holds, which is what keeps it honest:

    start_gate(a) = a * (1 - SELECTION_PENALTY * (1 - a))

Three properties are worth stating because they are what make it safe to run.
It is **inert at the ends** — a player at 1.0 keeps 1.0 and a player at 0.0
stays at 0.0, so the ~97% of the pool with nothing wrong with them are
untouched, bit for bit. It is **monotone**, so it can never reorder two players
the availability number already ordered. And it takes **no new input**: there
is no second dict to plumb through the horizon and no way for it to disagree
with the availability layer, because it is a function of that layer's own
output.

Being a function of availability also gives it the right behaviour over time
for free. `fplquant.news.availability` already recovers a doubt toward the
ceiling as the weeks pass, so the selection discount relaxes on exactly the
same schedule and lapses when the doubt does. A player projected back to 0.9 in
three weeks' time carries a 0.95 selection factor then, not the 0.875 he
carries this Saturday.

**Why this is not double-counting**, which is the objection every other layer
here is careful about: the fitness gate prices *whether he is involved*, and
this prices *how he is involved given that he is*. They are different events,
multiplied, and the second is conditional on the first. What would be
double-counting is applying this on top of an estimate that already averages in
benched weeks — which is exactly why it is not applied in
`fplquant.form.fixtures`, whose base is an unconditional EWMA of gameweek
points with no normalisation to absorb it. It is applied in
`fplquant.engine.minutes`, where start probability is an explicit quantity and
where a club still fields eleven players: the probability a doubtful player
gives up is handed to his teammates rather than deleted, which is what actually
happens when a manager decides to be careful with somebody.
"""

from fplquant.config import settings

# How much more a doubt discounts *starting* than it discounts *featuring*.
# At 0.5 the familiar grades read as: 75% to feature is 65.6% to start, 50% is
# 37.5%, 25% is 15.6%.
#
# This is a prior, not a fitted value, and it cannot honestly be presented as
# anything else yet: the archived seasons carry no injury news at all —
# `fplquant.backtest.hydrate` sets every player's status to available — so
# there is nothing historical to calibrate against, and `player_snapshots`,
# which does archive status day by day, only began collecting on 2026-08-31.
# Half is chosen as the deliberately conservative reading of a real effect
# rather than a measured one: it says a manager being careful with a player is
# somewhat more likely to bench him than to leave him out, without pretending
# to know how much more. Once a season of snapshots exists this is the first
# constant here that should be fitted rather than assumed.
SELECTION_PENALTY = 0.5


def start_gate(availability: float) -> float:
    """The share of a player's start probability that survives his news.

    Returns `availability` unchanged at 0.0 and 1.0, and below it in between.
    Feed this to the engine's start-probability normalisation in place of the
    bare availability; keep the bare number for anything that means *fitness*,
    including the bench-appearance term, which moves the other way — a doubtful
    player who does not start is a substitute, not an absentee.
    """
    if not settings.news_selection_feeds_the_model:
        return availability
    if availability <= 0.0 or availability >= 1.0:
        return availability
    return availability * (1.0 - SELECTION_PENALTY * (1.0 - availability))
