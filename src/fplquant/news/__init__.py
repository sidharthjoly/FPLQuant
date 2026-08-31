"""What the world knows about a player that their match record does not.

Every other signal in this codebase is derived from things that have already
happened: minutes played, goals scored, prices moved. News is the one input
that is about the future — a club saying a player will be back on the 10th, a
ban that expires on a known date, a knock that will have cleared by the time
the next-but-one fixture arrives. None of that is recoverable from history, and
all of it changes what a squad should look like.

The layer has three parts, deliberately separable so a second source can be
added without touching the model:

- `parse` turns one published news string into a structured `PlayerNews` —
  category, condition, and a return date where one is stated.
- `sources` fetches those items. `FPLPlayerNewsSource` is the only one today
  and reads what the ingest already stores on `Player`; it costs nothing extra
  and it is the source FPL itself uses to set `chance_of_playing_next_round`.
- `availability` turns them into the number the engine consumes: how likely a
  player is to be fit and eligible **for each gameweek in the horizon**, rather
  than one number reused for all of them.

That last part is the whole point, and it is worth being precise about why it
is not double-counting. `chance_of_playing_next_round` already prices the news
for the next round — the percentage *is* the press-conference summary — so
reading the same text again and applying a second discount would charge the
same evidence twice, the mistake `lineup.starts` and `form.fixtures` both
warn about. The one thing the percentage structurally cannot carry is time: it
is by construction a statement about one round, and the projection applies it
unchanged to five. A player out until the 10th is not out in November, and a
player carrying a knock this week is not carrying it a month from now.

So the contract this layer holds itself to, enforced in `availability` and
asserted in the tests, is:

    availability[events[0]][player] == form.fixtures.chance_of_playing(player)

Next-round availability comes out bit-identical to what FPL published. The
layer only ever has something to say about events 2..N, and where the news
carries no time information it says nothing at all and every event keeps the
published number.
"""
