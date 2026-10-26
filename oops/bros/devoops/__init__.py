# `spells` is bound before the persona module, which reads it as `devoops.spells`
from bros.devoops import spells
from bros.devoops.devoops import Devoops

__all__ = ['Devoops', 'spells']
