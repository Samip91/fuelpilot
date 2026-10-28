import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

# Imported only after Django is set up; loads stations, places and boundary before the
# first request instead of during it.
from fuelplan.planner import warm_up  # noqa: E402 - needs the app registry from the line above

warm_up()
