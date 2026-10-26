# `spells` is bound before the persona module, which reads it as `lead.spells`
from bros.lead import spells
from bros.lead.lead import Lead

__all__ = ['Lead', 'spells']
