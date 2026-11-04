class C2AError(Exception):
    """Base error; the CLI prints these without a traceback."""


class ConfigError(C2AError):
    pass


class MediaError(C2AError):
    pass


class StageError(C2AError):
    pass


class GateError(C2AError):
    pass


class BoardError(C2AError):
    pass


class ProviderError(C2AError):
    pass
