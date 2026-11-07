from __future__ import annotations

from c2a.config import Config
from c2a.core.errors import ProviderError
from c2a.providers.base import Stack
from c2a.providers.cloud import cloud_stack, missing_keys
from c2a.providers.fake import fake_stack


def build_stack(cfg: Config) -> Stack:
    if cfg.run.stack == "fake":
        stack = fake_stack()
    elif cfg.run.stack == "cloud":
        missing = missing_keys(cfg)
        if missing:
            raise ProviderError("the cloud stack needs API keys: " + "; ".join(
                f"{env} for {what}" for env, what in missing)
                + " (c2a Review: Settings → AI providers; or export them). To try the "
                "pipeline without AI, use the demo stack: --stack fake")
        stack = cloud_stack(cfg)
    else:
        raise ProviderError(
            "the local GPU stack (ComfyUI) is not available yet: use the cloud stack "
            "(--stack cloud) or the demo stack (--stack fake)")
    if cfg.run.commercial:
        bad = [p.info.name for p in stack.all() if not p.info.commercial_ok]
        if bad:
            raise ProviderError(f"run.commercial is true but these providers are not "
                                f"cleared for commercial use: {', '.join(bad)}")
    return stack
