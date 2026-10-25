import os
import sys
from dataclasses import dataclass
from typing import TypedDict

SUPPORTED_ARCHITECTURES = frozenset({'aarch64', 'x86_64'})
MAXIMUM_LANDLOCK_ABI = 6
REQUIRED_PROTECTIONS = frozenset(
  {
    'seccomp',
    'landlock.fs',
    'rlimits.as',
    'rlimits.cpu',
    'rlimits.fsize',
    'rlimits.nofile',
    'rlimits.core',
  }
)


@dataclass(frozen=True)
class SandboxLimits:
  wall_seconds: float = 30
  cpu_seconds: int = 30
  address_space_bytes: int = 1024**3
  file_size_bytes: int = 64 * 1024**2
  open_files: int = 64

  def __post_init__(self) -> None:
    if not 0 < self.wall_seconds < float('inf'):
      raise ValueError('wall_seconds must be finite and positive')
    for name in ('cpu_seconds', 'address_space_bytes', 'file_size_bytes', 'open_files'):
      value = getattr(self, name)
      if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')


class SandboxReport(TypedDict):
  backend: str
  arch: str
  kernel_abi: int
  landlock_abi: int
  landlock: list[str]
  seccomp: bool
  rlimits: list[str]


def architecture() -> str:
  if sys.platform != 'linux':
    raise RuntimeError(f'no confinement backend for {sys.platform}')
  machine = os.uname().machine
  if machine not in SUPPORTED_ARCHITECTURES:
    raise RuntimeError(f'no confinement backend for linux/{machine}')
  return machine


def arm_parent_death(parent_pid: int, limits: SandboxLimits) -> None:
  """Arm the worker backstops before relocating its reply descriptor."""
  architecture()
  from . import linux

  linux.arm_parent_death(parent_pid, limits)


def apply(limits: SandboxLimits) -> SandboxReport:
  """Irreversibly confine this single-threaded worker after preloading its provider."""
  arch = architecture()
  from . import linux

  report = linux.apply(limits, arch)
  validate_report(report)
  return report


def validate_report(report: object) -> None:
  """Reject a worker report that omits or weakens a required protection."""
  if not isinstance(report, dict):
    raise RuntimeError('file sandbox unavailable: missing sandbox report')
  arch = report.get('arch')
  if (
    report.get('backend') != 'linux' or type(arch) is not str or arch not in SUPPORTED_ARCHITECTURES
  ):
    raise RuntimeError('file sandbox unavailable: missing supported backend')
  kernel_abi, abi = report.get('kernel_abi'), report.get('landlock_abi')
  if (
    type(kernel_abi) is not int
    or type(abi) is not int
    or kernel_abi < 1
    or abi != min(kernel_abi, MAXIMUM_LANDLOCK_ABI)
  ):
    raise RuntimeError('file sandbox unavailable: missing Landlock ABI')
  protections = set()
  if report.get('seccomp') is True:
    protections.add('seccomp')
  for field in ('landlock', 'rlimits'):
    values = report.get(field)
    if not isinstance(values, list) or any(type(value) is not str for value in values):
      raise RuntimeError(f'file sandbox unavailable: missing {field}')
    protections.update(f'{field}.{value}' for value in values)
  expected = set(REQUIRED_PROTECTIONS)
  if abi >= 4:
    expected.add('landlock.tcp')
  if abi >= 6:
    expected.add('landlock.scope')
  missing = expected - protections
  if missing:
    raise RuntimeError(f'file sandbox unavailable: missing {sorted(missing)[0]}')
