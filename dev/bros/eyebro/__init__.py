# `spells` is bound before the persona module, which reads it as `eyebro.spells`
from bros.eyebro import spells
from bros.eyebro.eyebro import Eyebro

__all__ = ['Eyebro', 'spells']
