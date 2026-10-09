from django.conf import settings


def test_settings_load():
    assert settings.INSTALLED_APPS
