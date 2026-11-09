"""Limitation de débit en mémoire (fenêtre glissante).

Objectif : freiner les abus sur les routes SENSIBLES ou publiques —
inscription, connexion, réinitialisation, tracking — sans dépendre
d'un service externe. Fenêtre glissante par clé (IP, email, …) :

    limiter = RateLimiter(max_events=10, window_s=900)
    if not limiter.allow('login:1.2.3.4'):
        raise HTTPException(429, 'Trop de tentatives…')

Limites assumées (documentées dans docs/SECURITY.md) : état par
processus — adapté au plan Render mono-instance ; un redémarrage
réinitialise les compteurs (acceptable : les protections financières
sont, elles, dans PostgreSQL, via contraintes uniques).
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class RateLimiter:
    """Fenêtre glissante : au plus `max_events` par `window_s` et par clé."""

    def __init__(self, max_events: int, window_s: float):
        self.max_events = max_events
        self.window_s = window_s
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._last_gc = 0.0

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        """Enregistre un événement et dit s'il est autorisé."""
        now = time.time() if now is None else now
        with self._lock:
            events = self._events.setdefault(key, [])
            cutoff = now - self.window_s
            events[:] = [t for t in events if t > cutoff]
            if len(events) >= self.max_events:
                return False
            events.append(now)
            self._gc(now)
            return True

    def _gc(self, now: float) -> None:
        """Purge périodique des clés devenues silencieuses (mémoire)."""
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        cutoff = now - self.window_s
        for k in [k for k, v in self._events.items()
                  if not v or v[-1] <= cutoff]:
            del self._events[k]

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
