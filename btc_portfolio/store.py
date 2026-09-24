"""Write intent before submission; only confirmed fills change the spot wallet."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def encoded(value):
    return json.dumps(value, sort_keys=True, default=str, allow_nan=False)


class Store:
    def __init__(self, path, identity, mode):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, venue TEXT NOT NULL,
                request TEXT NOT NULL, phase TEXT NOT NULL, response TEXT, created_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(venue TEXT, symbol TEXT, trade_id TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(venue,symbol,trade_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT,
                created_ms INTEGER NOT NULL);
        ''')
        binding = {"identity": identity, "mode": mode}
        previous = self.get("binding")
        if previous is not None and previous != binding:
            self.db.close()
            raise ValueError("Portfolio configuration, code or mode changed; explicit migration needed")
        self.put("binding", binding)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, encoded(value)))

    def event(self, kind, value):
        self.db.execute("INSERT INTO events(kind,payload,created_ms) VALUES(?,?,?)",
                        (kind, encoded(value), time.time_ns()//1_000_000))

    def intent(self, venue, key, request):
        ident = "bsg_"+hashlib.sha256((self.get("binding")["identity"]+venue+key).encode()).hexdigest()[:28]
        row = self.db.execute("SELECT * FROM intents WHERE id=?", (ident,)).fetchone()
        if row:
            if row["request"] != encoded(request):
                raise ValueError("Previously committed order request changed")
            return ident, False
        if self.pending() and not (venue == "coinm" and request.get("emergency") and request.get("reduce_only")):
            raise ValueError("Unresolved order prevents new submission")
        self.db.execute("INSERT INTO intents VALUES(?,?,?,'PENDING',NULL,?)",
                        (ident, venue, encoded(request), time.time_ns()//1_000_000))
        return ident, True

    def pending(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM intents WHERE phase='PENDING' ORDER BY created_ms")]

    def complete(self, ident, response):
        self.db.execute("UPDATE intents SET phase='DONE',response=? WHERE id=?", (encoded(response), ident))

    def fill(self, venue, symbol, trade_id, payload):
        old = self.db.execute("SELECT payload FROM fills WHERE venue=? AND symbol=? AND trade_id=?",
                              (venue, symbol, str(trade_id))).fetchone()
        data = encoded(payload)
        if old:
            if old[0] != data:
                raise ValueError("Previously recorded fill changed")
            return False
        self.db.execute("INSERT INTO fills VALUES(?,?,?,?)", (venue, symbol, str(trade_id), data))
        self.event("fill", {"venue": venue, "symbol": symbol, **payload})
        return True

    def close(self):
        self.db.close()
