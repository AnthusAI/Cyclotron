"""Item values as write-once, content-addressed records beside a control journal.

A catalog or workspace row that carries an item's values keeps ``valuesRef``
(the sha256 and length of the values' canonical JSON) instead of the values
themselves. The values are one create-only object in the same payload store the
journal already writes (``<scope>/payloads/<sha256>.blob``), so identical values
are one object, a repeated write is harmless, and every read verifies the digest.
The control journal then grows with the number of items, not with their text.

A document written before this existed still holds ``values`` inline and reads
exactly as before.
"""

import json
from hashlib import sha256

from .journal import encode
from .storage_payloads import PayloadRef

REF = "valuesRef"


def canonical(values):
    return encode(values)


def put_values(payloads, values):
    """Store ``values`` once; return the reference a row keeps in their place."""
    if not isinstance(values, dict):
        raise ValueError("item values must be an object")
    reference = payloads.put(canonical(values))
    return {"digest": reference.digest, "length": reference.length}


def get_values(payloads, ref):
    """The values a reference names, verified against its digest."""
    if not isinstance(ref, dict) or set(ref) != {"digest", "length"}:
        raise ValueError("invalid item values reference")
    return json.loads(payloads.get(PayloadRef(payloads.scope, ref["digest"], ref["length"])))


def externalized(payloads, document):
    """A copy of ``document`` whose ``values`` became a ``valuesRef``; anything else is unchanged."""
    if not isinstance(document, dict) or "values" not in document:
        return document
    rest = {key: value for key, value in document.items() if key != "values"}
    return {**rest, REF: put_values(payloads, document["values"])}


def resolved(payloads, document):
    """A copy of ``document`` with its ``valuesRef`` read back into ``values``; inline documents are unchanged."""
    if not isinstance(document, dict) or REF not in document:
        return document
    rest = {key: value for key, value in document.items() if key != REF}
    return {**rest, "values": get_values(payloads, document[REF])}


def same_values(document, values):
    """Whether a stored document (inline or by reference) holds exactly ``values``, without reading the record."""
    if REF in document:
        content = canonical(values)
        return document[REF] == {"digest": sha256(content).hexdigest(), "length": len(content)}
    return document.get("values") == values
