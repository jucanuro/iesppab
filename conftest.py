import os

# Ubuntu 20.04 no soportado por Playwright; solo QA local
os.environ.setdefault("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "ubuntu22.04-x64")
