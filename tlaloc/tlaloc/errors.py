"""Shared exception taxonomy (leaf module): GuardrailError halts the run; PolicyError rejects one candidate and the loop continues."""


class TlalocError(Exception):
    pass


class ConfigError(TlalocError):
    pass


class StateError(TlalocError):
    pass


class GuardrailError(TlalocError):
    pass


class PolicyError(TlalocError):
    pass


class EffectParamError(TlalocError):
    pass


class ClefError(TlalocError):
    pass


class LLMError(TlalocError):
    pass


class LLMUnparseable(LLMError):
    pass


class C2Error(TlalocError):
    pass


class TransportError(TlalocError):
    pass
