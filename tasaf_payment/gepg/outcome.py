from django.core.cache import cache

KEY = 'tasaf.verification.outcome:{}'
KEEP_SECONDS = 24 * 3600
FIELDS = ('count', 'batches', 'refused', 'refused_batches', 'skipped', 'withdrawn', 'error', 'queued')


def remember(client_mutation_id, result):
    if client_mutation_id and isinstance(result, dict):
        cache.set(KEY.format(client_mutation_id), {k: result.get(k) for k in FIELDS}, KEEP_SECONDS)


def recall(client_mutation_id):
    return cache.get(KEY.format(client_mutation_id)) if client_mutation_id else None
