"""On a fresh clone there is no .env, so `settings` holds empty defaults.

Several tests reason about the configured filters (price range, neighborhoods,
work distance). Without a .env, load the shipped .env.example instead and give
the Notion fields placeholder values, so the suite runs green before
`rental-finder init`. With a .env present, nothing changes.
"""

from __future__ import annotations

from rental_finder import config

if not (config.ROOT / ".env").exists():
    _example = config.Settings(_env_file=str(config.ROOT / ".env.example"))
    for _name, _value in _example.model_dump().items():
        setattr(config.settings, _name, _value)
    config.settings.notion_token = config.settings.notion_token or "ntn_test"
    config.settings.notion_parent_page_id = (
        config.settings.notion_parent_page_id or "test-parent"
    )
