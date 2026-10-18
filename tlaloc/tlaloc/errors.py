"""The exception taxonomy every layer shares (leaf module, imports nothing).

GuardrailError vs PolicyError is load-bearing: a guardrail breach HALTS the run
(something tried to touch what it must never touch), a policy breach rejects one
candidate and the loop continues. The earlier agent collapsed the distinction in
places and sweeps crashed on an unimported PolicyError; the hierarchy is the
contract.
"""


class TlalocError(Exception):
    pass


class ConfigError(TlalocError):
    pass


class StateError(TlalocError):
    pass


class GuardrailError(TlalocError):
    """Hard safety violated (out-of-ROE target, kill switch, danger lint).

    The loop must stop, not re-plan: a guardrail breach means the design was
    wrong or something upstream is broken.
    """


class PolicyError(TlalocError):
    """ROE bound rejected this candidate (ladder tier, caps, windows)."""


class EffectParamError(TlalocError):
    pass


class ClefError(TlalocError):
    """The decision model is down / refused / over budget."""


class LLMError(TlalocError):
    pass


class LLMUnparseable(LLMError):
    pass


class C2Error(TlalocError):
    pass


class TransportError(TlalocError):
    pass
