"""Optional, rebuildable payload discovery; never a mutable head or paid ledger.

Candidates are untrusted hints. Consumers must read and verify canonical bytes
through Payloads, and separately authorize the human/account and domain action.
The host injects a fixed client/table/scope; this module discovers no credentials.
"""

import re

from .batch_storage import StorageScope
from .storage_payloads import PayloadRef


class IndexCorrupt(RuntimeError):
    pass


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("payload SHA256 cursor required")
    return value


class DynamoPayloadIndex:
    """Low-level DynamoDB client; string PK=account/workspace, SK=SHA256.

    No infrastructure is created. Losing the table loses only discovery hints;
    a bounded stream of known canonical references can rebuild it. The index
    does not identify a current generation, prove completeness or grant dispatch.
    """

    def __init__(self, client, table, scope):
        if not isinstance(table, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table) or not isinstance(scope, StorageScope):
            raise ValueError("fixed table and account/workspace scope required")
        self.client, self.table, self.scope = client, table, scope

    def rebuild(self, payloads, references, *, max_records):
        """Add verified candidates; bounded and safely repeatable after failure.

        Explicit failure can leave partial index progress. It never removes
        canonical bytes or existing candidates. Snapshot discovery is the host's
        job; this is not an unbounded bucket/table scan or a migration authority.
        """
        if type(max_records) is not int or max_records < 1:
            raise ValueError("bounded rebuild required")
        count = 0
        for reference in references:
            if count >= max_records:
                raise ValueError("payload index rebuild budget exceeded")
            if not isinstance(reference, PayloadRef) or reference.scope != self.scope:
                raise PermissionError("payload index scope refused")
            payloads.get(reference)  # Verify canonical existence, length and hash.
            self.client.put_item(
                TableName=self.table,
                Item={"PK": {"S": self.scope.key}, "SK": {"S": reference.digest}, "length": {"N": str(reference.length)}},
                ConditionExpression="attribute_not_exists(PK) OR #length = :length",
                ExpressionAttributeNames={"#length": "length"},
                ExpressionAttributeValues={":length": {"N": str(reference.length)}},
            )
            count += 1
        return count

    def _key(self, key):
        if not isinstance(key, dict) or set(key) != {"PK", "SK"} or key["PK"] != {"S": self.scope.key}:
            raise IndexCorrupt("foreign payload index key")
        try:
            return _digest(key["SK"]["S"])
        except (KeyError, TypeError, ValueError):
            raise IndexCorrupt("invalid payload index key") from None

    def page(self, *, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded index page required")
        request = dict(
            TableName=self.table,
            KeyConditionExpression="PK = :scope",
            ExpressionAttributeValues={":scope": {"S": self.scope.key}},
            ProjectionExpression="PK, SK, #length",
            ExpressionAttributeNames={"#length": "length"},
            ConsistentRead=False,
            Limit=limit,
            ScanIndexForward=True,
        )
        if after is not None:
            request["ExclusiveStartKey"] = {"PK": {"S": self.scope.key}, "SK": {"S": _digest(after)}}
        response = self.client.query(**request)
        rows = response.get("Items")
        if not isinstance(rows, list) or len(rows) > limit:
            raise IndexCorrupt("invalid bounded index page")
        references, last = [], after
        for row in rows:
            try:
                if not isinstance(row, dict) or set(row) != {"PK", "SK", "length"}:
                    raise ValueError()
                digest = self._key({key: row[key] for key in ("PK", "SK")})
                length = row["length"]["N"]
                if (
                    not isinstance(length, str)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,19}", length)
                    or last is not None
                    and digest <= last
                ):
                    raise ValueError()
                references.append(PayloadRef(self.scope, digest, int(length)))
                last = digest
            except (KeyError, TypeError, ValueError):
                raise IndexCorrupt("invalid payload index candidate") from None
        key = response.get("LastEvaluatedKey")
        cursor = self._key(key) if key else None
        if cursor is not None and (last is None or cursor != last or after is not None and cursor <= after):
            raise IndexCorrupt("invalid payload index continuation")
        return references, cursor
