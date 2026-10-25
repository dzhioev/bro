import pytest

from bro.local import syscall_tables


def test_x86_tables_select_native_and_common_entries_not_x32():
  assert syscall_tables.x86_table("""# header
0 common read sys_read
15 64 rt_sigreturn sys_rt_sigreturn
512 x32 rt_sigaction compat_sys_rt_sigaction
""") == {'read': 0, 'rt_sigreturn': 15}


def test_arm_tables_follow_native_conditionals_and_resolve_3264_aliases():
  source = """
#define __NR3264_fcntl 25
#ifdef __ARCH_WANT_RENAMEAT
#define __NR_renameat 38
#endif
#ifdef __ARCH_WANT_SYNC_FILE_RANGE2
#define __NR_sync_file_range2 84
#else
#define __NR_sync_file_range 84
#endif
#if defined(__SYSCALL_COMPAT) || __BITS_PER_LONG == 32
#define __NR_clock_gettime64 403
#endif
#ifdef __ARCH_WANT_MEMFD_SECRET
#define __NR_memfd_secret 447
#endif
#if __BITS_PER_LONG == 64 && !defined(__SYSCALL_COMPAT)
#define __NR_fcntl __NR3264_fcntl
#else
#define __NR_fcntl64 __NR3264_fcntl
#endif
#define __NR_arch_specific_syscall 244
#define __NR_syscalls 468
"""
  assert syscall_tables.arm_table(source) == {
    'fcntl': 25,
    'renameat': 38,
    'sync_file_range': 84,
    'memfd_secret': 447,
  }


def test_an_unreviewed_header_condition_is_rejected():
  with pytest.raises(ValueError, match='unreviewed preprocessor condition'):
    syscall_tables.arm_table('#if defined(NEW_KERNEL_OPTION)\n#endif')


def test_an_unclosed_header_condition_is_rejected():
  with pytest.raises(ValueError, match='unclosed'):
    syscall_tables.arm_table('#ifdef __ARCH_WANT_RENAMEAT')


@pytest.mark.parametrize(
  'definition',
  [
    '#define __NR_future (500)',
    '#define __NR_future __NR_base + 1',
    '#include <asm/future_syscalls.h>',
  ],
)
def test_new_header_syntax_cannot_silently_omit_syscall_decisions(definition):
  with pytest.raises(ValueError, match='unreviewed syscall'):
    syscall_tables.arm_table(definition)
