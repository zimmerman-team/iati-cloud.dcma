"""Test suite"""

import os

# The app refuses to start without an API key (spec §7). Set one before app.config is first imported, so the tests
# don't depend on the developer's .env.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
