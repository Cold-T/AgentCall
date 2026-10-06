"""Bounded per-client authentication failure windows for fixed PIN access."""

import math
from collections import OrderedDict, deque
from time import monotonic


class FailedAuthLimiter:
    def __init__(self, attempts=10, window=60, clients=1024):
        self.attempts = attempts
        self.window = window
        self.clients = clients
        self.failures = OrderedDict()

    def retry_after(self, client):
        entries = self.failures.get(client)
        if entries is None:
            return 0
        now = monotonic()
        while entries and entries[0] <= now - self.window:
            entries.popleft()
        if not entries:
            self.failures.pop(client, None)
            return 0
        self.failures.move_to_end(client)
        if len(entries) >= self.attempts:
            return max(1, math.ceil(self.window - (now - entries[0])))
        return 0

    def failed(self, client):
        if client not in self.failures:
            if len(self.failures) >= self.clients:
                self.failures.popitem(last=False)
            self.failures[client] = deque(maxlen=self.attempts)
        self.failures[client].append(monotonic())
