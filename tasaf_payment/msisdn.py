"""Tanzanian mobile-money numbers: 255 + 9 digits, the national number starting with 6 or 7."""
import re

MSISDN_RE = re.compile(r'^255[67]\d{8}$')
MSISDN_DB_PATTERN = r'^255[67][0-9]{8}$'


def normalise(value):
    """The unambiguous forms become 255XXXXXXXXX (+255…, 07…, 7…, spaces, dashes). Anything else
    is returned stripped but unchanged — a number with a digit too many is never guessed at."""
    digits = re.sub(r'[\s\-().+]', '', value or '')
    if re.fullmatch(r'0[67]\d{8}', digits):
        return '255' + digits[1:]
    if re.fullmatch(r'[67]\d{8}', digits):
        return '255' + digits
    return digits


def is_valid(value):
    return bool(MSISDN_RE.match(value or ''))
