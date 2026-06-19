"""Dynamic compression threshold based on model context length.

Reads a simple config file to determine the optimal compression threshold
for the current model's context window size.  Short context → high threshold
(preserve turns); long context → low threshold (save tokens).

Config file: ~/.boshi/dynamic_threshold.yaml
If the config file is missing, built-in defaults are used.

Self-healing: On import, checks if the patch is applied to Hermes source.
If not, automatically applies it so upgrades don't break this feature.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BOSHI_DIR = Path.home() / ".boshi"
_CONFIG_PATH = _BOSHI_DIR / "dynamic_threshold.yaml"
_HERMES_DIR = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
_PATCH_FILE = _BOSHI_DIR / "dynamic_compression_threshold.patch"

# Marker string to check if patch is applied
_PATCH_MARKER = "from dynamic_threshold import get_dynamic_threshold"

# Built-in defaults: context_length_upper_bound → threshold
_DEFAULT_TIERS = [
    (32768,   0.80),   # ≤32K:  delay compression, preserve turns
    (131072,  0.65),   # ≤128K: balanced
    (409600,  0.50),   # ≤400K: default
    (float("inf"), 0.30),   # >400K: early compression, save tokens
]


def _check_and_apply_patch():
    """Check if our patch is present in Hermes source; auto-apply if not."""
    agent_init = _HERMES_DIR / "agent" / "agent_init.py"
    if not agent_init.is_file():
        return  # Can't check — probably wrong path

    content = agent_init.read_text(encoding="utf-8", errors="ignore")
    if _PATCH_MARKER in content:
        return  # Patch already applied

    # Patch missing — try to apply
    if not _PATCH_FILE.is_file():
        return  # No patch file to apply

    try:
        result = subprocess.run(
            ["git", "apply", str(_PATCH_FILE)],
            cwd=str(_HERMES_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            import logging
            logging.getLogger(__name__).info(
                "Auto-applied dynamic compression threshold patch"
            )
        else:
            # git apply failed — patch might not match current code
            # Don't force it, just log
            import logging
            logging.getLogger(__name__).warning(
                "Could not auto-apply patch: %s. Run manually: bash ~/.boshi/apply_dynamic_compression_patches.sh",
                result.stderr.strip()[:200],
            )
    except Exception:
        pass


# Run self-heal check on import
_check_and_apply_patch()


def _load_tiers():
    """Load threshold tiers from config file, falling back to defaults."""
    if _CONFIG_PATH.is_file():
        try:
            import yaml
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if isinstance(cfg, dict) and "tiers" in cfg:
                tiers = []
                for entry in cfg["tiers"]:
                    tiers.append((entry["max_context"], entry["threshold"]))
                if tiers:
                    return tiers
        except Exception:
            pass
    return _DEFAULT_TIERS


def get_dynamic_threshold(context_length: int) -> float | None:
    """Return the optimal compression threshold for a given context length.

    Returns None if context_length is falsy (caller should use default).
    """
    if not context_length:
        return None
    tiers = _load_tiers()
    for max_ctx, threshold in tiers:
        if context_length <= max_ctx:
            return threshold
    return tiers[-1][1]
