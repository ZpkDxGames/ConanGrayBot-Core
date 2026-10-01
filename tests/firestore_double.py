"""In-process Firestore SDK contract double; production certification uses real Firestore."""

import copy
import threading
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

from firebase_admin import firestore


def materialize(value):
    if value is firestore.SERVER_TIMESTAMP:
        return datetime.now(timezone.utc)
    if isinstance(value, dict):
        return {k: materialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [materialize(v) for v in value]
    return copy.deepcopy(value)


class Snapshot:
    def __init__(self, reference):
        self.reference, self.id = reference, reference.id
        self.exists = reference.path in reference.client.docs

    def to_dict(self):
        return copy.deepcopy(self.reference.client.docs.get(self.reference.path))


class Document:
    def __init__(self, client, path):
        self.client, self.path, self.id = client, path, path.rsplit("/", 1)[-1]

    def collection(self, name):
        return Query(self.client, self.path + "/" + name)

    def get(self, **kwargs):
        return Snapshot(self)

    def set(self, row, merge=False):
        data = materialize(row)
        if merge:
            data = {**self.client.docs.get(self.path, {}), **data}
        self.client.docs[self.path] = data

    def delete(self):
        self.client.docs.pop(self.path, None)


class Query:
    def __init__(self, client, path, filters=(), ordering=(), count=None, cursor=None):
        self.client, self.path, self.filters, self.ordering, self.limit_count, self.cursor = (
            client,
            path,
            filters,
            ordering,
            count,
            cursor,
        )

    def document(self, name):
        return Document(self.client, self.path + "/" + str(name))

    def add(self, row):
        doc = self.document(uuid4().hex)
        doc.set(row)
        return datetime.now(timezone.utc), doc

    def where(self, *args, filter=None):
        field, op, value = (
            (filter.field_path, filter.op_string, filter.value) if filter else args
        )
        assert op == "=="
        return Query(
            self.client,
            self.path,
            self.filters + ((field, value),),
            self.ordering,
            self.limit_count,
            self.cursor,
        )

    def order_by(self, field, direction="ASCENDING"):
        return Query(
            self.client,
            self.path,
            self.filters,
            self.ordering + ((field, direction),),
            self.limit_count,
            self.cursor,
        )

    def limit(self, count):
        return Query(
            self.client, self.path, self.filters, self.ordering, count, self.cursor
        )

    def start_after(self, snapshot):
        return Query(
            self.client, self.path, self.filters, self.ordering, self.limit_count, snapshot.id
        )

    def count(self, alias=None):
        return Aggregate(self, "count", None, alias)

    def sum(self, field, alias=None):
        return Aggregate(self, "sum", field, alias)

    def stream(self):
        paths = [
            p
            for p, row in self.client.docs.items()
            if p.startswith(self.path + "/")
            and "/" not in p[len(self.path) + 1 :]
            and all(row.get(k) == v for k, v in self.filters)
        ]
        for field, direction in reversed(self.ordering):
            paths.sort(
                key=lambda p: (
                    p.rsplit("/", 1)[-1]
                    if field == "__name__"
                    else self.client.docs[p].get(field)
                ),
                reverse=direction == "DESCENDING",
            )
        if self.cursor is not None:
            ids = [p.rsplit("/", 1)[-1] for p in paths]
            paths = paths[ids.index(self.cursor) + 1 :]
        if self.limit_count is not None:
            paths = paths[: self.limit_count]
        return iter(Snapshot(Document(self.client, p)) for p in paths)


class Aggregate:
    def __init__(self, query, operation, field, alias):
        self.query = query
        self.operation = operation
        self.field = field
        self.alias = alias

    def get(self):
        rows = [snapshot.to_dict() for snapshot in self.query.stream()]
        if self.operation == "count":
            value = len(rows)
        else:
            value = sum(row.get(self.field, 0) for row in rows)
        return [[SimpleNamespace(alias=self.alias, value=value)]]


class Batch:
    def __init__(self):
        self.operations = []

    def set(self, ref, data, merge=False):
        self.operations.append(lambda: ref.set(data, merge=merge))

    def delete(self, ref):
        self.operations.append(ref.delete)

    def commit(self):
        for operation in self.operations:
            operation()
        self.operations = []


class Client:
    def __init__(self):
        self.docs = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return Query(self, name)

    def batch(self):
        return Batch()

    def transaction(self):
        return Batch()


def transactional(lock):
    def decorate(function):
        @wraps(function)
        def run(tx):
            with lock:
                result = function(tx)
                tx.commit()
                return result

        return run

    return decorate
