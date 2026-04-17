# openimis-be-tasaf_payment_py

The openIMIS Backend TASAF Payment module.

Provides `PaymentAccount` and `VerificationRecord` models for managing FSP payment accounts
linked to GroupBeneficiary records, plus NIDA/FSP name-matching verification.

## What this module adds

This module covers only what is **not** already in the OpenIMIS stack:

| New Model | Purpose |
|-----------|---------|
| `PaymentAccount` | Bank/mobile account per GroupBeneficiary; tracks FSP type, name, account number, and verification status |
| `VerificationRecord` | One result entry from a verification run (NIDA name vs FSP name, match score) |

Everything else reuses existing modules: payroll, payment_cycle, individual, social_protection.

## Permissions

| Code | Description |
|------|-------------|
| 152001 | Search payment accounts |
| 152002 | Create payment account |
| 152003 | Update payment account |
| 152004 | Delete payment account |
| 152101 | Run verification |
| 152102 | Approve payment accounts |
| 152103 | Generate payroll |
| 152104 | Submit payroll to MUSE |
| 152105 | Resubmit failed accounts |

## License

GNU AGPL v3
