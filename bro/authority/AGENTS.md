# Use/give authority

Pure symbolic algebra with no I/O.

- `values.py` — finite and cofinite strings, finite scalar sets, integer interval unions, and finite-base subset lattice interval unions.
- `patterns.py` — descriptors, matching, rectangle subtraction, and exact inclusion of finite pattern unions.
- `authority.py` — atoms, permissions, use/recipient pairs, authority joins, cap meets, lazy entries, and symbolic permission partitions.
- `rules.py` — option and type declarations, normalization, use and gift rules, admission checks, and immutable start, joint-start, and override results.
- `syntax.py` — installation-independent parsed entries and layers, explicit registry resolution, forwarding of plain gifts, and canonical value-set, permission, pattern, and entry rendering.

Lazy resolvers are pure and region-constant, and must represent the same authority as their eager expansion for every checked start.
An override result is a candidate transition: publish it only after existence checks and application succeed.
Tests compare each layer with the independent finite evaluator in `oracle_test_helper.py`.
