"""
Derived match numbers from bookmaker odds (Python standard library only).

Used by fetch_odds.py (live feed for the app) AND by the app's training code (ML/odds.py, historical
football-data.co.uk prices) – both must produce the same numbers from the same kind of input.

Input per match:
  h2h:    list of (home, draw, away) decimal odds, one triple per bookmaker
  totals: list of (line, over, under) decimal odds, one per bookmaker and half-goal line (2.5, 3.5 …)

Output (all vig-free, nothing that could reconstruct a bookmaker's price):
  p_home, p_draw, p_away   median over bookmakers of the normalised implied probabilities (renormalised)
  exp_goals                expected total goals T: the Poisson mean whose P(total > line) equals the
                           vig-free over probability, median over bookmakers / lines
  lambda_home, lambda_away independent Poisson goal rates with lambda_home + lambda_away = T and
                           P(home win) - P(away win) of the Poisson model = p_home - p_away
  cs_home, cs_away         P(clean sheet) = exp(-lambda of the other side)
  books                    number of bookmakers with a valid 1X2 price
Without a totals price T is chosen so that the Poisson model's draw probability equals p_draw.
"""
import math
from statistics import median

MAX_GOALS = 12


def vig_free(odds):
    """Decimal odds of one market -> implied probabilities normalised to 1 (None if any price is invalid)."""
    try:
        inv = [1.0 / float(o) for o in odds]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if any(not math.isfinite(x) or x <= 0 or x >= 1 for x in inv):
        return None
    s = sum(inv)
    return [x / s for x in inv]


def poisson_pmf(lam):
    out, p = [], math.exp(-lam)
    for k in range(MAX_GOALS + 1):
        out.append(p)
        p *= lam / (k + 1)
    return out


def poisson_cdf(k, lam):
    return sum(poisson_pmf(lam)[: k + 1])


def match_probs(lh, la):
    """P(home win), P(draw), P(away win) for independent Poisson goals (0…MAX_GOALS each)."""
    ph, pa = poisson_pmf(lh), poisson_pmf(la)
    win = draw = loss = 0.0
    for i, a in enumerate(ph):
        for j, b in enumerate(pa):
            if i > j:
                win += a * b
            elif i == j:
                draw += a * b
            else:
                loss += a * b
    s = win + draw + loss
    return win / s, draw / s, loss / s


def _bisect(fn, lo, hi, target, increasing, iters=60):
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        v = fn(mid)
        if (v < target) == increasing:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def total_from_over(p_over, line):
    """Poisson mean T with P(total > line) = p_over (line = x.5)."""
    k = int(math.floor(line))
    p_over = min(max(p_over, 1e-4), 1 - 1e-4)
    return _bisect(lambda t: 1.0 - poisson_cdf(k, t), 0.05, 9.0, p_over, increasing=True)


def split_total(total, diff):
    """lambda_home in (0, total) with P(H) - P(A) = diff under independent Poisson goals."""
    def d(lh):
        w, _, l = match_probs(lh, total - lh)
        return w - l
    lh = _bisect(d, 1e-4, total - 1e-4, diff, increasing=True)
    return lh, total - lh


def total_from_draw(p_draw, diff):
    """T such that the Poisson model (split to match diff) gives P(draw) = p_draw (draw falls as T grows)."""
    def draw(t):
        lh, la = split_total(t, diff)
        return match_probs(lh, la)[1]
    return _bisect(draw, 0.3, 6.0, p_draw, increasing=False, iters=40)


def derive(h2h, totals):
    """See module doc. Returns None without a single valid 1X2 price."""
    probs = [p for p in (vig_free(t) for t in h2h) if p is not None]
    if not probs:
        return None
    ph, pd, pa = (median(p[i] for p in probs) for i in range(3))
    s = ph + pd + pa
    ph, pd, pa = ph / s, pd / s, pa / s
    ts = []
    for line, over, under in totals:
        try:
            line = float(line)
        except (TypeError, ValueError):
            continue
        if abs(line - math.floor(line) - 0.5) > 1e-9:
            continue   # only half-goal lines map exactly to a Poisson total
        p = vig_free((over, under))
        if p is not None:
            ts.append(total_from_over(p[0], line))
    total = median(ts) if ts else total_from_draw(pd, ph - pa)
    lh, la = split_total(total, ph - pa)
    return {
        "p_home": ph, "p_draw": pd, "p_away": pa, "exp_goals": total,
        "lambda_home": lh, "lambda_away": la, "cs_home": math.exp(-la), "cs_away": math.exp(-lh),
        "books": len(probs),
    }
