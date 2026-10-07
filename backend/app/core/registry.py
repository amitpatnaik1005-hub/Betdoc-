"""Generic, thread-safe plugin registry shared by every integration family."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class PluginNotFoundError(LookupError):
    """Raised when a plugin cannot be resolved and no fallback plugin exists."""

    def __init__(self, registry_name: str, plugin_name: str) -> None:
        self.registry_name = registry_name
        self.plugin_name = plugin_name
        super().__init__(
            f"No plugin named {plugin_name!r} is registered in registry "
            f"{registry_name!r} and no fallback plugin is configured."
        )


class PluginRegistry(Generic[T]):
    """Registry mapping case-insensitive names to plugin classes.

    The registry stores class references (``type[T]``), never instances, so
    every caller gets a fresh object and no mutable state is shared between
    requests or threads. Lookups that miss fall back to the registered
    fallback plugin, if one exists.
    """

    def __init__(self, name: str = "plugins") -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Registry name must be a non-empty string.")
        self._name = name.strip()
        self._plugins: dict[str, type[T]] = {}
        self._fallback: type[T] | None = None
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def fallback(self) -> type[T] | None:
        with self._lock:
            return self._fallback

    @staticmethod
    def normalize(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError(f"Plugin names must be strings, got {type(name).__name__}.")
        key = name.strip().casefold()
        if not key:
            raise ValueError("Plugin names must be non-empty.")
        return key

    def register(
        self,
        name: str,
        *,
        aliases: Iterable[str] = (),
        fallback: bool = False,
        replace: bool = False,
    ) -> Callable[[type[T]], type[T]]:
        """Class decorator: ``@registry.register("Name")``."""
        keys = tuple(dict.fromkeys([self.normalize(name), *(self.normalize(a) for a in aliases)]))

        def decorator(plugin: type[T]) -> type[T]:
            if not isinstance(plugin, type):
                raise TypeError(
                    f"Registry {self._name!r} only accepts classes, got {type(plugin).__name__}."
                )
            with self._lock:
                if not replace:
                    for key in keys:
                        existing = self._plugins.get(key)
                        if existing is not None and existing is not plugin:
                            raise ValueError(
                                f"Plugin name {key!r} is already registered in registry "
                                f"{self._name!r} by {existing.__qualname__}."
                            )
                    if fallback and self._fallback is not None and self._fallback is not plugin:
                        raise ValueError(
                            f"Registry {self._name!r} already has fallback "
                            f"{self._fallback.__qualname__}."
                        )
                for key in keys:
                    self._plugins[key] = plugin
                if fallback:
                    self._fallback = plugin
            return plugin

        return decorator

    def register_plugin(
        self,
        name: str,
        plugin: type[T],
        *,
        aliases: Iterable[str] = (),
        fallback: bool = False,
        replace: bool = False,
    ) -> type[T]:
        """Imperative equivalent of the ``register`` decorator."""
        return self.register(name, aliases=aliases, fallback=fallback, replace=replace)(plugin)

    def set_fallback(self, plugin: type[T] | None) -> None:
        if plugin is not None and not isinstance(plugin, type):
            raise TypeError("Fallback plugin must be a class or None.")
        with self._lock:
            self._fallback = plugin

    def unregister(self, name: str) -> type[T] | None:
        key = self.normalize(name)
        with self._lock:
            return self._plugins.pop(key, None)

    def get(self, name: str) -> type[T] | None:
        """Return the exact plugin for ``name`` without applying the fallback."""
        key = self.normalize(name)
        with self._lock:
            return self._plugins.get(key)

    def resolve(self, name: str) -> type[T]:
        """Return the plugin for ``name``, or the fallback plugin if none matches."""
        key = self.normalize(name)
        with self._lock:
            plugin = self._plugins.get(key)
            if plugin is not None:
                return plugin
            if self._fallback is not None:
                return self._fallback
        raise PluginNotFoundError(self._name, name)

    def create(self, name: str, /, *args: Any, **kwargs: Any) -> T:
        """Resolve ``name`` and build a new, independent plugin instance."""
        plugin_cls = self.resolve(name)
        return plugin_cls(*args, **kwargs)

    def is_registered(self, name: str) -> bool:
        return self.get(name) is not None

    def plugins(self) -> dict[str, type[T]]:
        with self._lock:
            return dict(self._plugins)

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._plugins))

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            return self.is_registered(name)
        except ValueError:
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._plugins)

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __repr__(self) -> str:
        fallback = self.fallback
        fallback_name = fallback.__qualname__ if fallback is not None else None
        return f"PluginRegistry(name={self._name!r}, plugins={list(self.names())!r}, fallback={fallback_name!r})"
