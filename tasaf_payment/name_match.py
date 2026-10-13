import re
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher

METHOD = 'token-v1'

VERIFIED = 'VERIFIED'
MANUAL = 'MANUAL'
FAILED = 'FAILED'

DEFAULT_THRESHOLDS = {
    'verified_min': 80,
    'manual_min': 60,
    'token_min_ratio': 0.80,
    'initial_credit': 0.5,
    'min_tokens_matched': 2,
    'require_surname': True,
    'surname_min_ratio': 0.90,
}

TITLES = {'MR', 'MRS', 'MS', 'MISS', 'DR', 'BI', 'BW', 'BIBI', 'BWANA'}


@dataclass
class MatchResult:
    decision: str
    reason: str
    score: int
    matched: list = field(default_factory=list)
    member: str = None
    method: str = METHOD
    thresholds: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


def thresholds(overrides=None):
    return {**DEFAULT_THRESHOLDS, **(overrides or {})}


def normalise(name):
    if not name:
        return []
    text = unicodedata.normalize('NFKD', str(name))
    text = ''.join(c for c in text if not unicodedata.combining(c)).upper()
    words = re.sub(r"[^A-Z]+", ' ', text).split()
    return [w for w in words if w not in TITLES]


def _credit(a, b, cfg):
    if a == b:
        return 1.0
    if (len(a) == 1 or len(b) == 1) and a[0] == b[0]:
        return cfg['initial_credit']
    if len(a) == 1 or len(b) == 1:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= cfg['token_min_ratio'] else 0.0


def _align(expected, returned, cfg):
    pairs = sorted(
        ((_credit(e, r, cfg), i, j) for i, e in enumerate(expected) for j, r in enumerate(returned)),
        reverse=True,
    )
    used_e, used_r, matched = set(), set(), []
    for credit, i, j in pairs:
        if credit <= 0:
            break
        if i in used_e or j in used_r:
            continue
        used_e.add(i)
        used_r.add(j)
        matched.append((expected[i], returned[j], round(credit, 2)))
    return matched


def score(expected, returned, overrides=None):
    cfg = thresholds(overrides)
    exp, ret = normalise(expected), normalise(returned)
    if not exp or not ret:
        return 0, []
    matched = _align(exp, ret, cfg)
    total = sum(credit for _, _, credit in matched)
    return round(100 * total / min(len(exp), len(ret))), matched


def _surname_present(expected_surname, matched, cfg):
    surname = normalise(expected_surname)
    if not surname:
        return True
    hits = {e for e, _, credit in matched if credit >= cfg['surname_min_ratio']}
    return any(w in hits for w in surname)


def decide(expected, returned, surname=None, others=(), overrides=None):
    cfg = thresholds(overrides)
    snapshot = {k: cfg[k] for k in DEFAULT_THRESHOLDS}

    if not normalise(returned):
        return MatchResult(FAILED, 'NO_NAME_RETURNED', 0, thresholds=snapshot)
    if not normalise(expected):
        return MatchResult(MANUAL, 'NO_EXPECTED_NAME', 0, thresholds=snapshot)

    value, matched = score(expected, returned, cfg)
    result = MatchResult(FAILED, 'NAME_MISMATCH', value,
                         matched=[list(m) for m in matched], thresholds=snapshot)

    if value >= cfg['verified_min']:
        if len(matched) < cfg['min_tokens_matched']:
            result.decision, result.reason = MANUAL, 'TOO_FEW_WORDS_MATCHED'
        elif cfg['require_surname'] and not _surname_present(surname, matched, cfg):
            result.decision, result.reason = MANUAL, 'SURNAME_MISMATCH'
        else:
            result.decision, result.reason = VERIFIED, 'NAME_MATCH'
        return result

    for label, name in others:
        other_value, _ = score(name, returned, cfg)
        if other_value >= cfg['verified_min']:
            result.decision, result.reason, result.member = MANUAL, 'ANOTHER_MEMBER', label
            return result

    if value >= cfg['manual_min']:
        result.decision, result.reason = MANUAL, 'PARTIAL_MATCH'
    return result
