
"""Fernet-based encryption for the local configuration cache.



The cache exists so that a configuration-server outage cannot stop trading.

It holds live credentials at rest, so the key is stored outside the cache

directory with ``0o600`` permissions and written via ``O_EXCL`` to avoid

clobbering an existing key or following a symlink.

"""



from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken

from betdoc.infrastructure.config.provider import ConfigError

__all__ = ["DataEncoder"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



_SECURE_FILE_MODE: Final[int] = 0o600

_SECURE_DIR_MODE: Final[int] = 0o700





class _CanonicalJSONEncoder(json.JSONEncoder):

    """JSON encoder that renders :class:`Decimal` losslessly as a string."""



    def default(self, o: Any) -> Any:

        if isinstance(o, Decimal):

            return str(o)

        if isinstance(o, (set, frozenset)):

            return sorted(str(item) for item in o)

        if isinstance(o, Path):

            return str(o)

        return super().default(o)





class DataEncoder:

    """Symmetric encoder for cached configuration payloads.



    A single randomly generated Fernet key is persisted at ``key_file``. If the

    file is absent a fresh key is created on first use; if it is present its

    permissions are verified and tightened when the file is group- or

    world-accessible.

    """



    __slots__ = ("_fernet", "_key_file")



    def __init__(

        self,

        key_file: str | Path = Path("var/lib/betdoc/config.key"),

        *,

        create_if_missing: bool = True,

    ) -> None:

        self._key_file: Path = Path(key_file)

        key: bytes = self._load_or_create_key(create_if_missing=create_if_missing)

        try:

            self._fernet: Fernet = Fernet(key)

        except (ValueError, TypeError) as error:

            raise ConfigError(

                f"key file does not contain a valid Fernet key: {error}",

                code="INVALID_KEY",

            ) from error



    @property

    def key_file(self) -> Path:

        """Filesystem location of the Fernet key."""

        return self._key_file



    @classmethod

    def generate_key(cls) -> bytes:

        """Return a fresh url-safe base64 Fernet key."""

        return Fernet.generate_key()



    def encode(self, data: Mapping[str, Any] | list[Any]) -> bytes:

        """Serialise ``data`` to canonical JSON and encrypt it.



        Raises

        ------

        ConfigError

            ``ENCODE_FAILED`` if the payload is not JSON-serialisable.

        """

        try:

            plaintext: bytes = json.dumps(

                data,

                cls=_CanonicalJSONEncoder,

                sort_keys=True,

                separators=(",", ":"),

                ensure_ascii=False,

            ).encode("utf-8")

        except (TypeError, ValueError) as error:

            raise ConfigError(

                f"payload is not JSON-serialisable: {error}", code="ENCODE_FAILED"

            ) from error



        try:

            return self._fernet.encrypt(plaintext)

        except Exception as error:

            raise ConfigError(

                f"encryption failed: {error}", code="ENCODE_FAILED"

            ) from error



    def decode(self, token: bytes | str, *, ttl_seconds: int | None = None) -> Any:

        """Decrypt ``token`` and parse the embedded JSON document.



        Raises

        ------

        ConfigError

            ``DECODE_FAILED`` if the token is not authentic, has expired, or

            does not contain valid JSON.

        """

        raw: bytes = token.encode("utf-8") if isinstance(token, str) else token

        try:

            plaintext: bytes = self._fernet.decrypt(raw, ttl=ttl_seconds)

        except InvalidToken as error:

            raise ConfigError(

                "ciphertext failed authentication (wrong key, corrupt, or expired)",

                code="DECODE_FAILED",

            ) from error

        except (TypeError, ValueError) as error:

            raise ConfigError(

                f"ciphertext is malformed: {error}", code="DECODE_FAILED"

            ) from error



        try:

            return json.loads(plaintext.decode("utf-8"))

        except (UnicodeDecodeError, json.JSONDecodeError) as error:

            raise ConfigError(

                f"decrypted payload is not valid JSON: {error}", code="DECODE_FAILED"

            ) from error



    def save_encoded(self, path: str | Path, data: Mapping[str, Any]) -> None:

        """Encrypt ``data`` and write it atomically to ``path`` with ``0o600``.



        The ciphertext is written to a temporary file in the destination

        directory and then renamed, so a crash mid-write cannot leave a

        truncated cache behind.

        """

        target = Path(path)

        payload: bytes = self.encode(data)



        try:

            target.parent.mkdir(parents=True, exist_ok=True, mode=_SECURE_DIR_MODE)

            handle, temporary = tempfile.mkstemp(

                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)

            )

            temporary_path = Path(temporary)

            try:

                with os.fdopen(handle, "wb") as stream:

                    stream.write(payload)

                    stream.flush()

                    os.fsync(stream.fileno())

                os.chmod(temporary_path, _SECURE_FILE_MODE)

                os.replace(temporary_path, target)

            except BaseException:

                temporary_path.unlink(missing_ok=True)

                raise

        except OSError as error:

            raise ConfigError(

                f"unable to write encrypted cache to {target}: {error}",

                code="WRITE_FAILED",

            ) from error



    def load_encoded(

        self, path: str | Path, *, ttl_seconds: int | None = None

    ) -> dict[str, Any]:

        """Read and decrypt the cache file at ``path``.



        Raises

        ------

        ConfigError

            ``CACHE_MISSING`` when the file does not exist, ``READ_FAILED`` on

            an I/O error, or ``DECODE_FAILED`` when the contents are unusable.

        """

        source = Path(path)

        if not source.is_file():

            raise ConfigError(

                f"encrypted cache not found at {source}", code="CACHE_MISSING"

            )

        try:

            ciphertext: bytes = source.read_bytes()

        except OSError as error:

            raise ConfigError(

                f"unable to read encrypted cache at {source}: {error}",

                code="READ_FAILED",

            ) from error



        decoded = self.decode(ciphertext, ttl_seconds=ttl_seconds)

        if not isinstance(decoded, dict):

            raise ConfigError(

                "encrypted cache does not contain a JSON object", code="DECODE_FAILED"

            )

        return decoded



    def _load_or_create_key(self, *, create_if_missing: bool) -> bytes:

        if self._key_file.is_file():

            return self._read_existing_key()

        if not create_if_missing:

            raise ConfigError(

                f"key file {self._key_file} is missing and creation is disabled",

                code="KEY_MISSING",

            )

        return self._create_key()



    def _read_existing_key(self) -> bytes:

        try:

            mode = stat.S_IMODE(self._key_file.stat().st_mode)

            if mode & (stat.S_IRWXG | stat.S_IRWXO):

                _LOG.warning(

                    "key file %s had permissions %o, tightening to %o",

                    self._key_file,

                    mode,

                    _SECURE_FILE_MODE,

                )

                os.chmod(self._key_file, _SECURE_FILE_MODE)

            key = self._key_file.read_bytes().strip()

        except OSError as error:

            raise ConfigError(

                f"unable to read key file {self._key_file}: {error}",

                code="KEY_UNREADABLE",

            ) from error

        if not key:

            raise ConfigError(

                f"key file {self._key_file} is empty", code="INVALID_KEY"

            )

        return key



    def _create_key(self) -> bytes:

        key: bytes = self.generate_key()

        try:

            self._key_file.parent.mkdir(

                parents=True, exist_ok=True, mode=_SECURE_DIR_MODE

            )

            descriptor = os.open(

                self._key_file,

                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),

                _SECURE_FILE_MODE,

            )

            with os.fdopen(descriptor, "wb") as stream:

                stream.write(key)

                stream.flush()

                os.fsync(stream.fileno())

        except FileExistsError:

            # Another process won the race; adopt the key it wrote.

            return self._read_existing_key()

        except OSError as error:

            raise ConfigError(

                f"unable to create key file {self._key_file}: {error}",

                code="KEY_WRITE_FAILED",

            ) from error



        _LOG.info("generated a new configuration cache key at %s", self._key_file)

        return key


