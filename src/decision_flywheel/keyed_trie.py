"""A persistent hash trie of text values over immutable, content-addressed payloads.

A journal head that keeps a map of every item or request grows with history, and every commit rewrites it.
This trie keeps such a map out of the head: the head names only the root node. Each node is one immutable
payload holding at most sixteen entries, addressed by its SHA-256, so a node never changes once written.

A key's path is the hex SHA-256 of the key; depth ``d`` branches on path character ``d``. An entry is a
leaf (``["leaf", key, value]``) or a child node (``["node", digest, length]``). A leaf sits at the shallowest
depth where its path is unique among the keys present, and removing keys pulls a lone leaf back up, so the
same contents always give the same nodes and the same root, whatever order they were written in.

Changing a key rewrites only the nodes on its path: about log16 of the key count, each at most sixteen
entries. A lookup reads the same path. Absence is proved by the trie itself (an empty slot or a different
leaf), so no lookup ever GETs an object that might not exist, which a principal without list permission
could not tell from a forbidden one.

``update`` writes nothing: it returns the new root and the bytes of every new node, which the caller
publishes before it commits the head that names the root. Unreferenced nodes from an abandoned commit are
harmless orphans.
"""

from collections import defaultdict
from hashlib import sha256

from .journal import decode, encode
from .storage_payloads import PayloadRef

SCHEMA = "cyclotron-trie/v1"
_HEX = "0123456789abcdef"


class TrieCorrupt(RuntimeError):
    pass


def _path(key):
    return sha256(key.encode()).hexdigest()


def ref_of(content):
    """``[digest, length]`` of node bytes, the form a head stores."""
    return [sha256(content).hexdigest(), len(content)]


class KeyedTrie:
    """Lookups and copy-on-write updates of one payload store's tries. Nodes read are kept, since they never change."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.nodes = {}  # digest -> entries

    # Nodes -------------------------------------------------------------------
    def _node(self, ref):
        digest, length = ref
        entries = self.nodes.get(digest)
        if entries is None:
            content = self.payloads.get(PayloadRef(self.payloads.scope, digest, length))
            entries = self._decode(content)
            self.nodes[digest] = entries
        return entries

    def preload(self, contents):
        """Keep nodes fetched elsewhere ({digest: bytes}), after checking each is the node its digest names."""
        for digest, content in contents.items():
            if ref_of(content)[0] != digest:
                raise TrieCorrupt("keyed trie node does not match its digest")
            self.nodes.setdefault(digest, self._decode(content))

    @staticmethod
    def _decode(content):
        try:
            node = decode(content)
            if set(node) != {"schema", "entries"} or node["schema"] != SCHEMA or not isinstance(node["entries"], dict):
                raise ValueError()
            for slot, entry in node["entries"].items():
                if slot not in _HEX or len(slot) != 1 or not isinstance(entry, list) or len(entry) != 3:
                    raise ValueError()
                if entry[0] == "leaf":
                    if not isinstance(entry[1], str) or not isinstance(entry[2], str):
                        raise ValueError()
                elif entry[0] != "node" or not isinstance(entry[1], str) or type(entry[2]) is not int:
                    raise ValueError()
            return node["entries"]
        except Exception:
            raise TrieCorrupt("invalid keyed trie node") from None

    def _encode(self, entries, written):
        content = encode({"schema": SCHEMA, "entries": entries})
        ref = ref_of(content)
        self.nodes[ref[0]] = entries
        written[ref[0]] = content
        return ref

    # Reads -------------------------------------------------------------------
    def get(self, root, key):
        """The value at ``key``, or None when the trie holds no such key."""
        if root is None:
            return None
        path, entries = _path(key), self._node(root)
        for slot in path:
            entry = entries.get(slot)
            if entry is None:
                return None
            if entry[0] == "leaf":
                return entry[2] if entry[1] == key else None
            entries = self._node(entry[1:])
        raise TrieCorrupt("keyed trie deeper than a key path")

    def items(self, root):
        """Every (key, value), in path order. Reads the whole trie: for audits, conversions and specs."""
        if root is None:
            return
        stack = [self._node(root)]
        while stack:
            entries = stack.pop()
            for slot in sorted(entries, reverse=True):
                entry = entries[slot]
                if entry[0] == "leaf":
                    yield entry[1], entry[2]
                else:
                    stack.append(self._node(entry[1:]))

    # Writes ------------------------------------------------------------------
    def update(self, root, changes):
        """(new root, {digest: node bytes} to publish) after setting each key to its text, or removing it for None."""
        written = {}
        items = [(_path(key), key, value) for key, value in changes.items()]
        for _, key, value in items:
            if not isinstance(key, str) or (value is not None and not isinstance(value, str)):
                raise ValueError("text keys and text values (or None to remove) required")
        if not items:
            return root, written
        base = self._node(root) if root is not None else {}
        entries = self._apply(base, 0, items, written)
        if entries == base:
            return root, written
        if not entries:
            return None, written
        return self._encode(entries, written), written

    def _apply(self, entries, depth, items, written):
        groups = defaultdict(list)
        for item in items:
            if depth >= len(item[0]):
                raise TrieCorrupt("two keys share a whole path")
            groups[item[0][depth]].append(item)
        out = dict(entries)
        for slot, group in groups.items():
            entry = self._slot(out.get(slot), depth + 1, group, written)
            if entry is None:
                out.pop(slot, None)
            else:
                out[slot] = entry
        return out

    def _slot(self, entry, depth, items, written):
        if entry is not None and entry[0] == "leaf":
            if all(key != entry[1] for _, key, _ in items):
                items = [(_path(entry[1]), entry[1], entry[2]), *items]
            base = {}
        else:
            base = self._node(entry[1:]) if entry is not None else {}
        items = [item for item in items if item[2] is not None or base]
        if not items:
            return entry if base else None
        if not base and len(items) == 1:
            _, key, value = items[0]
            return None if value is None else ["leaf", key, value]
        entries = self._apply(base, depth, items, written)
        if entry is not None and entry[0] == "node" and entries == base:
            return entry
        if not entries:
            return None
        if len(entries) == 1:
            (only,) = entries.values()
            if only[0] == "leaf":
                return only
        return ["node", *self._encode(entries, written)]
