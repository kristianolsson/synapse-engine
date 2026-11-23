"""Plain Google Chrome attached over CDP (see launcher.py)."""
from .launcher import ChromeLaunchError, chrome_cdp

__all__ = ["ChromeLaunchError", "chrome_cdp"]
