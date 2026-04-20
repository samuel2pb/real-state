from rental_finder.config import settings


def test_settings_loads():
    assert settings.notion_token.startswith(("ntn_", "secret_"))
    assert settings.notion_parent_page_id
    assert 1 <= len(settings.neighborhoods_list) <= 20
    assert settings.rent_price_min < settings.rent_price_max
    assert settings.work_max_distance_km > 0


def test_cron_list_non_empty():
    assert settings.cron_list
    for c in settings.cron_list:
        assert len(c.split()) == 5
