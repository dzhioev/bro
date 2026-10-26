# `spells` is bound before the persona module, which reads it as `bro.spells`
from bros.bro import spells
from bros.bro.bro import Bro

__all__ = ['Bro', 'spells']
