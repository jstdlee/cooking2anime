from __future__ import annotations

from c2a.config import Config
from c2a.core.errors import ProviderError
from c2a.providers.base import Stack
from c2a.providers.fake import fake_stack


def build_stack(cfg: Config) -> Stack:
    if cfg.run.stack == "fake":
        stack = fake_stack()
    else:
        raise ProviderError(
            f"stack {cfg.run.stack!r} is not available yet: real ComfyUI/fal/Fish backends "
            "land in milestones M3–M7 (docs/DEV_PLAN.md). Use --stack fake to run the "
            "pipeline end-to-end with deterministic stand-ins.")
    if cfg.run.commercial:
        bad = [p.info.name for p in stack.all() if not p.info.commercial_ok]
        if bad:
            raise ProviderError(f"run.commercial is true but these providers are not "
                                f"cleared for commercial use: {', '.join(bad)}")
    return stack
