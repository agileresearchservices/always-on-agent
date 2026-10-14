"""Search attributes for the PR workflows, so `temporal workflow list --query 'PrNumber=27'` (and the
Temporal UI) can find them. They must be registered on the server before a workflow uses them."""
from __future__ import annotations

import logging
from typing import Any

from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

REPO = SearchAttributeKey.for_keyword("Repo")
PR_NUMBER = SearchAttributeKey.for_int("PrNumber")
SHA = SearchAttributeKey.for_keyword("Sha")
SESSION = SearchAttributeKey.for_keyword("Session")

log = logging.getLogger(__name__)


def pr_attrs(repo: str, number: int, sha: str, session: str) -> TypedSearchAttributes:
    return TypedSearchAttributes([
        SearchAttributePair(REPO, repo), SearchAttributePair(PR_NUMBER, int(number)),
        SearchAttributePair(SHA, sha), SearchAttributePair(SESSION, session),
    ])


async def ensure_registered(client: Any) -> bool:
    """Register the attributes if the server does not know them yet. Safe to call on every start. Returns
    False (and logs) when the server refuses, so callers can carry on without attributes."""
    from temporalio.api.enums.v1 import IndexedValueType
    from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest, ListSearchAttributesRequest
    from temporalio.service import RPCError

    want = {k.name: k.indexed_value_type for k in (REPO, PR_NUMBER, SHA, SESSION)}
    ops = client.operator_service
    try:
        have = await ops.list_search_attributes(ListSearchAttributesRequest(namespace=client.namespace))
        missing = {n: t for n, t in want.items() if n not in have.custom_attributes}
        if missing:
            await ops.add_search_attributes(AddSearchAttributesRequest(
                namespace=client.namespace,
                search_attributes={n: IndexedValueType.Value(f"INDEXED_VALUE_TYPE_{t.name}") for n, t in missing.items()},
            ))
        return True
    except RPCError as e:
        log.warning("could not register search attributes (%s); PR workflows start without them", e.status)
        return False
