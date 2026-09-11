
"""Configuration management for BetDoc infrastructure services.



Exposes the remote KV-v2 backed :class:`ConfigProvider`, the Fernet-based

:class:`DataEncoder` used for encrypted local fallback caching, the

:class:`ServiceCredentials` value object, and the :class:`ConfigError`

raised by both layers.

"""



from __future__ import annotations



from betdoc.infrastructure.config.encoder import DataEncoder

from betdoc.infrastructure.config.provider import (

    ConfigError,

    ConfigProvider,

    ServiceCredentials,

)



__all__ = [

    "ConfigError",

    "ConfigProvider",

    "DataEncoder",

    "ServiceCredentials",

] 

