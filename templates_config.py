from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.templating import Jinja2Templates

# Jinja2's LRU cache uses tuples as dict keys, which breaks on Python 3.14.
# Disabling the cache (cache_size=0) resolves the compatibility issue.
_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_env)
