# `spells` is bound before the persona module, which reads it as `browser.spells`
from bros.browser import spells
from bros.browser.browser import Browser

__all__ = ['Browser', 'spells']
