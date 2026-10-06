"""aio_sysmon: a flight recorder for the computer itself.

It keeps a compact, power-loss-safe record of how the machine was doing, so that after a freeze,
a stall or a power cut the cause can be traced. Standard library only; designed to stay small
(fixed disk budget, adaptive detail) so it is never part of the problem.
"""

__version__ = "1.0.0"
