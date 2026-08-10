"""Reset the prompt-injection scenario using the shared actor lifecycle."""

from ..pg_lock_contention_v1.reset import kill_actor_processes, reset

__all__ = ["kill_actor_processes", "reset"]
