# -*- coding: utf-8 -*-
"""Where the model endpoint is configured, read from one place.

FreeCAD's parameter store, not the document and not a file in the repo: an API key must not
travel with a model someone shares. Environment variables are honoured as a fallback so a
headless session or an agent can run without anyone opening a dialog.
"""
from __future__ import annotations

import os

import FreeCAD

GROUP = "User parameter:BaseApp/Preferences/Mod/ConstraintKit"

DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "planner_model": "",
    "vision_model": "",
    "density": 1.27,
}

#: Checked when a setting is unset, so a terminal session needs no GUI. Matches the names the
#: rest of constraint-kit already uses, rather than inventing a second convention.
ENV = {
    "base_url": ("CK_BASE_URL", "LLM_BASE_URL"),
    "api_key": ("CK_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
    "planner_model": ("CK_PLANNER_MODEL", "MODEL_PLANNER"),
    "vision_model": ("CK_VISION_MODEL", "MODEL_VLM"),
}


def prefs():
    return FreeCAD.ParamGet(GROUP)


def llm_settings() -> dict:
    """base_url, api_key, planner_model, vision_model — store first, then environment."""
    p = prefs()
    out = {}
    for key in ("base_url", "api_key", "planner_model", "vision_model"):
        prop = "".join(w.capitalize() for w in key.split("_"))
        value = p.GetString(prop, "")
        if not value:
            for var in ENV.get(key, ()):  # noqa: B007
                value = os.environ.get(var, "")
                if value:
                    break
        out[key] = value
    return out


def save_llm_settings(**kw) -> None:
    p = prefs()
    for key, value in kw.items():
        prop = "".join(w.capitalize() for w in key.split("_"))
        if isinstance(value, float):
            p.SetFloat(prop, value)
        else:
            p.SetString(prop, str(value))


def density() -> float:
    return prefs().GetFloat("Density", DEFAULTS["density"])
