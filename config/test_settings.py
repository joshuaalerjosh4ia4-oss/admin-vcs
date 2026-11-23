from .settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

STORAGES["staticfiles"] = {
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
}
