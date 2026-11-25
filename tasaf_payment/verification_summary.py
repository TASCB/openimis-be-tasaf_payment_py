from collections import Counter

DECISION_TEXT = (('VERIFIED', 'verified'), ('FAILED', 'failed'), ('MANUAL', 'need review'),
                 ('UNCHECKED', 'not checked'), ('IGNORED', 'already decided'), ('', 'checked'))


def answered_desc(rows):
    rows = list(rows)
    waiting = sum(1 for status, _ in rows if status == 'PENDING')
    decisions = Counter(decision or '' for status, decision in rows if status == 'ANSWERED')
    parts = [f'{decisions[code]} {text}' for code, text in DECISION_TEXT if decisions.get(code)]
    text = 'Answered: ' + (', '.join(parts) or 'no account decided')
    return f'{text}; {waiting} still waiting' if waiting else text
