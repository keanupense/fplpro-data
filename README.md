# FPL Pro odds feed

A tiny daily job that turns Premier League betting odds into the numbers the app's xPts model uses
(win / draw / loss probabilities, expected goals, Poisson goal rates, clean-sheet chances per FPL
fixture) and publishes them as `odds.json`. Python standard library only, no server, no cost.

This folder is prepared inside the app repository; it is meant to become its **own repository**
(the app reads `odds.json` from it). Nothing here runs from the app repository – GitHub only runs
workflows from a repository's root `.github/`.

| file | purpose |
|---|---|
| `fetch_odds.py` | guarded fetch (one paid request at most) → `odds.json`, books credits in `state.json` |
| `derive.py` | odds → vig-free probabilities, expected goals, λ home/away, P(clean sheet) (also used by `ML/odds.py` for training) |
| `state.json` | committed ledger `{month, credits_used}` |
| `odds.json` | output – derived numbers only, never bookmaker prices |
| `.github/workflows/odds.yml` | daily 05:30 UTC + manual run, `concurrency` so two runs never overlap |
| `test_fetch_odds.py`, `sample_odds_api.json` | offline tests (no key, no network): `python3 -m unittest -v` |

## Setup (once)

1. **Create the repository** on GitHub, e.g. `keanupense/fplpro-data`, and copy the *contents* of this
   folder to its root (so the workflow ends up at `.github/workflows/odds.yml`). It has to be
   **public** for the app to download `odds.json` without a token – it only ever contains derived
   probabilities, never the key or bookmaker prices.
2. **Add the key as a secret:** repository → Settings → Secrets and variables → Actions →
   *New repository secret*, name `ODDS_API_KEY`, value = your The Odds API key. Never put the key in a
   file, an issue or a commit; the script only reads it from the environment.
3. **Enable Actions:** repository → Actions → enable workflows. Settings → Actions → General →
   *Workflow permissions* → "Read and write permissions" (the job commits `odds.json` + `state.json`).
4. Optional first run: Actions → `odds` → *Run workflow*. The log ends with `ok: N fixtures, 2 credits`
   or a `skip: …` reason.
5. In the app, `OddsFeed.url` (`FPLPRO4/V2/Engine/XPtsService.swift`) must point to
   `https://raw.githubusercontent.com/<owner>/<repo>/main/odds.json`.

GitHub disables scheduled workflows after 60 days without repository activity. The job commits
whenever odds change, so this only happens in the summer break – re-enable the workflow in August.

## Why the free plan can never be exceeded or charged

- One request = `regions=uk` × `markets=h2h,totals` = **2 credits**. Daily ≈ 62 credits a month of the
  free plan's 500.
- The script stops before the paid request when
  1. there is no Premier League fixture in the next 8 days (free FPL `fixtures/` endpoint),
  2. its own ledger says this month's usage + 2 would exceed **200** credits,
  3. the free `/v4/sports/` endpoint reports fewer than **150** credits left on the account (or no
     quota header at all).
- No retries: every request is made exactly once; a failed paid request is still booked in the
  ledger (the commit step runs even when the fetch fails), and the run ends.
- `concurrency` makes overlapping runs (cron + manual) impossible.
- The free plan has no payment method attached, so there is nothing that could be billed: when the
  monthly quota is used up the API simply refuses further requests until the next month.

## `odds.json`

```json
{"format": 1, "updated": "2026-11-20T05:30:12Z", "source": "…",
 "fixtures": {"123": {"home": "ARS", "away": "CHE", "gw": 12, "kickoff": "2026-11-21T15:00:00Z",
                      "p_home": 0.5412, "p_draw": 0.2391, "p_away": 0.2197, "exp_goals": 2.6311,
                      "lambda_home": 1.6512, "lambda_away": 0.9799, "cs_home": 0.3753, "cs_away": 0.1918,
                      "books": 21, "updated": "2026-11-20T05:30:12Z"}}}
```

Keys are FPL fixture ids. Probabilities are the median over bookmakers of each bookmaker's normalised
(vig-free) 1X2 probabilities; `exp_goals` is the Poisson total that reproduces the vig-free over/under
probability of each half-goal line (median); `lambda_home + lambda_away = exp_goals` with the Poisson
model's P(home) − P(away) equal to the market's; `cs_home = exp(−lambda_away)`. Entries of fixtures
that have not kicked off are kept when a later run does not list them. The app ignores entries older
than 10 days and treats a missing file / fixture as "no odds" (the model handles missing values).
