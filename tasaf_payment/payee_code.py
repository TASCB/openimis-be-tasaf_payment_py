"""HHID <-> MUSE payeeCode. The only implementation; every MUSE path goes through it.

payeeCode = "TSFBN" + base62(block9 * 10^8 + block8), left-padded to 10 characters, so always
15 characters and within MUSE's ^[0-9a-zA-Z]{1,15}$. The code is case-sensitive: never change its
case anywhere, an uppercased code decodes to a different, valid HHID.
"""
import re

ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
PREFIX = 'TSFBN'
WIDTH = 10
MAX_VALUE = 10 ** 17 - 1
HHID_RE = re.compile(r'^P3-(\d{9})-(\d{8})$')
PAYEE_CODE_RE = re.compile(r'^TSFBN([0-9A-Za-z]{10})$')
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}


class PayeeCodeError(ValueError):
    pass


def encode_hhid(hhid):
    match = HHID_RE.match(hhid) if isinstance(hhid, str) else None
    if not match:
        raise PayeeCodeError(f"Not a P3 HHID (P3-#########-########): {hhid!r}")
    n = int(match.group(1)) * 10 ** 8 + int(match.group(2))
    digits = ''
    while n:
        n, r = divmod(n, 62)
        digits = ALPHABET[r] + digits
    return PREFIX + digits.rjust(WIDTH, '0')


def decode_payee_code(code):
    match = PAYEE_CODE_RE.match(code) if isinstance(code, str) else None
    if not match:
        raise PayeeCodeError(f"Not a TASAF payeeCode (TSFBN + 10 base62 characters): {code!r}")
    n = 0
    for ch in match.group(1):
        n = n * 62 + _INDEX[ch]
    if n > MAX_VALUE:
        raise PayeeCodeError(f"payeeCode {code!r} cannot come from a valid HHID")
    block9, block8 = divmod(n, 10 ** 8)
    return f"P3-{block9:09d}-{block8:08d}"
